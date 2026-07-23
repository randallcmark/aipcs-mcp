"""SYNTHETIC_FIXTURE. Low-level MCP catalogue and fail-closed callback tests."""

from __future__ import annotations

import json
from typing import Any

import anyio
import pytest
from mcp import types

import aipcs_mcp.mcp_server as mcp_server

_NAMES = [
    "aipcs_server_info",
    "aipcs_service_seed",
    "aipcs_service_list",
    "aipcs_service_inspect",
    "aipcs_service_design",
    "aipcs_service_materialise",
    "aipcs_service_evolve",
]
_DESCRIPTIONS = {
    "aipcs_server_info": "Return the public AIPCS MCP capability contract.",
    "aipcs_service_seed": "Create or replay a durable service seed.",
    "aipcs_service_list": "List durable services for the configured principal.",
    "aipcs_service_inspect": "Inspect one durable service projection.",
    "aipcs_service_design": "Validate and store an initial service design.",
    "aipcs_service_materialise": "Materialise a designed service using an exact lifecycle precondition.",
    "aipcs_service_evolve": "Evolve a materialised service with one complete adjacent manifest.",
}
_REQUIRED = {
    "aipcs_server_info": [],
    "aipcs_service_seed": [
        "domain_name",
        "domain_class",
        "intent_description",
        "idempotency_key",
    ],
    "aipcs_service_list": [],
    "aipcs_service_inspect": ["service_id"],
    "aipcs_service_design": ["service_id", "schema", "idempotency_key"],
    "aipcs_service_materialise": [
        "service_id",
        "expected_service_revision",
        "expected_schema_version",
        "idempotency_key",
    ],
    "aipcs_service_evolve": [
        "service_id",
        "expected_service_revision",
        "expected_schema_version",
        "idempotency_key",
        "schema",
    ],
}
_RESULT_MODELS = {
    "aipcs_server_info": "ServerInfo",
    "aipcs_service_seed": "ServiceMetadata",
    "aipcs_service_list": "ServiceListResult",
    "aipcs_service_inspect": "ServiceMetadata",
    "aipcs_service_design": "ServiceMetadata",
    "aipcs_service_materialise": "ServiceMetadata",
    "aipcs_service_evolve": "ServiceMetadata",
}


async def _catalogue(server: object) -> list[types.Tool]:
    handler = server.request_handlers[types.ListToolsRequest]  # type: ignore[attr-defined]
    result = await handler(types.ListToolsRequest())
    return list(result.root.tools)


async def _call(server: object, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    handler = server.request_handlers[types.CallToolRequest]  # type: ignore[attr-defined]
    request = types.CallToolRequest(
        params=types.CallToolRequestParams(name=name, arguments=arguments)
    )
    return (await handler(request)).root


def test_low_level_catalogue_is_exact_and_flat() -> None:
    stateless = mcp_server.create_server()
    assert [tool.name for tool in anyio.run(_catalogue, stateless)] == ["aipcs_server_info"]

    class Executor:
        def execute(self, command: object) -> object:
            raise AssertionError("catalogue construction must not execute lifecycle work")

    ready = mcp_server.create_server(
        application=object(),  # type: ignore[arg-type]
        principal_id="configured-principal",
        registry_lifecycle=True,
        lifecycle_executor=Executor(),  # type: ignore[arg-type]
    )
    tools = anyio.run(_catalogue, ready)
    assert [tool.name for tool in tools] == _NAMES
    for tool in tools:
        assert tool.description == _DESCRIPTIONS[tool.name]
        assert tool.inputSchema["type"] == "object"
        assert tool.inputSchema["additionalProperties"] is False
        assert tool.inputSchema["required"] == _REQUIRED[tool.name]
        assert "request" not in tool.inputSchema["properties"]
        assert tool.outputSchema is not None
        success_name = f"_TypedSuccessEnvelope_{_RESULT_MODELS[tool.name]}_"
        assert tool.outputSchema["anyOf"] == [
            {"$ref": f"#/$defs/{success_name}"},
            {"$ref": "#/$defs/FailureEnvelope"},
        ]
        success_schema = tool.outputSchema["$defs"][success_name]
        assert success_schema["additionalProperties"] is False
        assert success_schema["properties"]["result"] == {
            "$ref": f"#/$defs/{_RESULT_MODELS[tool.name]}"
        }
        result_schema = tool.outputSchema["$defs"][_RESULT_MODELS[tool.name]]
        assert result_schema["additionalProperties"] is False
        assert {"principal_id", "created_via", "audit"}.isdisjoint(result_schema["properties"])
    list_schema = next(tool.inputSchema for tool in tools if tool.name == "aipcs_service_list")
    assert list_schema["properties"]["limit"] == {
        "default": 100,
        "maximum": 100,
        "minimum": 1,
        "title": "Limit",
        "type": "integer",
    }


def test_incomplete_lifecycle_binding_fails_during_construction() -> None:
    with pytest.raises(ValueError, match="Incomplete lifecycle binding"):
        mcp_server.create_server(application=object(), registry_lifecycle=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Incomplete lifecycle binding"):
        mcp_server.create_server(principal_id="configured-principal", registry_lifecycle=True)
    with pytest.raises(ValueError, match="Incomplete lifecycle binding"):
        mcp_server.create_server(application=object(), principal_id="configured-principal")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Incomplete materialisation lifecycle binding"):
        mcp_server.create_server(lifecycle_executor=object())  # type: ignore[arg-type]


@pytest.mark.parametrize("fault", ["dispatch", "result"])
def test_callback_faults_are_converted_before_sdk_serialization(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    secret = "injected-traceback-secret"
    if fault == "dispatch":
        monkeypatch.setattr(
            mcp_server,
            "_dispatch",
            lambda *_: (_ for _ in ()).throw(RuntimeError(secret)),
        )
    else:
        monkeypatch.setattr(
            mcp_server,
            "_result",
            lambda *_: (_ for _ in ()).throw(RuntimeError(secret)),
        )
    server = mcp_server.create_server()
    result = anyio.run(_call, server, "aipcs_server_info", {})
    assert result.isError is True
    assert result.structuredContent == json.loads(result.content[0].text)
    assert result.structuredContent["error"]["code"] == "internal_error"
    assert secret not in result.content[0].text
