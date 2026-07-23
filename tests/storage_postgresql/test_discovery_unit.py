"""SYNTHETIC_FIXTURE. PostgreSQL discovery boundary unit proofs."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from fixtures import entity

from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.records import (
    MaintenanceQuery,
    RecordContractError,
    SummaryQuery,
    compile_record_specification,
)
from aipcs_mcp.storage.errors import StorageUnavailable
from aipcs_mcp.storage.postgresql.discovery import (
    _decode_field,
    maintenance_scan,
    service_summary,
)

NAMESPACE = "svc_00000000000000000000000000000001"


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str, _parameters: tuple[object, ...] = ()) -> object:
        self.statements.append(statement)
        raise AssertionError("invalid input must fail before SQL")


def _specification():
    manifest = ManifestV2.model_validate(
        {
            "manifest_version": 2,
            "schema_version": 1,
            "entities": [entity("note", [{"name": "title", "type": "string", "required": True}])],
            "relationships": [],
            "indices": [{"name": "note_owner_idx", "entity": "note", "fields": ["owner_id"]}],
            "query_patterns": [],
            "discovery_facets": [],
            "retrieval_guidance": None,
            "migration_history": [],
        }
    )
    return compile_record_specification(manifest)


@pytest.mark.parametrize(
    ("namespace", "principal"),
    [
        ("public", "principal"),
        ("svc_00000000000000000000000000000000", "principal"),
        (NAMESPACE, ""),
        (NAMESPACE, "bad\x00principal"),
    ],
)
def test_malformed_scope_is_rejected_before_sql(namespace: str, principal: str) -> None:
    connection = RecordingConnection()
    with pytest.raises(RecordContractError):
        service_summary(connection, namespace, principal, _specification(), SummaryQuery())
    assert connection.statements == []


def test_unknown_sample_or_entity_is_rejected_before_sql() -> None:
    connection = RecordingConnection()
    specification = _specification()
    with pytest.raises(RecordContractError):
        service_summary(
            connection,
            NAMESPACE,
            "principal",
            specification,
            SummaryQuery((("missing", 1),)),
        )
    with pytest.raises(RecordContractError):
        maintenance_scan(
            connection,
            NAMESPACE,
            "principal",
            specification,
            MaintenanceQuery(("stale",), entity_name="missing"),
            now=datetime(2026, 7, 23, tzinfo=UTC),
        )
    assert connection.statements == []


def test_native_postgresql_field_values_decode_to_canonical_record_values() -> None:
    manifest = ManifestV2.model_validate(
        {
            "manifest_version": 2,
            "schema_version": 1,
            "entities": [
                entity(
                    "note",
                    [
                        {"name": "flag", "type": "boolean"},
                        {"name": "moment", "type": "datetime"},
                        {"name": "reference", "type": "uuid"},
                        {"name": "tags", "type": "string_list"},
                    ],
                )
            ],
            "relationships": [],
            "indices": [{"name": "note_owner_idx", "entity": "note", "fields": ["owner_id"]}],
            "query_patterns": [],
            "discovery_facets": [],
            "retrieval_guidance": None,
            "migration_history": [],
        }
    )
    specification = compile_record_specification(manifest)
    assert _decode_field(specification.field("note", "flag"), True) is True  # type: ignore[arg-type]
    assert (
        _decode_field(
            specification.field("note", "moment"),  # type: ignore[arg-type]
            datetime(2026, 7, 23, 12, tzinfo=UTC),
        )
        == "2026-07-23T12:00:00.000000Z"
    )
    assert _decode_field(
        specification.field("note", "reference"),
        UUID(int=2),  # type: ignore[arg-type]
    ) == str(UUID(int=2))
    assert _decode_field(specification.field("note", "tags"), ["a", "b"]) == [  # type: ignore[arg-type]
        "a",
        "b",
    ]
    with pytest.raises(StorageUnavailable):
        _decode_field(specification.field("note", "tags"), {"not": "a list"})  # type: ignore[arg-type]
