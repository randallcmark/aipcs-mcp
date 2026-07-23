"""SYNTHETIC_FIXTURE. Synthetic fixture provenance: no-Docker PostgreSQL foundation tests."""

from __future__ import annotations

from uuid import UUID

import pytest

from aipcs_mcp.storage import MigrationState, ServiceStoreLocator, StorageContractError
from aipcs_mcp.storage.postgresql import service_store as service_store_module
from aipcs_mcp.storage.postgresql.connection import PostgreSQLDsn
from aipcs_mcp.storage.postgresql.service_store import PostgreSQLServiceStoreCatalog
from aipcs_mcp.storage.postgresql.service_store_migrations import (
    CHECK_EXPRESSIONS,
    CHECKSUM,
    CONSTRAINT_KEYS,
    CONSTRAINTS,
    INDEXES,
    SEQUENCES,
    TABLE_COLUMNS,
    TARGET_REVISION,
    ddl,
)


class _Result:
    def __init__(self, one=None, many=()):  # type: ignore[no-untyped-def]
        self._one = one
        self._many = many

    def fetchone(self):  # type: ignore[no-untyped-def]
        return self._one

    def fetchall(self):  # type: ignore[no-untyped-def]
        return self._many


class _MissingSchemaConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.rolled_back = False
        self.closed = False

    def execute(self, statement: str, _parameters=()):  # type: ignore[no-untyped-def]
        self.statements.append(statement)
        if statement == "BEGIN READ ONLY":
            return _Result()
        if "server_version_num" in statement:
            return _Result((160_000,))
        if "FROM pg_catalog.pg_namespace" in statement:
            return _Result(None)
        raise AssertionError("unexpected inspection query")

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class _ConstraintConnection:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows
        self.statements: list[str] = []

    def execute(self, statement: str, _parameters=()):  # type: ignore[no-untyped-def]
        self.statements.append(statement)
        return _Result(many=self.rows)


def _catalog(connection: object) -> PostgreSQLServiceStoreCatalog:
    return PostgreSQLServiceStoreCatalog(
        PostgreSQLDsn("postgresql://synthetic:secret@127.0.0.1/synthetic"),
        connection_factory=lambda _dsn, _policy: connection,
    )


def test_catalog_allocates_exact_postgresql_namespace_without_connection() -> None:
    catalog = _catalog(object())
    locator = catalog.allocate(UUID(int=1))

    assert locator == ServiceStoreLocator("postgresql", "svc_" + "0" * 31 + "1")
    assert catalog.info().backend == "postgresql"
    assert catalog.info().supported_components == frozenset({"service_store"})
    assert repr(catalog) == "PostgreSQLServiceStoreCatalog(<redacted>)"


def test_inspection_of_a_missing_schema_performs_no_ddl() -> None:
    connection = _MissingSchemaConnection()
    catalog = _catalog(connection)

    assert catalog.inspect_migration(catalog.allocate(UUID(int=1))) == MigrationState(
        "service_store", 0, TARGET_REVISION, "uninitialised"
    )
    assert connection.rolled_back and connection.closed
    assert not any("CREATE " in statement.upper() for statement in connection.statements)


def test_foreign_locator_fails_without_constructing_a_connection() -> None:
    catalog = _catalog(object())
    foreign = ServiceStoreLocator("sqlite", "svc_" + "0" * 31 + "1")

    for operation in (catalog.inspect_migration, catalog.migrate):
        with pytest.raises(StorageContractError):
            operation(foreign)


def test_r1_descriptor_contains_current_complete_reserved_foundation() -> None:
    statements = ddl("svc_" + "0" * 31 + "1")

    assert len(CHECKSUM) == 64
    assert len(TABLE_COLUMNS) == 6
    assert len(CONSTRAINTS) >= 39
    assert len(INDEXES) >= 13
    assert {"__aipcs_record_history_event_sequence_seq"} == SEQUENCES
    assert all(
        "CREATE TABLE" in statement
        or "CREATE INDEX" in statement
        or "CREATE UNIQUE INDEX" in statement
        for statement in statements
    )
    assert all(
        "CREATE DATABASE" not in statement and "CREATE ROLE" not in statement
        for statement in statements
    )


def _expected_constraint_rows() -> list[tuple[object, ...]]:
    rows = [
        ("__aipcs_service_store_meta", name, "c", [], None, [], " ", " ", expression)
        for name, expression in CHECK_EXPRESSIONS.items()
    ]
    for name, (table, kind, keys, referenced_table, referenced_keys) in CONSTRAINT_KEYS.items():
        update_action = "r" if kind == "f" else " "
        delete_action = "r" if kind == "f" else " "
        rows.append(
            (
                table,
                name,
                kind,
                list(keys),
                referenced_table,
                list(referenced_keys),
                update_action,
                delete_action,
                None,
            )
        )
    return rows


def test_pg18_not_null_constraints_are_excluded_at_the_catalog_query() -> None:
    connection = _ConstraintConnection(_expected_constraint_rows())

    assert service_store_module._constraints_match(connection, 1)
    assert len(connection.statements) == 1
    assert "con.contype IN ('c', 'p', 'u', 'f')" in connection.statements[0]


@pytest.mark.parametrize("kind", ["c", "p", "u", "f"])
def test_extra_contract_constraint_kinds_still_fail_closed(kind: str) -> None:
    rows = _expected_constraint_rows()
    rows.append(
        (
            "__aipcs_service_store_meta",
            "__aipcs_hostile_extra",
            kind,
            [],
            None,
            [],
            "r" if kind == "f" else " ",
            "r" if kind == "f" else " ",
            "true" if kind == "c" else None,
        )
    )

    assert not service_store_module._constraints_match(_ConstraintConnection(rows), 1)
