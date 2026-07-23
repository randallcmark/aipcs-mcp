"""SYNTHETIC_FIXTURE. Mocked PostgreSQL domain-store transaction and inspection tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import pytest
from storage_contracts.parity_fixtures import parity_manifest

from aipcs_mcp.relational import compile_manifest
from aipcs_mcp.storage import DomainSchemaState, MigrationState, ServiceStoreLocator
from aipcs_mcp.storage.errors import StorageBusy, StorageMigrationError, StorageUnavailable
from aipcs_mcp.storage.postgresql import domain_schema as domain_schema_module
from aipcs_mcp.storage.postgresql.connection import PostgreSQLDsn
from aipcs_mcp.storage.postgresql.domain_schema import PostgreSQLDomainSchemaStore, _failure
from aipcs_mcp.storage.postgresql.domain_schema_layout import (
    _primary_key_name,
    build_postgresql_domain_layout,
)
from aipcs_mcp.storage.postgresql.service_store import PostgreSQLServiceStoreCatalog


@dataclass
class _Result:
    rows: list[tuple[object, ...]]

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows


@dataclass
class _State:
    materialised: bool = False
    statements: list[str] = field(default_factory=list)


class _Connection:
    def __init__(self, state: _State) -> None:
        self._state = state
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, statement: str, _parameters: tuple[object, ...] = ()) -> _Result:
        self._state.statements.append(statement)
        if statement.startswith("CREATE TABLE"):
            self._state.materialised = True
        if "SELECT c.relname, c.relkind" in statement and "c.relowner =" not in statement:
            return _Result([("note", "r")] if self._state.materialised else [])
        if "c.relowner =" in statement:
            return _Result(
                [
                    ("note", "r", True, False),
                    (_primary_key_name("note"), "i", True, False),
                    ("note_title_idx", "i", True, False),
                ]
                if self._state.materialised
                else []
            )
        if "FROM pg_catalog.pg_class AS c" in statement and "pg_catalog.pg_attribute" in statement:
            return _Result(
                [
                    ("note", "id", "uuid", True, "", False, None),
                    ("note", "owner_id", "text", True, "", False, "C"),
                    ("note", "created_at", "timestamp with time zone", True, "", False, None),
                    ("note", "updated_at", "timestamp with time zone", True, "", False, None),
                    ("note", "created_via", "text", True, "", False, "C"),
                    ("note", "record_version", "bigint", True, "", False, None),
                    ("note", "title", "text", True, "", False, "C"),
                    ("note", "rank", "bigint", False, "", False, None),
                    ("note", "tags", "jsonb", False, "", False, None),
                    ("note", "parent_id", "uuid", False, "", False, None),
                ]
                if self._state.materialised
                else []
            )
        if "FROM pg_catalog.pg_constraint" in statement:
            return _Result(
                [
                    (
                        "note_parent_fk",
                        "note",
                        "f",
                        False,
                        False,
                        True,
                        "r",
                        "r",
                        ["parent_id"],
                        "svc_00000000000000000000000000000001",
                        "note",
                        ["id"],
                    ),
                    (
                        _primary_key_name("note"),
                        "note",
                        "p",
                        False,
                        False,
                        True,
                        " ",
                        " ",
                        ["id"],
                        None,
                        None,
                        [],
                    ),
                ]
                if self._state.materialised
                else []
            )
        if "FROM pg_catalog.pg_index" in statement:
            return _Result(
                [
                    (
                        "note",
                        _primary_key_name("note"),
                        True,
                        True,
                        False,
                        True,
                        True,
                        True,
                        False,
                        "btree",
                        1,
                        1,
                        ["id"],
                        [0],
                        ["uuid_ops"],
                        [None],
                    ),
                    (
                        "note",
                        "note_title_idx",
                        False,
                        False,
                        False,
                        True,
                        True,
                        True,
                        False,
                        "btree",
                        1,
                        1,
                        ["title"],
                        [0],
                        ["text_ops"],
                        ["C"],
                    ),
                ]
                if self._state.materialised
                else []
            )
        return _Result([])

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class _ExtraConstraintConnection(_Connection):
    def __init__(self, state: _State, kind: str) -> None:
        super().__init__(state)
        self._kind = kind

    def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> _Result:
        result = super().execute(statement, parameters)
        if "FROM pg_catalog.pg_constraint" not in statement:
            return result
        result.rows.append(
            (
                "hostile_extra",
                "note",
                self._kind,
                False,
                False,
                True,
                "r" if self._kind == "f" else " ",
                "r" if self._kind == "f" else " ",
                [],
                None,
                None,
                [],
            )
        )
        return result


def _locator() -> ServiceStoreLocator:
    return ServiceStoreLocator.for_service("postgresql", UUID(int=1))


def _ready_catalog(
    _self: PostgreSQLServiceStoreCatalog, _locator: ServiceStoreLocator
) -> MigrationState:
    return MigrationState("service_store", 1, 1, "ready")


def test_inspect_is_read_only_and_never_emits_domain_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _State()
    monkeypatch.setattr(PostgreSQLServiceStoreCatalog, "inspect_migration", _ready_catalog)
    store = PostgreSQLDomainSchemaStore(
        PostgreSQLDsn("postgresql://private.invalid/aipcs"),
        connection_factory=lambda _dsn, _policy: _Connection(state),
    )

    assert store.inspect(_locator(), compile_manifest(parity_manifest())) == DomainSchemaState(
        "unmaterialised"
    )
    assert state.statements[0] == "BEGIN READ ONLY"
    assert not any(
        statement.startswith(("CREATE", "ALTER", "DROP")) for statement in state.statements
    )
    assert not any("current_schema" in statement for statement in state.statements)


def test_materialise_locks_transaction_creates_exact_ddl_then_reinspects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _State()
    monkeypatch.setattr(PostgreSQLServiceStoreCatalog, "inspect_migration", _ready_catalog)
    store = PostgreSQLDomainSchemaStore(
        PostgreSQLDsn("postgresql://private.invalid/aipcs"),
        connection_factory=lambda _dsn, _policy: _Connection(state),
    )

    assert store.materialise(_locator(), compile_manifest(parity_manifest())) == DomainSchemaState(
        "ready"
    )
    assert "BEGIN" in state.statements
    assert any("pg_advisory_xact_lock" in statement for statement in state.statements)
    assert any(
        statement.startswith('CREATE TABLE "svc_00000000000000000000000000000001"."note"')
        for statement in state.statements
    )
    assert any(
        statement.startswith(
            'CREATE INDEX "note_title_idx" ON "svc_00000000000000000000000000000001"."note"'
        )
        for statement in state.statements
    )
    assert any("con.contype IN ('c', 'p', 'u', 'f')" in statement for statement in state.statements)


@pytest.mark.parametrize("kind", ["c", "p", "u", "f"])
def test_extra_domain_constraint_kinds_still_fail_closed(kind: str) -> None:
    state = _State(materialised=True)
    layout = build_postgresql_domain_layout(
        compile_manifest(parity_manifest()),
        _locator().namespace,
    )

    assert not domain_schema_module._constraints_match(
        _ExtraConstraintConnection(state, kind),
        _locator().namespace,
        layout,
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [("08006", StorageUnavailable), ("55P03", StorageBusy), ("57014", StorageBusy)],
)
def test_phase_aware_sqlstate_mapping_before_commit(state: str, expected: type[Exception]) -> None:
    error = RuntimeError()
    error.sqlstate = state  # type: ignore[attr-defined]

    assert type(_failure(error, commit_started=False)) is expected


def test_commit_started_failure_never_guesses_a_durable_result() -> None:
    error = RuntimeError()
    error.sqlstate = "08006"  # type: ignore[attr-defined]

    assert type(_failure(error, commit_started=True)) is StorageMigrationError
