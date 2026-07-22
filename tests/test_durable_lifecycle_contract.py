from __future__ import annotations

import anyio
import pytest
from fixtures import valid_design_request
from mcp import types

from aipcs_mcp.application.errors import (
    Conflict,
    InternalFailure,
    InvalidCommand,
    InvalidState,
    NotFound,
)
from aipcs_mcp.application.models import ApplicationContext
from aipcs_mcp.contracts import (
    parse_service_design,
    parse_service_inspect,
    parse_service_list,
    parse_service_seed,
    public_server_info,
)
from aipcs_mcp.errors import AipcsContractError, ErrorCode
from aipcs_mcp.mcp_server import _dispatch, create_server


async def _tool_names(server: object) -> list[str]:
    handler = server.request_handlers[types.ListToolsRequest]  # type: ignore[attr-defined]
    result = await handler(types.ListToolsRequest())
    return [tool.name for tool in result.root.tools]


def test_low_level_catalogue_is_finite_and_profile_truthful() -> None:
    stateless = create_server()
    assert anyio.run(_tool_names, stateless) == ["aipcs_server_info"]
    assert public_server_info().features.registry_lifecycle is False
    with pytest.raises(ValueError):
        create_server(application=object(), registry_lifecycle=True)  # type: ignore[arg-type]


def test_service_design_parser_preserves_precedence() -> None:
    request = valid_design_request()
    request["idempotency_key"] = "design-1"
    request["service_id"] = "00000000-0000-0000-0000-000000000000"
    request["schema"] = {"manifest_version": 1, "tool_definitions": []}
    with pytest.raises(AipcsContractError) as raised:
        parse_service_design(request)
    assert raised.value.code is ErrorCode.INVALID_IDENTIFIER


def test_service_design_parser_rejects_retired_before_legacy() -> None:
    request = valid_design_request()
    request["idempotency_key"] = "design-1"
    request["schema"] = {"manifest_version": 1, "tool_definitions": []}
    with pytest.raises(AipcsContractError) as raised:
        parse_service_design(request)
    assert raised.value.code is ErrorCode.RETIRED_FIELD


def test_design_parser_requires_strict_idempotency_key() -> None:
    with pytest.raises(AipcsContractError) as raised:
        parse_service_design(valid_design_request())
    assert raised.value.code is ErrorCode.VALIDATION_FAILED


def test_service_design_missing_identifier_is_strict_validation() -> None:
    with pytest.raises(AipcsContractError) as raised:
        parse_service_design({"idempotency_key": "design-1"})
    assert raised.value.code is ErrorCode.VALIDATION_FAILED


@pytest.mark.parametrize("value", [True, "1", 101, 0])
def test_list_parser_rejects_coercion_and_limits(value: object) -> None:
    with pytest.raises(AipcsContractError) as raised:
        parse_service_list({"limit": value})
    assert raised.value.code is ErrorCode.VALIDATION_FAILED


@pytest.mark.parametrize(
    "value",
    ["not-a-uuid", "5B5D0B6A-976C-4E32-B4D2-CC0A64E3EE23", "00000000-0000-0000-0000-000000000000"],
)
def test_inspect_parser_gives_identifier_precedence(value: str) -> None:
    with pytest.raises(AipcsContractError) as raised:
        parse_service_inspect({"service_id": value})
    assert raised.value.code is ErrorCode.INVALID_IDENTIFIER


def test_seed_parser_rejects_missing_and_unknown_fields() -> None:
    for payload in ({}, {"unexpected": "never echoed"}):
        with pytest.raises(AipcsContractError) as raised:
            parse_service_seed(payload)
        assert raised.value.code is ErrorCode.VALIDATION_FAILED


def test_lifecycle_server_has_flat_strict_schemas() -> None:
    class Application:
        def seed(self, context: ApplicationContext, command: object) -> object:
            raise NotFound()

        def list(self, context: ApplicationContext, limit: int) -> list[object]:
            return []

        def inspect(self, context: ApplicationContext, service_id: object) -> object:
            raise NotFound()

        def design(self, context: ApplicationContext, command: object) -> object:
            raise NotFound()

    server = create_server(application=Application(), principal_id="fixed", registry_lifecycle=True)  # type: ignore[arg-type]

    async def snapshot() -> dict[str, dict[str, object]]:
        handler = server.request_handlers[types.ListToolsRequest]
        result = await handler(types.ListToolsRequest())
        return {tool.name: tool.inputSchema for tool in result.root.tools}

    schemas = anyio.run(snapshot)
    assert list(schemas) == [
        "aipcs_server_info",
        "aipcs_service_seed",
        "aipcs_service_list",
        "aipcs_service_inspect",
        "aipcs_service_design",
    ]
    for schema in schemas.values():
        assert schema["additionalProperties"] is False
    assert "request" not in schemas["aipcs_service_seed"]["properties"]
    assert schemas["aipcs_service_list"]["properties"]["limit"]["type"] == "integer"


def test_transport_owns_fixed_context_and_safe_validation() -> None:
    seen: list[ApplicationContext] = []

    class Application:
        def seed(self, context: ApplicationContext, command: object) -> object:
            seen.append(context)
            raise NotFound()

        def list(self, context: ApplicationContext, limit: int) -> list[object]:
            return []

        def inspect(self, context: ApplicationContext, service_id: object) -> object:
            raise NotFound()

        def design(self, context: ApplicationContext, command: object) -> object:
            raise NotFound()

    server = create_server(
        application=Application(), principal_id="fixed-principal", registry_lifecycle=True
    )  # type: ignore[arg-type]

    async def call(arguments: dict[str, object]) -> types.CallToolResult:
        handler = server.request_handlers[types.CallToolRequest]
        request = types.CallToolRequest(
            params=types.CallToolRequestParams(name="aipcs_service_seed", arguments=arguments)
        )
        return (await handler(request)).root

    good = anyio.run(
        call,
        {
            "domain_name": "project",
            "domain_class": "project",
            "intent_description": "durable context",
            "idempotency_key": "seed-1",
        },
    )
    assert good.structuredContent["error"]["code"] == "not_found"
    assert seen == [ApplicationContext(principal_id="fixed-principal", created_via="mcp")]
    bad = anyio.run(call, {"unexpected": "never echoed"})
    assert bad.structuredContent["error"]["code"] == "validation_failed"
    assert "never echoed" not in bad.content[0].text


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (NotFound, "not_found"),
        (Conflict, "conflict"),
        (InvalidState, "invalid_state"),
        (InvalidCommand, "validation_failed"),
        (InternalFailure, "internal_error"),
    ],
)
def test_dispatch_maps_application_classes_explicitly(error: type[Exception], code: str) -> None:
    class Application:
        def seed(self, context: ApplicationContext, command: object) -> object:
            raise error()

    envelope = _dispatch(
        "aipcs_service_seed",
        {
            "domain_name": "project",
            "domain_class": "project",
            "intent_description": "durable context",
            "idempotency_key": "seed-1",
        },
        Application(),  # type: ignore[arg-type]
        "fixed-principal",
        True,
    )
    assert envelope.error is not None
    assert envelope.error.code == code
