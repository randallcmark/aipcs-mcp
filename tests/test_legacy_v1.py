from __future__ import annotations

import pytest
from fixtures import synthetic_legacy_manifest

from aipcs_mcp.errors import AipcsContractError, ErrorCode
from aipcs_mcp.legacy_v1 import import_legacy_v1


def test_synthetic_legacy_fixture_converts_one_way_with_provenance() -> None:
    result = import_legacy_v1(synthetic_legacy_manifest())
    converted = result.manifest.model_dump(mode="json")
    fields = {attribute["name"]: attribute for attribute in converted["entities"][0]["attributes"]}
    assert converted["manifest_version"] == 2
    assert converted["migration_history"] == []
    assert fields["score"]["type"] == "number"
    assert fields["record_version"] == {
        "name": "record_version",
        "type": "integer",
        "required": True,
        "primary_key": False,
        "description": None,
        "allowed_values": None,
        "retrieval_mode": None,
    }
    assert result.provenance.source == "legacy_v1"
    assert result.provenance.source_manifest_version == 1
    assert {"tool_definitions", "endpoint", "aliases", "migration_history"} <= set(
        result.discarded_fields
    )
    assert any(warning.code == "legacy_type_normalised" for warning in result.warnings)
    assert any(warning.code == "server_managed_field_normalised" for warning in result.warnings)
    assert any(warning.code == "legacy_migration_history_discarded" for warning in result.warnings)


def test_legacy_nonempty_relationships_are_not_silently_preserved() -> None:
    legacy = synthetic_legacy_manifest()
    legacy["relationships"] = [{"from": "note.project_id", "to": "project.id"}]
    with pytest.raises(AipcsContractError) as raised:
        import_legacy_v1(legacy)
    assert raised.value.code is ErrorCode.VALIDATION_FAILED
    assert raised.value.issues[0].code == "legacy_relationship_ambiguous"


def test_legacy_unqualified_ambiguous_facet_is_rejected() -> None:
    legacy = synthetic_legacy_manifest()
    legacy["entities"].append(
        {
            "name": "tag",
            "fields": [
                {"name": "id", "data_type": "uuid", "required": True, "primary_key": True},
                {"name": "category", "data_type": "string", "required": True},
            ],
        }
    )
    legacy["discovery_facets"] = ["category"]
    with pytest.raises(AipcsContractError) as raised:
        import_legacy_v1(legacy)
    assert raised.value.code is ErrorCode.VALIDATION_FAILED
    assert "ambiguous" in raised.value.message


def test_legacy_importer_rejects_non_v1_input() -> None:
    with pytest.raises(AipcsContractError) as raised:
        import_legacy_v1({"manifest_version": 2})
    assert raised.value.code is ErrorCode.MANIFEST_VERSION_UNSUPPORTED


def test_legacy_importer_reports_every_retired_locator() -> None:
    legacy = synthetic_legacy_manifest()
    legacy["dsn"] = "postgresql://private"
    legacy["database_path"] = "/private/database.sqlite"
    result = import_legacy_v1(legacy)
    assert {"dsn", "database_path", "endpoint", "tool_definitions"} <= set(result.discarded_fields)
    serialized = result.manifest.model_dump_json()
    assert "postgresql" not in serialized
    assert "database_path" not in serialized


def test_legacy_importer_rejects_unknown_fields_instead_of_silently_dropping() -> None:
    legacy = synthetic_legacy_manifest()
    legacy["unknown_private_state"] = "must not disappear"
    with pytest.raises(AipcsContractError) as raised:
        import_legacy_v1(legacy)
    envelope = raised.value.envelope().model_dump(mode="json")
    assert raised.value.code is ErrorCode.VALIDATION_FAILED
    assert envelope["error"]["issues"][0]["code"] == "legacy_field_unrecognised"
    assert "must not disappear" not in repr(envelope)


def test_legacy_oversized_ambiguous_facet_returns_bounded_safe_error() -> None:
    legacy = synthetic_legacy_manifest()
    legacy["discovery_facets"] = ["x" * 1_000]
    with pytest.raises(AipcsContractError) as raised:
        import_legacy_v1(legacy)
    envelope = raised.value.envelope().model_dump(mode="json")
    assert envelope["error"]["code"] == "validation_failed"
    assert "x" * 100 not in repr(envelope)
