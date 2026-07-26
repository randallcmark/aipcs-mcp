from __future__ import annotations

import pytest
from fixtures import valid_design_request, valid_manifest

import aipcs_mcp.legacy_v1
from aipcs_mcp.contracts import (
    StorageSummary,
    parse_public_design,
    parse_service_evolve,
    parse_service_materialise,
    public_server_info,
    validate_transport_environment,
)
from aipcs_mcp.errors import AipcsContractError, ErrorCode


def test_server_info_is_safe_and_capability_versioned() -> None:
    data = public_server_info().model_dump(mode="json")
    assert data == {
        "server_name": "aipcs-mcp",
        "package_version": "0.0.0.dev0",
        "aipcs_mcp_contract": "1.2.0",
        "supported_manifest_versions": [2],
        "transports": ["stdio", "streamable-http"],
        "features": {
            "server_info": True,
            "manifest_v2_validation": True,
            "legacy_v1_importer": True,
            "stdio_preflight": True,
            "registry_lifecycle": False,
            "materialisation_lifecycle": False,
            "record_runtime": False,
            "discovery_topology": False,
        },
        "operational_statuses": ["active"],
    }
    assert "path" not in repr(data).lower()
    assert "dsn" not in repr(data).lower()


def test_data_runtime_capabilities_are_an_atomic_ready_binding() -> None:
    with pytest.raises(ValueError):
        public_server_info(record_runtime=True, discovery_topology=False)
    with pytest.raises(ValueError):
        public_server_info(record_runtime=True, discovery_topology=True)
    info = public_server_info(
        registry_lifecycle=True,
        materialisation_lifecycle=True,
        record_runtime=True,
        discovery_topology=True,
    )
    assert info.features.record_runtime is True
    assert info.features.discovery_topology is True


def test_public_storage_summary_uses_the_exact_service_store_namespace() -> None:
    valid = "svc_00000000000000000000000000000001"
    assert StorageSummary(backend="sqlite", namespace=valid).model_dump(mode="json") == {
        "backend": "sqlite",
        "namespace": valid,
    }
    hostile = (
        "svc_00000000000000000000000000000000",
        "svc_agent_defined-name",
        "svc_0000000000000000000000000000000A",
        "svc_%2f",
        "sqlite://host/database",
        "file:" + "//host/share/database",
        "svc_0000000000000000000000000000000é",
    )
    for value in hostile:
        with pytest.raises(ValueError):
            StorageSummary(backend="sqlite", namespace=value)
    for backend in ("mysql", "SQLite", " sqlite", 1, None):
        with pytest.raises(ValueError):
            StorageSummary(backend=backend, namespace=valid)


def test_normal_design_accepts_v2_only() -> None:
    assert parse_public_design(valid_design_request()).schema_.manifest_version == 2
    legacy = valid_design_request()
    legacy["schema"] = {"manifest_version": 1}
    with pytest.raises(AipcsContractError) as raised:
        parse_public_design(legacy)
    assert raised.value.code is ErrorCode.LEGACY_IMPORT_REQUIRED


def test_normal_design_rejects_retired_fields_with_stable_error() -> None:
    request = valid_design_request()
    request["schema"]["tool_definitions"] = []
    with pytest.raises(AipcsContractError) as raised:
        parse_public_design(request)
    envelope = raised.value.envelope().model_dump(mode="json")
    assert envelope["error"]["code"] == "retired_field"
    assert "traceback" not in repr(envelope).lower()


def test_normal_design_rejects_malformed_identifier_with_specific_code() -> None:
    request = valid_design_request()
    request["service_id"] = "not-a-uuid"
    with pytest.raises(AipcsContractError) as raised:
        parse_public_design(request)
    assert raised.value.code is ErrorCode.INVALID_IDENTIFIER


def test_normal_design_requires_initial_schema_state() -> None:
    request = valid_design_request()
    request["schema"]["schema_version"] = 2
    with pytest.raises(AipcsContractError) as raised:
        parse_public_design(request)
    assert raised.value.code is ErrorCode.VALIDATION_FAILED
    assert raised.value.issues[0].code == "invalid_value"


def test_normal_design_rejects_future_manifest_version() -> None:
    request = valid_design_request()
    request["schema"]["manifest_version"] = 3
    with pytest.raises(AipcsContractError) as raised:
        parse_public_design(request)
    assert raised.value.code is ErrorCode.MANIFEST_VERSION_UNSUPPORTED


def test_normal_v2_intake_never_calls_legacy_converter(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_called(source: object) -> object:
        raise AssertionError(f"legacy converter called unexpectedly: {type(source).__name__}")

    monkeypatch.setattr(aipcs_mcp.legacy_v1, "import_legacy_v1", fail_if_called)
    assert parse_public_design(valid_design_request()).schema_.manifest_version == 2


def test_validation_error_sanitises_oversized_untrusted_location() -> None:
    request = valid_design_request()
    request["schema"]["x" * 1_000] = "not echoed"
    with pytest.raises(AipcsContractError) as raised:
        parse_public_design(request)
    envelope = raised.value.envelope().model_dump(mode="json")
    issue = envelope["error"]["issues"][0]
    assert len(issue["path"]) <= 240
    assert "x" * 100 not in repr(envelope)
    assert "not echoed" not in repr(envelope)


@pytest.mark.parametrize(
    ("transport", "environment"),
    [
        ("sse", {}),
        (None, {"FASTMCP_PORT": "8000"}),
        (None, {"AIPCS_HOST": "127.0.0.1"}),
        ("stdio", {"AIPCS_MCP_TRANSPORT": "sse"}),
        ("stdio", {"FASTMCP_STREAMABLE_HTTP_PATH": "/mcp"}),
    ],
)
def test_undocumented_listener_configuration_is_rejected_before_server_construction(
    transport: str | None, environment: dict[str, str]
) -> None:
    with pytest.raises(AipcsContractError) as raised:
        validate_transport_environment(transport, environment)
    assert raised.value.code is ErrorCode.TRANSPORT_NOT_SUPPORTED


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
def test_documented_transport_is_allowed(transport: str) -> None:
    assert validate_transport_environment(transport, {}) == transport


def test_lifecycle_parsers_require_strict_preconditions_and_detach_evolve_manifest() -> None:
    materialise = parse_service_materialise(
        {
            "service_id": "5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23",
            "expected_service_revision": 2,
            "expected_schema_version": 1,
            "idempotency_key": "materialise-1",
        }
    )
    assert materialise.expected_service_revision == 2

    schema = valid_manifest()
    schema["schema_version"] = 2
    schema["migration_history"] = [
        {"from_schema_version": 1, "to_schema_version": 2, "operations": ["add summary"]}
    ]
    request = {
        "service_id": "5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23",
        "expected_service_revision": 3,
        "expected_schema_version": 1,
        "idempotency_key": "evolve-1",
        "schema": schema,
    }
    evolve = parse_service_evolve(request)
    request["schema"]["entities"][0]["name"] = "mutated_after_validation"
    assert evolve.schema_.entities[0].name == "project"


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (
            parse_service_materialise,
            {
                "service_id": "5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23",
                "expected_service_revision": True,
                "expected_schema_version": 1,
                "idempotency_key": "materialise-1",
            },
        ),
        (
            parse_service_materialise,
            {
                "service_id": "5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23",
                "expected_service_revision": 1,
                "expected_schema_version": 2,
                "idempotency_key": "materialise-1",
            },
        ),
        (
            parse_service_evolve,
            {
                "service_id": "5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23",
                "expected_service_revision": 1,
                "expected_schema_version": 1,
                "idempotency_key": "evolve-1",
                "schema": {"manifest_version": 1},
            },
        ),
    ],
)
def test_lifecycle_parsers_reject_invalid_input_before_execution(
    parser: object, payload: dict[str, object]
) -> None:
    with pytest.raises(AipcsContractError):
        parser(payload)  # type: ignore[operator]
