"""SYNTHETIC_FIXTURE. PostgreSQL registry R1 unit and boundary proof."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from aipcs_mcp.application.models import Service
from aipcs_mcp.application.operational_lifecycle import OperationalLifecyclePhase
from aipcs_mcp.application.registry_authority import (
    ExportCommand,
    ImportCommand,
    ImportIdentity,
    PreparedRegistryClaim,
    PurgeAuthority,
    PurgeAuthorityKind,
    PurgeCommand,
    ReceiptKind,
    ReceiptVerification,
    RegistryUnsupportedTransition,
    StorageBackend,
    TransferReceipt,
    prepare_registry_intent,
)
from aipcs_mcp.storage.codecs import encode_time
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
    CHECK_EXPRESSIONS,
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
    R1_CHECKSUM,
    R1_MIGRATION_ID,
    R2_DDL,
    SCHEMA,
    SEQUENCES,
    TABLE_COLUMNS,
    TARGET_REVISION,
)
from aipcs_mcp.storage.postgresql.registry_uow import PostgreSQLRegistryUnitOfWork
from aipcs_mcp.storage.registry_authority_codecs import encode_registry_authority_intent


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
                        expression = CHECK_EXPRESSIONS[name]
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
            rows = [
                (
                    "registry",
                    1,
                    R1_MIGRATION_ID,
                    R1_CHECKSUM,
                    datetime(2026, 1, 1, tzinfo=UTC),
                )
            ]
            if TARGET_REVISION > 1:
                rows.append(
                    (
                        "registry",
                        TARGET_REVISION,
                        MIGRATION_ID,
                        CHECKSUM,
                        datetime(2026, 1, 1, tzinfo=UTC),
                    )
                )
            return Cursor(rows)
        if any(
            f'FROM "aipcs_registry"."{table}"' in sql
            for table in (
                "aipcs_registry_service",
                "aipcs_registry_claim",
                "aipcs_registry_identity",
                "aipcs_registry_receipt",
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


def test_r2_uses_native_types_fixed_schema_and_no_privileged_database_actions() -> None:
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


def test_r2_uses_claim_identity_and_receipt_ledgers_without_live_service_foreign_keys() -> None:
    joined = "\n".join(R2_DDL)

    assert 'CREATE TABLE "aipcs_registry"."aipcs_registry_claim"' in joined
    assert 'CREATE TABLE "aipcs_registry"."aipcs_registry_identity"' in joined
    assert 'CREATE TABLE "aipcs_registry"."aipcs_registry_receipt"' in joined
    assert 'DROP CONSTRAINT "aipcs_registry_audit_service_fkey"' in joined
    assert 'DROP TABLE "aipcs_registry"."aipcs_registry_mutation"' in joined
    assert '"phase" IN (\'prepared\', \'completed\', \'recovery_required\', \'tombstoned\')' in joined
    assert "'export', 'import', 'purge'" in joined
    assert "'import_prepared', 'tombstoned'" in joined
    assert 'aipcs_registry_claim_active_lifecycle' in joined


def test_structured_ready_inspection_is_read_only_and_schema_qualified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = ReadyCatalogConnection()
    monkeypatch.setattr(
        registry_inspection_module,
        "CHECK_EXPRESSION_DIGEST",
        _fixture_check_expression_digest(),
    )

    assert inspect_registry(connection) == MigrationState(
        "registry", TARGET_REVISION, TARGET_REVISION, "ready"
    )
    assert all(
        not statement.lstrip().upper().startswith(
            ("CREATE ", "ALTER ", "DROP ", "INSERT ", "UPDATE ", "DELETE ", "GRANT ", "REVOKE ")
        )
        for statement in connection.statements
    )
    assert any("pg_catalog.pg_attribute" in statement for statement in connection.statements)
    assert any("pg_catalog.pg_constraint" in statement for statement in connection.statements)
    assert any("pg_catalog.pg_index" in statement for statement in connection.statements)


def test_check_expression_rewrite_with_all_expected_tokens_is_incompatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = ReadyCatalogConnection()
    monkeypatch.setattr(
        registry_inspection_module,
        "CHECK_EXPRESSION_DIGEST",
        _fixture_check_expression_digest(),
    )
    original = connection.execute

    def execute(sql: str, params: tuple[object, ...] = ()) -> Cursor:
        cursor = original(sql, params)
        if "FROM pg_catalog.pg_constraint" not in sql:
            return cursor
        rows = cursor.fetchall()
        changed: list[tuple[object, ...]] = []
        for row in rows:
            if row[1] == "aipcs_registry_claim_kind_check":
                # Every expected token remains, but the expression admits a
                # different value.  Token-only inspection must reject it.
                changed.append((*row[:-1], f"{row[-1]} OR 'surprise'"))
            else:
                changed.append(row)
        return Cursor(changed)

    connection.execute = execute  # type: ignore[method-assign]

    assert inspect_registry(connection) == MigrationState(
        "registry", TARGET_REVISION, TARGET_REVISION, "incompatible"
    )


def _fixture_check_expression_digest() -> str:
    return hashlib.sha256(
        "\n".join(
            f"{name}\0{expression}"
            for name, expression in sorted(CHECK_EXPRESSIONS.items())
        ).encode()
    ).hexdigest()


def test_existing_empty_registry_schema_is_an_incompatible_collision() -> None:
    connection = ReadyCatalogConnection()
    original = connection.execute

    def execute(sql: str, params: tuple[object, ...] = ()) -> Cursor:
        if "c.relkind IN ('r','p','i','S','v','m','f')" in sql:
            return Cursor([])
        return original(sql, params)

    connection.execute = execute  # type: ignore[method-assign]

    assert inspect_registry(connection) == MigrationState(
        "registry", 0, TARGET_REVISION, "incompatible"
    )


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


def test_r1_upgrade_runs_only_r2_ddl_then_records_revision_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = BoundaryConnection()
    states = iter(
        [
            MigrationState("registry", 1, TARGET_REVISION, "outdated"),
            MigrationState("registry", TARGET_REVISION, TARGET_REVISION, "ready"),
        ]
    )
    monkeypatch.setattr(
        "aipcs_mcp.storage.postgresql.registry.connect_postgresql",
        lambda _dsn, _policy: connection,
    )
    monkeypatch.setattr(
        "aipcs_mcp.storage.postgresql.registry.inspect_registry",
        lambda _connection: next(states),
    )

    assert _adapter().migrate() == MigrationState(
        "registry", TARGET_REVISION, TARGET_REVISION, "ready"
    )
    for statement in R2_DDL:
        assert statement in connection.statements
    assert any(
        statement.startswith('UPDATE "aipcs_registry"."aipcs_registry_meta"')
        for statement in connection.statements
    )
    assert 'CREATE SCHEMA "aipcs_registry" AUTHORIZATION CURRENT_USER' not in connection.statements


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


def test_uow_prepares_an_invisible_import_identity_in_shared_claim_ledger() -> None:
    connection = BoundaryConnection()
    uow = PostgreSQLRegistryUnitOfWork(connection)
    service_id = UUID("11111111-1111-1111-1111-111111111111")
    command = ImportCommand(
        "principal", "test", ImportIdentity("principal", service_id, "imported", f"svc_{service_id.hex}"),
        StorageBackend.POSTGRESQL, "0" * 64, "import-claim",
    )

    result = uow.resolve_or_prepare(command)

    assert type(result) is PreparedRegistryClaim
    joined = "\n".join(connection.statements)
    assert 'INSERT INTO "aipcs_registry"."aipcs_registry_claim"' in joined
    assert 'INSERT INTO "aipcs_registry"."aipcs_registry_identity"' in joined
    assert "'import_prepared'" in joined
    assert "pg_advisory_xact_lock" in joined


def test_uow_rejects_an_import_for_another_selected_backend() -> None:
    connection = BoundaryConnection()
    uow = PostgreSQLRegistryUnitOfWork(connection)
    service_id = UUID("12121212-1212-1212-1212-121212121212")

    result = uow.resolve_or_prepare(
        ImportCommand(
            "principal",
            "test",
            ImportIdentity("principal", service_id, "imported", f"svc_{service_id.hex}"),
            StorageBackend.SQLITE,
            "0" * 64,
            "wrong-backend",
        )
    )

    assert type(result) is RegistryUnsupportedTransition
    assert not any(
        statement.startswith('INSERT INTO "aipcs_registry"."aipcs_registry_claim"')
        for statement in connection.statements
    )


def test_uow_rejects_an_export_receipt_for_another_selected_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_id = UUID("13131313-1313-1313-1313-131313131313")
    at = datetime(2026, 7, 23, tzinfo=UTC)
    prepared = prepare_registry_intent(
        ExportCommand("principal", "test", service_id, 1, "export-backend")
    )
    service = Service(
        service_id, "principal", "notes", "project", "portable", at, at, at
    )
    receipt = TransferReceipt(
        UUID("14141414-1414-1414-1414-141414141414"),
        ReceiptKind.EXPORT,
        "principal",
        service_id,
        "0" * 64,
        1,
        StorageBackend.SQLITE,
        1,
        "active",
        ReceiptVerification.VERIFIED,
        at,
    )
    marker = object()
    monkeypatch.setattr(
        PostgreSQLRegistryUnitOfWork,
        "_authority_prepared_or_terminal",
        lambda _self, _prepared: PreparedRegistryClaim(prepared),
    )
    monkeypatch.setattr(
        PostgreSQLRegistryUnitOfWork,
        "_service",
        lambda _self, _principal, _service_id: service,
    )
    monkeypatch.setattr(
        PostgreSQLRegistryUnitOfWork,
        "_mark_authority_recovery",
        lambda _self, _prepared, _at: marker,
    )

    result = PostgreSQLRegistryUnitOfWork(BoundaryConnection()).complete_export(
        prepared, receipt
    )

    assert result is marker


def test_authority_terminal_updates_store_the_terminal_intent_phase() -> None:
    class CompletionConnection(BoundaryConnection):
        def __init__(self) -> None:
            super().__init__()
            self.parameters: list[tuple[object, ...]] = []

        def execute(self, sql: str, params: tuple[object, ...] = ()) -> Cursor:
            self.statements.append(sql)
            self.parameters.append(params)
            return Cursor(rowcount=1 if 'UPDATE "aipcs_registry"."aipcs_registry_claim"' in sql else -1)

    service_id = UUID("22222222-2222-2222-2222-222222222222")
    at = datetime(2026, 7, 23, tzinfo=UTC)
    prepared = prepare_registry_intent(
        ExportCommand("principal", "test", service_id, 7, "export-claim")
    )
    receipt = TransferReceipt(
        UUID("33333333-3333-3333-3333-333333333333"),
        ReceiptKind.EXPORT,
        "principal",
        service_id,
        "a" * 64,
        1,
        StorageBackend.POSTGRESQL,
        7,
        "suspended",
        ReceiptVerification.VERIFIED,
        at,
    )
    connection = CompletionConnection()
    uow = PostgreSQLRegistryUnitOfWork(connection)
    uow._write()

    completed = uow._complete_authority(prepared, receipt, at)

    terminal_json = encode_registry_authority_intent(
        prepared.with_phase(OperationalLifecyclePhase.COMPLETED)
    )
    update_index = next(
        index
        for index, statement in enumerate(connection.statements)
        if 'UPDATE "aipcs_registry"."aipcs_registry_claim"' in statement
    )
    assert connection.parameters[update_index][0] == terminal_json
    assert completed.intent.phase is OperationalLifecyclePhase.COMPLETED

    recovery_prepared = prepare_registry_intent(
        ExportCommand("principal", "test", service_id, 7, "export-recovery")
    )
    recovered = uow._mark_authority_recovery(recovery_prepared, at)
    recovery_json = encode_registry_authority_intent(
        recovery_prepared.with_phase(OperationalLifecyclePhase.RECOVERY_REQUIRED)
    )
    recovery_index = next(
        index
        for index, statement in enumerate(connection.statements)
        if '"phase"=\'recovery_required\'' in statement
    )
    assert connection.parameters[recovery_index][0] == recovery_json
    assert recovered.intent.phase is OperationalLifecyclePhase.RECOVERY_REQUIRED


def test_import_receipt_inspection_binds_claim_intent_and_registered_service() -> None:
    service_id = UUID("44444444-4444-4444-4444-444444444444")
    receipt_id = UUID("55555555-5555-5555-5555-555555555555")
    at = datetime(2026, 7, 23, tzinfo=UTC)
    service = Service(
        service_id,
        "principal",
        "imported",
        "project",
        "portable",
        at,
        at,
        at,
        service_revision=7,
    )
    prepared = prepare_registry_intent(
        ImportCommand(
            "principal",
            "test",
            ImportIdentity("principal", service_id, "imported", f"svc_{service_id.hex}"),
            StorageBackend.POSTGRESQL,
            "b" * 64,
            "import-claim",
        )
    )
    terminal = prepared.with_phase(OperationalLifecyclePhase.COMPLETED)
    claim_row = {
        "principal_id": "principal",
        "idempotency_key": "import-claim",
        "service_id": str(service_id),
        "operation_kind": "import",
        "phase": "completed",
    }
    claim = {
        "intent": json.loads(encode_registry_authority_intent(terminal)),
        "value": service,
    }
    receipt_row = {
        "receipt_id": str(receipt_id),
        "principal_id": "principal",
        "idempotency_key": "import-claim",
        "service_id": str(service_id),
        "bundle_root_sha256": "b" * 64,
        "receipt_kind": "import",
        "export_format_version": 1,
        "storage_backend": "postgresql",
        "service_revision": 7,
        "operational_status": "active",
        "verification": "verified",
        "created_at": encode_time(at),
    }

    assert registry_inspection_module._valid_receipt_row(
        receipt_row,
        {("principal", "import-claim"): (claim_row, claim)},
        {str(service_id): service},
    )
    assert not registry_inspection_module._valid_receipt_row(
        {**receipt_row, "bundle_root_sha256": "c" * 64},
        {("principal", "import-claim"): (claim_row, claim)},
        {str(service_id): service},
    )


def test_import_publication_uses_destination_receipt_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PublishConnection(BoundaryConnection):
        def execute(self, sql: str, _params: tuple[object, ...] = ()) -> Cursor:
            self.statements.append(sql)
            if (
                'SELECT * FROM "aipcs_registry"."aipcs_registry_identity"' in sql
            ):
                return Cursor(
                    [
                        {
                            "service_id": str(service_id),
                            "principal_id": "principal",
                            "domain_name": "imported",
                            "storage_backend": "postgresql",
                            "storage_namespace": f"svc_{service_id.hex}",
                            "identity_state": "import_prepared",
                            "claim_idempotency_key": "import-time",
                            "lifecycle_at": encode_time(receipt_at),
                        }
                    ]
                )
            if (
                sql.startswith('INSERT INTO "aipcs_registry"."aipcs_registry_service"')
                or sql.startswith('UPDATE "aipcs_registry"."aipcs_registry_identity"')
            ):
                return Cursor(rowcount=1)
            return Cursor()

    service_id = UUID("56565656-5656-5656-5656-565656565656")
    source_at = datetime(2020, 1, 1, tzinfo=UTC)
    receipt_at = datetime(2026, 7, 23, tzinfo=UTC)
    prepared = prepare_registry_intent(
        ImportCommand(
            "principal",
            "test",
            ImportIdentity("principal", service_id, "imported", f"svc_{service_id.hex}"),
            StorageBackend.POSTGRESQL,
            "d" * 64,
            "import-time",
        )
    )
    service = Service(
        service_id,
        "principal",
        "imported",
        "project",
        "portable",
        source_at,
        source_at,
        source_at,
    )
    receipt = TransferReceipt(
        UUID("57575757-5757-5757-5757-575757575757"),
        ReceiptKind.IMPORT,
        "principal",
        service_id,
        "d" * 64,
        1,
        StorageBackend.POSTGRESQL,
        1,
        "active",
        ReceiptVerification.VERIFIED,
        receipt_at,
    )
    observed_at: list[datetime] = []
    monkeypatch.setattr(
        PostgreSQLRegistryUnitOfWork,
        "_authority_prepared_or_terminal",
        lambda _self, _prepared: PreparedRegistryClaim(prepared),
    )
    monkeypatch.setattr(
        PostgreSQLRegistryUnitOfWork,
        "_service",
        lambda _self, _principal, _service_id: None,
    )
    monkeypatch.setattr(
        PostgreSQLRegistryUnitOfWork,
        "_receipt_exists",
        lambda _self, _receipt_id: False,
    )
    monkeypatch.setattr(
        PostgreSQLRegistryUnitOfWork,
        "_insert_receipt",
        lambda _self, _prepared, _receipt: None,
    )

    def complete(
        _self: PostgreSQLRegistryUnitOfWork,
        _prepared: object,
        result: object,
        at: datetime,
    ) -> object:
        observed_at.append(at)
        return result

    monkeypatch.setattr(PostgreSQLRegistryUnitOfWork, "_complete_authority", complete)
    uow = PostgreSQLRegistryUnitOfWork(PublishConnection())

    assert uow.publish_import(prepared, service, receipt) == service
    assert observed_at == [receipt_at]


def test_purge_retains_bounded_audit_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    class PurgeConnection(BoundaryConnection):
        def execute(self, sql: str, _params: tuple[object, ...] = ()) -> Cursor:
            self.statements.append(sql)
            return Cursor(
                rowcount=1
                if (
                    'UPDATE "aipcs_registry"."aipcs_registry_identity"' in sql
                    or 'DELETE FROM "aipcs_registry"."aipcs_registry_service"' in sql
                )
                else -1
            )

    service_id = UUID("66666666-6666-6666-6666-666666666666")
    at = datetime(2026, 7, 23, tzinfo=UTC)
    service = Service(
        service_id,
        "principal",
        "archived",
        "project",
        "portable",
        at,
        at,
        at,
        operational_status="archived",
        service_revision=7,
    )
    prepared = prepare_registry_intent(
        PurgeCommand(
            "principal",
            "test",
            service_id,
            7,
            "purge-claim",
            PurgeAuthority(PurgeAuthorityKind.EXPLICIT_OVERRIDE),
        )
    )
    connection = PurgeConnection()
    uow = PostgreSQLRegistryUnitOfWork(connection)
    monkeypatch.setattr(
        PostgreSQLRegistryUnitOfWork,
        "_authority_prepared_or_terminal",
        lambda _self, _prepared: PreparedRegistryClaim(prepared),
    )
    monkeypatch.setattr(
        PostgreSQLRegistryUnitOfWork,
        "_service",
        lambda _self, _principal, _service_id: service,
    )
    monkeypatch.setattr(
        PostgreSQLRegistryUnitOfWork,
        "_purge_receipt_is_valid",
        lambda _self, _prepared: True,
    )
    monkeypatch.setattr(
        PostgreSQLRegistryUnitOfWork,
        "_complete_authority",
        lambda _self, _prepared, result, _at: result,
    )

    uow.finalize_purge(prepared, at)

    assert not any(
        statement.startswith('DELETE FROM "aipcs_registry"."aipcs_registry_audit"')
        for statement in connection.statements
    )
    assert any(
        statement.startswith('DELETE FROM "aipcs_registry"."aipcs_registry_service"')
        for statement in connection.statements
    )


def test_uow_commit_started_connection_loss_is_uncertain() -> None:
    connection = BoundaryConnection(commit_error=SqlStateError("08006"))
    uow = PostgreSQLRegistryUnitOfWork(connection)

    with pytest.raises(StorageMigrationError):
        uow.commit()
