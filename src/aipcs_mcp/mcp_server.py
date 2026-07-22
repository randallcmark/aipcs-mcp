"""Low-level MCP transport adapter for the deliberately narrow public surface."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from pydantic import TypeAdapter

from . import __version__
from .application.errors import Conflict, InternalFailure, InvalidCommand, InvalidState, NotFound
from .application.models import ApplicationContext, DesignCommand, SeedCommand
from .application.services import ServiceApplication
from .contracts import (
    ServerInfo,
    ServiceDesignRequest,
    ServiceInspectRequest,
    ServiceListRequest,
    ServiceListResult,
    ServiceMetadata,
    ServiceSeedRequest,
    parse_service_design,
    parse_service_inspect,
    parse_service_list,
    parse_service_seed,
    public_server_info,
)
from .errors import (
    AipcsContractError,
    ErrorCode,
    FailureEnvelope,
    PublicModel,
    SuccessEnvelope,
    success,
)


class _TypedSuccessEnvelope[ResultT](PublicModel):
    """Schema-only success envelope with a concrete per-tool result shape."""

    ok: Literal[True] = True
    result: ResultT
    error: None = None


_EMPTY_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}
_INTERNAL_RESULT = types.CallToolResult(
    content=[
        types.TextContent(
            type="text",
            text='{"ok":false,"result":null,"error":{"code":"internal_error","message":"The request could not be completed safely.","issues":[],"retryable":false}}',
        )
    ],
    structuredContent={
        "ok": False,
        "result": None,
        "error": {
            "code": "internal_error",
            "message": "The request could not be completed safely.",
            "issues": [],
            "retryable": False,
        },
    },
    isError=True,
)


def create_server(
    *,
    application: ServiceApplication | None = None,
    principal_id: str | None = None,
    registry_lifecycle: bool = False,
) -> Server:
    """Build the finite server catalogue; configuration is composed elsewhere."""

    if registry_lifecycle != (application is not None and principal_id is not None):
        raise ValueError("Incomplete lifecycle binding.")
    if registry_lifecycle and not _valid_principal(principal_id):
        raise ValueError("Invalid lifecycle binding.")
    server = Server("aipcs-mcp", version=__version__)
    tools = _tools(registry_lifecycle)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return tools

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        # This catches every failure before the SDK's wrapper can serialize exception text.
        try:
            envelope = _dispatch(name, arguments, application, principal_id, registry_lifecycle)
            return _result(envelope)
        except Exception:
            return _safe_internal_result()

    return server


async def run_stdio(server: Server) -> None:
    """Run one already-composed low-level server over the public stdio seam."""

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _tools(registry_lifecycle: bool) -> list[types.Tool]:
    values = [
        _tool(
            "aipcs_server_info",
            "Return the public AIPCS MCP capability contract.",
            _EMPTY_SCHEMA,
            ServerInfo,
        )
    ]
    if registry_lifecycle:
        values.extend(
            [
                _tool(
                    "aipcs_service_seed",
                    "Create or replay a durable service seed.",
                    ServiceSeedRequest,
                    ServiceMetadata,
                ),
                _tool(
                    "aipcs_service_list",
                    "List durable services for the configured principal.",
                    ServiceListRequest,
                    ServiceListResult,
                ),
                _tool(
                    "aipcs_service_inspect",
                    "Inspect one durable service projection.",
                    ServiceInspectRequest,
                    ServiceMetadata,
                ),
                _tool(
                    "aipcs_service_design",
                    "Validate and store an initial service design.",
                    ServiceDesignRequest,
                    ServiceMetadata,
                ),
            ]
        )
    return values


def _tool(
    name: str,
    description: str,
    request: type[Any] | dict[str, object],
    result: type[Any],
) -> types.Tool:
    schema = request if isinstance(request, dict) else request.model_json_schema(by_alias=True)
    schema.setdefault("required", [])
    schema["additionalProperties"] = False
    output_schema = TypeAdapter(_TypedSuccessEnvelope[result] | FailureEnvelope).json_schema(
        by_alias=True
    )
    return types.Tool(
        name=name, description=description, inputSchema=schema, outputSchema=output_schema
    )


def _dispatch(
    name: object,
    arguments: object,
    application: ServiceApplication | None,
    principal_id: str | None,
    registry_lifecycle: bool,
) -> SuccessEnvelope | FailureEnvelope:
    if not isinstance(name, str):
        return _failure(ErrorCode.UNSUPPORTED_OPERATION, "The requested MCP tool is unavailable.")
    if not isinstance(arguments, Mapping):
        return _failure(ErrorCode.VALIDATION_FAILED, "Request arguments must be a JSON object.")
    if name == "aipcs_server_info":
        if arguments:
            return _failure(
                ErrorCode.VALIDATION_FAILED, "Request failed public contract validation."
            )
        return success(
            public_server_info(registry_lifecycle=registry_lifecycle).model_dump(mode="json")
        )
    if not registry_lifecycle or application is None or principal_id is None:
        return _failure(ErrorCode.UNSUPPORTED_OPERATION, "The requested MCP tool is unavailable.")
    context = ApplicationContext(principal_id=principal_id, created_via="mcp")
    try:
        if name == "aipcs_service_seed":
            request = parse_service_seed(arguments)
            result = application.seed(
                context,
                SeedCommand(
                    domain_name=request.domain_name,
                    domain_class=request.domain_class,
                    intent_description=request.intent_description,
                    idempotency_key=request.idempotency_key,
                ),
            )
        elif name == "aipcs_service_list":
            request = parse_service_list(arguments)
            result = ServiceListResult(services=application.list(context, request.limit))
        elif name == "aipcs_service_inspect":
            request = parse_service_inspect(arguments)
            result = application.inspect(context, request.service_id)
        elif name == "aipcs_service_design":
            request = parse_service_design(arguments)
            result = application.design(
                context,
                DesignCommand(
                    service_id=request.service_id,
                    manifest=request.schema_,
                    idempotency_key=request.idempotency_key,
                ),
            )
        else:
            return _failure(
                ErrorCode.UNSUPPORTED_OPERATION, "The requested MCP tool is unavailable."
            )
    except AipcsContractError as error:
        return error.envelope()
    except NotFound:
        return _failure(ErrorCode.NOT_FOUND, "The requested service was not found.")
    except Conflict:
        return _failure(
            ErrorCode.CONFLICT, "The request conflicts with existing application state."
        )
    except InvalidState:
        return _failure(
            ErrorCode.INVALID_STATE, "The service is not in a state that permits this operation."
        )
    except InvalidCommand:
        return _failure(ErrorCode.VALIDATION_FAILED, "The application command is invalid.")
    except InternalFailure:
        return _failure(
            ErrorCode.INTERNAL_ERROR, "The application operation could not be completed."
        )
    except Exception:
        return _failure(ErrorCode.INTERNAL_ERROR, "The request could not be completed safely.")
    try:
        return success(result.model_dump(mode="json", by_alias=True, warnings="error"))
    except Exception:
        return _failure(ErrorCode.INTERNAL_ERROR, "The request could not be completed safely.")


def _failure(code: ErrorCode, message: str) -> FailureEnvelope:
    return FailureEnvelope(error={"code": code, "message": message})


def _result(envelope: SuccessEnvelope | FailureEnvelope) -> types.CallToolResult:
    try:
        payload = envelope.model_dump(mode="json", warnings="error")
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            structuredContent=payload,
            isError=not envelope.ok,
        )
    except Exception:
        return _safe_internal_result()


def _safe_internal_result() -> types.CallToolResult:
    return _INTERNAL_RESULT


def _valid_principal(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= 128
        and value.isprintable()
    )
