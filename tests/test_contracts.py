from __future__ import annotations

import pytest
from fixtures import valid_design_request

import aipcs_mcp.legacy_v1
from aipcs_mcp.contracts import parse_public_design, public_server_info, validate_stdio_only
from aipcs_mcp.errors import AipcsContractError, ErrorCode


def test_server_info_is_safe_and_capability_versioned() -> None:
    data = public_server_info().model_dump(mode="json")
    assert data == {
        "server_name": "aipcs-mcp",
        "package_version": "0.0.0.dev0",
        "aipcs_mcp_contract": "1.0",
        "supported_manifest_versions": [2],
        "transports": ["stdio"],
        "features": {
            "server_info": True,
            "manifest_v2_validation": True,
            "legacy_v1_importer": True,
            "stdio_preflight": True,
        },
        "operational_statuses": ["active"],
    }
    assert "path" not in repr(data).lower()
    assert "dsn" not in repr(data).lower()


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
        ("streamable-http", {}),
        (None, {"FASTMCP_PORT": "8000"}),
        (None, {"AIPCS_HOST": "127.0.0.1"}),
        ("stdio", {"AIPCS_MCP_TRANSPORT": "sse"}),
        ("stdio", {"FASTMCP_STREAMABLE_HTTP_PATH": "/mcp"}),
    ],
)
def test_listener_configuration_is_rejected_before_server_construction(
    transport: str | None, environment: dict[str, str]
) -> None:
    with pytest.raises(AipcsContractError) as raised:
        validate_stdio_only(transport, environment)
    assert raised.value.code is ErrorCode.TRANSPORT_NOT_SUPPORTED


def test_stdio_configuration_is_allowed() -> None:
    assert validate_stdio_only("stdio", {}) == "stdio"
