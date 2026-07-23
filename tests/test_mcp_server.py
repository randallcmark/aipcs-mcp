"""SYNTHETIC_FIXTURE. Low-level MCP catalogue and fail-closed callback tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import anyio
import pytest
from mcp import types

import aipcs_mcp.mcp_server as mcp_server
from aipcs_mcp.records import (
    BranchMutationOutcome,
    BranchValue,
    CreateBranchCommand,
    DataFailure,
)

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
    with pytest.raises(ValueError, match="Incomplete data runtime binding"):
        mcp_server.create_server(data_application=object())  # type: ignore[arg-type]


def test_complete_data_binding_exposes_exact_21_tools_and_fixed_context() -> None:
    class Executor:
        def execute(self, command: object) -> object:
            raise AssertionError("data dispatch must not execute lifecycle work")

    seen: list[tuple[object, object]] = []

    class DataApplication:
        def create_record(self, context: object, service_id: object, command: object) -> DataFailure:
            seen.append((context, command))
            return DataFailure("storage_busy")

    server = mcp_server.create_server(
        application=object(),  # type: ignore[arg-type]
        principal_id="configured-principal",
        registry_lifecycle=True,
        lifecycle_executor=Executor(),  # type: ignore[arg-type]
        data_application=DataApplication(),  # type: ignore[arg-type]
    )
    tools = anyio.run(_catalogue, server)
    assert len(tools) == 21
    assert [tool.name for tool in tools][-14:] == [
        "aipcs_record_create", "aipcs_record_get", "aipcs_record_list", "aipcs_record_search",
        "aipcs_record_update", "aipcs_record_delete", "aipcs_record_history", "aipcs_bootstrap",
        "aipcs_service_summary", "aipcs_branch_create", "aipcs_branch_list", "aipcs_branch_update",
        "aipcs_branch_assign_records", "aipcs_maintenance_scan",
    ]
    result = anyio.run(
        _call,
        server,
        "aipcs_record_create",
        {
            "service_id": "00000000-0000-0000-0000-000000000101",
            "entity_name": "project",
            "record": {"title": "A"},
            "idempotency_key": "create",
        },
    )
    assert result.structuredContent["error"] == {
        "code": "storage_busy", "message": "Storage is temporarily busy.", "issues": [], "retryable": True,
    }
    assert seen[0][0].principal_id == "configured-principal"
    assert seen[0][0].created_via == "mcp"
    assert seen[0][1].record.to_dict() == {"title": "A"}


def test_public_branch_slug_boundaries_never_become_internal_errors() -> None:
    class Executor:
        def execute(self, command: object) -> object:
            raise AssertionError("branch dispatch must not execute lifecycle work")

    seen: list[CreateBranchCommand] = []

    class DataApplication:
        def create_branch(
            self, context: object, service_id: object, command: CreateBranchCommand
        ) -> BranchMutationOutcome:
            seen.append(command)
            now = datetime(2026, 7, 23, tzinfo=UTC)
            return BranchMutationOutcome(
                BranchValue(
                    UUID("00000000-0000-0000-0000-000000000201"),
                    command.slug,
                    command.title,
                    command.intent,
                    command.branch_type,
                    command.parent_branch_id,
                    "active",
                    command.retrieval_summary,
                    now,
                    now,
                    1,
                ),
                False,
            )

    server = mcp_server.create_server(
        application=object(),  # type: ignore[arg-type]
        principal_id="configured-principal",
        registry_lifecycle=True,
        lifecycle_executor=Executor(),  # type: ignore[arg-type]
        data_application=DataApplication(),  # type: ignore[arg-type]
    )
    valid_slug = "a" + "b" * 63
    valid = anyio.run(
        _call,
        server,
        "aipcs_branch_create",
        {
            "service_id": "00000000-0000-0000-0000-000000000101",
            "slug": valid_slug,
            "title": "Boundary",
            "intent": "Exercise the published branch slug boundary.",
            "idempotency_key": "branch-boundary-valid",
        },
    )
    assert valid.isError is False
    assert valid.structuredContent["result"]["branch"]["slug"] == valid_slug
    assert seen == [
        CreateBranchCommand(
            valid_slug,
            "Boundary",
            "Exercise the published branch slug boundary.",
            None,
            None,
            None,
            "branch-boundary-valid",
        )
    ]

    invalid = anyio.run(
        _call,
        server,
        "aipcs_branch_create",
        {
            "service_id": "00000000-0000-0000-0000-000000000101",
            "slug": "a" + "b" * 64,
            "title": "Boundary",
            "intent": "Reject an overlong branch slug.",
            "idempotency_key": "branch-boundary-invalid",
        },
    )
    assert invalid.isError is True
    assert invalid.structuredContent["error"]["code"] == "validation_failed"
    assert len(seen) == 1


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
