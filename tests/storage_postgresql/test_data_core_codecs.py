"""SYNTHETIC_FIXTURE. PostgreSQL data-core codec and phase-mapping tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from storage_contracts.parity_fixtures import parity_manifest

from aipcs_mcp.records import RecordContractError, RecordField, compile_record_specification
from aipcs_mcp.storage.errors import StorageBusy, StorageMigrationError, StorageUnavailable
from aipcs_mcp.storage.postgresql import data_store as data_store_module
from aipcs_mcp.storage.postgresql.data_core import cursor, cursor_record, failure, fingerprint
from aipcs_mcp.storage.postgresql.data_store import (
    _data_failure,
    _lock_idempotency,
    _require_domain,
)
from aipcs_mcp.storage.postgresql.topology import _decode_field


def test_canonical_fingerprint_and_query_bound_cursor_are_deterministic() -> None:
    assert fingerprint({"b": 1, "a": ["x"]}) == fingerprint({"a": ["x"], "b": 1})
    value = cursor("query", __import__("uuid").UUID(int=1))
    assert cursor_record(value, "query").int == 1
    with pytest.raises(RecordContractError):
        cursor_record(value, "other-query")


@pytest.mark.parametrize(
    ("state", "expected"), [("08006", StorageUnavailable), ("55P03", StorageBusy)]
)
def test_phase_mapping_is_bounded(state: str, expected: type[Exception]) -> None:
    error = RuntimeError()
    error.sqlstate = state  # type: ignore[attr-defined]
    assert type(failure(error, commit_started=False)) is expected
    assert type(failure(error, commit_started=True)) is StorageMigrationError


def test_topology_decodes_native_postgresql_values_to_public_record_values() -> None:
    uuid_field = RecordField("parent_id", "uuid", False, False, None, None, None)
    date_field = RecordField("when", "datetime", False, False, None, None, None)
    list_field = RecordField("tags", "string_list", False, False, None, None, "membership")
    assert _decode_field(uuid_field, UUID(int=1)) == "00000000-0000-0000-0000-000000000001"
    assert (
        _decode_field(date_field, datetime(2026, 7, 23, 12, tzinfo=UTC)) == "2026-07-23T12:00:00Z"
    )
    assert _decode_field(list_field, ["one", "two"]) == ["one", "two"]


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (None, "internal_error"),
        ("23001", "constraint_violation"),
        ("23503", "constraint_violation"),
        ("55P03", "storage_busy"),
        ("08006", "storage_unavailable"),
    ],
)
def test_data_failure_mapping_never_labels_programming_faults_as_schema_drift(
    state: str | None, expected: str
) -> None:
    error = RuntimeError()
    if state is not None:
        error.sqlstate = state  # type: ignore[attr-defined]
    assert _data_failure(error, False).code == expected


def test_idempotency_lock_is_parameterized_and_scoped_to_service_principal_and_key() -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Connection:
        def execute(self, statement: str, parameters: tuple[object, ...]) -> None:
            calls.append((statement, parameters))

    connection = Connection()
    _lock_idempotency(connection, "svc_00000000000000000000000000000001", "one", "key")
    _lock_idempotency(connection, "svc_00000000000000000000000000000001", "two", "key")
    _lock_idempotency(connection, "svc_00000000000000000000000000000002", "one", "key")
    assert all("pg_advisory_xact_lock" in statement for statement, _params in calls)
    assert all("one" not in statement and "key" not in statement for statement, _params in calls)
    assert calls[0][1] != calls[1][1]
    assert calls[0][1] != calls[2][1]


class _AdmissionResult:
    def __init__(self, names: tuple[str, ...], rows: list[tuple[object, ...]]) -> None:
        self.description = tuple((name,) for name in names)
        self._rows = rows

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class _AdmissionConnection:
    def __init__(self, extra_kind: str | None = None) -> None:
        self.extra_kind = extra_kind
        self.statements: list[str] = []

    def execute(self, statement: str, _parameters: tuple[object, ...]) -> _AdmissionResult:
        self.statements.append(statement)
        specification = compile_record_specification(parity_manifest())
        if "FROM pg_catalog.pg_constraint" not in statement:
            rows = [
                (
                    entity.name,
                    field.name,
                    data_store_module._POSTGRES_TYPES[field.logical_type],
                    field.required,
                    "",
                    False,
                    "C" if field.logical_type == "string" else None,
                )
                for entity in specification.entities
                for field in entity.fields
            ]
            return _AdmissionResult(
                (
                    "relname",
                    "attname",
                    "format_type",
                    "attnotnull",
                    "attidentity",
                    "atthasdef",
                    "collname",
                ),
                rows,
            )
        rows = [
            ("note_pkey", "note", "p", False, False, " ", " ", ["id"], None, []),
            (
                "note_parent_fk",
                "note",
                "f",
                False,
                False,
                "r",
                "r",
                ["parent_id"],
                "note",
                ["id"],
            ),
        ]
        if self.extra_kind is not None:
            rows.append(
                (
                    "hostile_extra",
                    "note",
                    self.extra_kind,
                    False,
                    False,
                    "r" if self.extra_kind == "f" else " ",
                    "r" if self.extra_kind == "f" else " ",
                    [],
                    None,
                    [],
                )
            )
        return _AdmissionResult(
            (
                "name",
                "source_name",
                "kind",
                "deferrable",
                "deferred",
                "update_action",
                "delete_action",
                "source_key",
                "target_name",
                "target_key",
            ),
            rows,
        )


def test_data_admission_excludes_pg18_not_null_constraints_at_query_boundary() -> None:
    connection = _AdmissionConnection()

    _require_domain(
        connection,
        "svc_00000000000000000000000000000001",
        compile_record_specification(parity_manifest()),
    )
    assert "con.contype IN ('c', 'p', 'u', 'f')" in connection.statements[-1]


@pytest.mark.parametrize("kind", ["c", "p", "u", "f"])
def test_data_admission_extra_constraint_kinds_still_fail_closed(kind: str) -> None:
    with pytest.raises(StorageMigrationError):
        _require_domain(
            _AdmissionConnection(kind),
            "svc_00000000000000000000000000000001",
            compile_record_specification(parity_manifest()),
        )
