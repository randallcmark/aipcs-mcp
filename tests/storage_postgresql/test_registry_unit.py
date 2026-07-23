"""SYNTHETIC_FIXTURE. PostgreSQL registry R1 unit and boundary proof."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from aipcs_mcp.storage.contracts import MigrationState
from aipcs_mcp.storage.errors import StorageBusy, StorageMigrationError, StorageUnavailable
from aipcs_mcp.storage.postgresql import registry_inspection as registry_inspection_module
from aipcs_mcp.storage.postgresql.connection import (
    PostgreSQLConnectionPolicy,
    PostgreSQLDsn,
)
from aipcs_mcp.storage.postgresql.registry import PostgreSQLRegistryAdapter
from aipcs_mcp.storage.postgresql.registry_inspection import (
    canonical_row,
    inspect_registry,
)
from aipcs_mcp.storage.postgresql.registry_migrations import (
    ADAPTER_ID,
    CHECK_TOKENS,
    CHECKSUM,
    CONSTRAINT_KEYS,
    CONSTRAINT_TYPES,
    DDL,
    INDEX_COLLATIONS,
    INDEX_COLUMNS,
    INDEX_OPCLASSES,
    INDEX_OPTIONS,
    INDEX_PREDICATE_TOKENS,
    INDEX_PREDICATES,
    INDEX_SIGNATURES,
    MIGRATION_ID,
    SCHEMA,
    SEQUENCES,
    TABLE_COLUMNS,
    TARGET_REVISION,
)
from aipcs_mcp.storage.postgresql.registry_uow import PostgreSQLRegistryUnitOfWork


class Cursor:
    def __init__(
        self,
        rows: list[tuple[object, ...]] | None = None,
        *,
        description: tuple[SimpleNamespace, ...] | None = None,
        rowcount: int = -1,
    ) -> None:
        self._rows = [] if rows is None else list(rows)
        self.description = description
        self.rowcount = rowcount

    def fetchone(self) -> tuple[object, ...] | None:
        return None if not self._rows else self._rows.pop(0)

    def fetchall(self) -> list[tuple[object, ...]]:
        rows = list(self._rows)
        self._rows.clear()
        return rows


class ReadyCatalogConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, _params: tuple[object, ...] = ()) -> Cursor:
        self.statements.append(sql)
        if "server_version_num" in sql:
            return Cursor([("160000",)])
        if "FROM pg_catalog.pg_namespace" in sql:
            return Cursor([(42, True, True)])
        if "c.relkind IN ('r','p','i','S','v','m','f')" in sql:
            objects = {
                **{name: "r" for name in TABLE_COLUMNS},
                **{name: "i" for name in INDEX_SIGNATURES},
                **{name: "S" for name in SEQUENCES},
            }
            return Cursor([(name, kind, True) for name, kind in sorted(objects.items())])
        if "c.relkind IN ('r','p','S')" in sql:
            return Cursor(
                [(name, True, False) for name in sorted(set(TABLE_COLUMNS) | SEQUENCES)]
            )
        if "FROM pg_catalog.pg_attribute" in sql:
            rows = [
                (table, *column, "C" if column[1] == "text" else "")
                for table in sorted(TABLE_COLUMNS)
                for column in TABLE_COLUMNS[table]
            ]
            return Cursor(rows)
        if "FROM pg_catalog.pg_constraint" in sql:
            rows = []
            for table in sorted(CONSTRAINT_TYPES):
                for name, kind in sorted(CONSTRAINT_TYPES[table].items()):
                    expected = CONSTRAINT_KEYS.get(name)
                    if expected is None:
                        keys: tuple[str, ...] = ()
                        reference = None
                        reference_keys: tuple[str, ...] = ()
                        reference_schema = None
                        expression = " ".join(CHECK_TOKENS[name])
                    else:
                        _, _, keys, reference, reference_keys = expected
                        reference_schema = SCHEMA if kind == "f" else None
                        expression = None
                    rows.append(
                        (
                            table,
                            name,
                            kind,
                            list(keys),
                            reference_schema,
                            reference,
                            list(reference_keys),
                            "r" if kind == "f" else "a",
                            "r" if kind == "f" else "a",
                            "s",
                            False,
                            False,
                            True,
                            expression,
                        )
                    )
            return Cursor(rows)
        if "FROM pg_catalog.pg_index" in sql:
            rows = [
                (
                    table,
                    name,
                    unique,
                    primary,
                    partial,
                    list(INDEX_COLUMNS[name]),
                    list(INDEX_OPTIONS[name]),
                    None
                    if name not in INDEX_PREDICATE_TOKENS
                    else INDEX_PREDICATES[name],
                    "btree",
                    list(INDEX_OPCLASSES[name]),
                    list(INDEX_COLLATIONS[name]),
                )
                for name, (table, unique, primary, partial) in sorted(
                    INDEX_SIGNATURES.items()
                )
            ]
            return Cursor(rows)
        if '"applied_revision" FROM "aipcs_registry"."aipcs_registry_meta"' in sql:
            return Cursor([(TARGET_REVISION,)])
        if 'FROM "aipcs_registry"."aipcs_registry_meta"' in sql:
            return Cursor([(1, ADAPTER_ID, "registry", TARGET_REVISION, False)])
        if 'FROM "aipcs_registry"."aipcs_registry_migration"' in sql:
            return Cursor(
                [
                    (
                        "registry",
                        TARGET_REVISION,
                        MIGRATION_ID,
                        CHECKSUM,
                        datetime(2026, 1, 1, tzinfo=UTC),
                    )
                ]
            )
        if any(
            f'FROM "aipcs_registry"."{table}"' in sql
            for table in (
                "aipcs_registry_service",
                "aipcs_registry_mutation",
                "aipcs_registry_audit",
            )
        ):
            return Cursor([])
        raise AssertionError(sql)


class SqlStateError(Exception):
    def __init__(self, state: str) -> None:
        self.sqlstate = state


class BoundaryConnection:
    def __init__(
        self,
        *,
        execute_error: BaseException | None = None,
        fail_when: str | None = None,
        commit_error: BaseException | None = None,
    ) -> None:
        self.execute_error = execute_error
        self.fail_when = fail_when
        self.commit_error = commit_error
        self.statements: list[str] = []
        self.closed = False

    def execute(self, sql: str, _params: tuple[object, ...] = ()) -> Cursor:
        self.statements.append(sql)
        if self.execute_error is not None and (
            (self.fail_when is None and not sql.startswith("BEGIN"))
            or (self.fail_when is not None and self.fail_when in sql)
        ):
            raise self.execute_error
        return Cursor([])

    def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _adapter() -> PostgreSQLRegistryAdapter:
    return PostgreSQLRegistryAdapter(
        PostgreSQLDsn("postgresql://synthetic.invalid/test"),
        PostgreSQLConnectionPolicy(SCHEMA, 5, 1_000, 5_000),
    )


def test_r1_uses_native_types_fixed_schema_and_no_privileged_database_actions() -> None:
    joined = "\n".join(DDL)

    assert DDL[0] == 'CREATE SCHEMA "aipcs_registry" AUTHORIZATION CURRENT_USER'
    assert DDL[1] == 'REVOKE ALL PRIVILEGES ON SCHEMA "aipcs_registry" FROM PUBLIC'
    assert " uuid " in joined
    assert " jsonb" in joined
    assert "bigint" in joined
    assert "boolean" in joined
    assert "timestamp(6) with time zone" in joined
    assert "CREATE DATABASE" not in joined
    assert "CREATE ROLE" not in joined
    assert "CREATE EXTENSION" not in joined
    assert "GRANT " not in joined


def test_structured_ready_inspection_is_read_only_and_schema_qualified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = ReadyCatalogConnection()
    fake_digest = hashlib.sha256(
        "\n".join(
            f"{name}\0{' '.join(tokens)}"
            for name, tokens in sorted(CHECK_TOKENS.items())
        ).encode()
    ).hexdigest()
    monkeypatch.setattr(
        registry_inspection_module,
        "CHECK_EXPRESSION_DIGEST",
        fake_digest,
    )

    assert inspect_registry(connection) == MigrationState("registry", 1, 1, "ready")
    assert all(
        not statement.lstrip().upper().startswith(
            ("CREATE ", "ALTER ", "DROP ", "INSERT ", "UPDATE ", "DELETE ", "GRANT ", "REVOKE ")
        )
        for statement in connection.statements
    )
    assert any("pg_catalog.pg_attribute" in statement for statement in connection.statements)
    assert any("pg_catalog.pg_constraint" in statement for statement in connection.statements)
    assert any("pg_catalog.pg_index" in statement for statement in connection.statements)


def test_existing_empty_registry_schema_is_an_incompatible_collision() -> None:
    connection = ReadyCatalogConnection()
    original = connection.execute

    def execute(sql: str, params: tuple[object, ...] = ()) -> Cursor:
        if "c.relkind IN ('r','p','i','S','v','m','f')" in sql:
            return Cursor([])
        return original(sql, params)

    connection.execute = execute  # type: ignore[method-assign]

    assert inspect_registry(connection) == MigrationState("registry", 0, 1, "incompatible")


@pytest.mark.parametrize("major", ["150000", "190000", "not-a-version"])
def test_inspection_rejects_unsupported_or_malformed_server_versions(major: str) -> None:
    connection = ReadyCatalogConnection()
    original = connection.execute

    def execute(sql: str, params: tuple[object, ...] = ()) -> Cursor:
        if "server_version_num" in sql:
            return Cursor([(major,)])
        return original(sql, params)

    connection.execute = execute  # type: ignore[method-assign]

    with pytest.raises(StorageUnavailable):
        inspect_registry(connection)


def test_native_driver_rows_are_detached_into_canonical_codec_values() -> None:
    manifest = {"schema_version": 1, "entities": []}
    row = (
        UUID("00000000-0000-0000-0000-000000000001"),
        datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC),
        manifest,
    )
    cursor = Cursor(
        description=tuple(
            SimpleNamespace(name=name)
            for name in ("service_id", "created_at", "manifest_json")
        )
    )

    detached = canonical_row(cursor, row)
    manifest["schema_version"] = 2

    assert detached == {
        "service_id": "00000000-0000-0000-0000-000000000001",
        "created_at": "2026-01-02T03:04:05.000006Z",
        "manifest_json": '{"entities":[],"schema_version":1}',
    }


@pytest.mark.parametrize(
    ("state", "expected"),
    [("55P03", StorageBusy), ("57014", StorageBusy), ("08006", StorageUnavailable)],
)
def test_read_phase_sqlstates_remain_bounded_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    expected: type[Exception],
) -> None:
    connection = BoundaryConnection(execute_error=SqlStateError(state))
    monkeypatch.setattr(
        "aipcs_mcp.storage.postgresql.registry.connect_postgresql",
        lambda _dsn, _policy: connection,
    )

    with pytest.raises(expected) as captured:
        _adapter().inspect_migration()

    assert captured.value.__cause__ is None
    assert connection.closed


def test_transactional_ddl_cancellation_is_busy_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = BoundaryConnection(
        execute_error=SqlStateError("57014"),
        fail_when="CREATE SCHEMA",
    )
    monkeypatch.setattr(
        "aipcs_mcp.storage.postgresql.registry.connect_postgresql",
        lambda _dsn, _policy: connection,
    )
    monkeypatch.setattr(
        "aipcs_mcp.storage.postgresql.registry.inspect_registry",
        lambda _connection: MigrationState("registry", 0, 1, "uninitialised"),
    )

    with pytest.raises(StorageBusy):
        _adapter().migrate()

    assert connection.closed


def test_commit_started_failure_is_bounded_as_uncertain_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = BoundaryConnection(commit_error=SqlStateError("57014"))
    monkeypatch.setattr(
        "aipcs_mcp.storage.postgresql.registry.connect_postgresql",
        lambda _dsn, _policy: connection,
    )
    monkeypatch.setattr(
        "aipcs_mcp.storage.postgresql.registry.inspect_registry",
        lambda _connection: MigrationState("registry", 0, 1, "uninitialised"),
    )

    with pytest.raises(StorageMigrationError):
        _adapter().migrate()

    assert connection.closed


def test_uow_uses_transaction_scoped_advisory_locks_and_no_internal_retry() -> None:
    connection = BoundaryConnection(execute_error=SqlStateError("55P03"))
    uow = PostgreSQLRegistryUnitOfWork(connection)

    with pytest.raises(StorageBusy):
        uow.resolve_non_lifecycle("seed", "principal", "key", "0" * 64)

    lock_statements = [
        statement for statement in connection.statements if "pg_advisory_xact_lock" in statement
    ]
    assert len(lock_statements) == 1
    assert len(connection.statements) == 2


def test_uow_commit_started_connection_loss_is_uncertain() -> None:
    connection = BoundaryConnection(commit_error=SqlStateError("08006"))
    uow = PostgreSQLRegistryUnitOfWork(connection)

    with pytest.raises(StorageMigrationError):
        uow.commit()
