"""SYNTHETIC_FIXTURE. Opt-in disposable PostgreSQL registry R1 proof."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from application.fakes import FixedClock, SequentialIds
from fixtures import valid_manifest
from storage_contracts.conformance import (
    assert_registry_application_conformance,
    assert_storage_component_ownership,
)

from aipcs_mcp.application.models import (
    ApplicationContext,
    OperationInProgress,
    PreparedLifecycleClaim,
    SeedCommand,
    Service,
)
from aipcs_mcp.application.operational_lifecycle import SuspendCommand
from aipcs_mcp.application.registry_authority import (
    CompletedRegistryClaim,
    ExportCommand,
    IdentityCollisionKind,
    ImportCommand,
    ImportIdentity,
    PreparedRegistryClaim,
    ReceiptKind,
    ReceiptVerification,
    RecoveryRequiredRegistryClaim,
    RegistryIdentityCollision,
    RegistryOperationInProgress,
    StorageBackend,
    TransferReceipt,
)
from aipcs_mcp.application.services import ServiceApplication
from aipcs_mcp.lifecycle import MaterialiseCommand, RecoveryState
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.storage.contracts import MigrationState
from aipcs_mcp.storage.postgresql.connection import (
    PostgreSQLConnectionPolicy,
    PostgreSQLDsn,
    connect_postgresql,
)
from aipcs_mcp.storage.postgresql.registry import PostgreSQLRegistryAdapter
from aipcs_mcp.storage.postgresql.registry_migrations import (
    ADAPTER_ID,
    R1_CHECKSUM,
    R1_DDL,
    R1_MIGRATION_ID,
    SCHEMA,
    TARGET_REVISION,
)

from .container import PostgresTestTarget


class TraceUow:
    def __init__(self, wrapped: object, harness: PostgreSQLRegistryHarness) -> None:
        self.wrapped = wrapped
        self.harness = harness
        self.calls: list[str] = []
        self.close_count = 0
        self.services = self
        self.mutations = self
        self.audits = self

    def _call(self, name: str, *args: object) -> object:
        self.calls.append(name)
        if self.harness.failure == name:
            raise RuntimeError("postgresql-secret")
        return getattr(self.wrapped, name)(*args)

    def find_domain(self, *args: object) -> object:
        return self._call("find_domain", *args)

    def get(self, *args: object) -> object:
        return self._call("get", *args)

    def count(self, *args: object) -> object:
        return self._call("count", *args)

    def list(self, *args: object) -> object:
        return self._call("list", *args)

    def add(self, *args: object) -> object:
        return self._call("add", *args)

    def save(self, *args: object) -> object:
        return self._call("save", *args)

    def resolve_non_lifecycle(self, *args: object) -> object:
        return self._call("resolve_non_lifecycle", *args)

    def complete_non_lifecycle(self, *args: object) -> object:
        return self._call("complete_non_lifecycle", *args)

    def recovery_state(self, *args: object) -> object:
        return self._call("recovery_state", *args)

    def append(self, *args: object) -> object:
        return self._call("append", *args)

    def commit(self) -> object:
        return self._call("commit")

    def rollback(self) -> object:
        return self._call("rollback")

    def close(self) -> None:
        self.close_count += 1
        self.calls.append("close")
        self.wrapped.close()
        if self.harness.failure == "close":
            raise RuntimeError("postgresql-secret")


class PostgreSQLRegistryHarness:
    def __init__(self, target: PostgresTestTarget) -> None:
        self._dsn = PostgreSQLDsn(target.dsn)
        self.adapter = PostgreSQLRegistryAdapter(
            self._dsn,
            PostgreSQLConnectionPolicy(SCHEMA, 5, 1_000, 10_000),
        )
        assert self.adapter.migrate().status == "ready"
        self.failure: str | None = None
        self.items: list[TraceUow] = []

    def application(self, *ids: UUID) -> ServiceApplication:
        def factory() -> TraceUow:
            uow = TraceUow(self.adapter.open_uow(), self)
            self.items.append(uow)
            return uow

        return ServiceApplication(
            factory,
            FixedClock(datetime(2026, 1, 1, tzinfo=UTC)),
            SequentialIds(*ids),
        )

    restart = application

    def traces(self) -> list[TraceUow]:
        return self.items

    def fail(self, boundary: str | None) -> None:
        self.failure = boundary


class FreshHarnessFactory:
    def __init__(self, target: PostgresTestTarget) -> None:
        self._target = target

    def __call__(self) -> PostgreSQLRegistryHarness:
        dsn = PostgreSQLDsn(self._target.dsn)
        connection = connect_postgresql(
            dsn,
            PostgreSQLConnectionPolicy(SCHEMA, 5, 1_000, 10_000),
        )
        try:
            connection.execute('DROP SCHEMA IF EXISTS "aipcs_registry" CASCADE')
            connection.commit()
        finally:
            connection.close()
        return PostgreSQLRegistryHarness(self._target)


def _registry_adapter(target: PostgresTestTarget) -> PostgreSQLRegistryAdapter:
    return PostgreSQLRegistryAdapter(
        PostgreSQLDsn(target.dsn),
        PostgreSQLConnectionPolicy(SCHEMA, 5, 1_000, 10_000),
    )


def _execute_test_mutation(target: PostgresTestTarget, *statements: str) -> None:
    connection = connect_postgresql(
        PostgreSQLDsn(target.dsn),
        PostgreSQLConnectionPolicy(SCHEMA, 5, 1_000, 10_000),
    )
    try:
        for statement in statements:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


def test_registry_migrates_and_opens_an_independent_ready_uow(
    postgres_test_target: PostgresTestTarget,
) -> None:
    adapter = PostgreSQLRegistryAdapter(
        PostgreSQLDsn(postgres_test_target.dsn),
        PostgreSQLConnectionPolicy(SCHEMA, 5, 1_000, 10_000),
    )

    assert adapter.inspect_migration() == MigrationState(
        "registry", 0, TARGET_REVISION, "uninitialised"
    )
    assert adapter.migrate() == MigrationState(
        "registry", TARGET_REVISION, TARGET_REVISION, "ready"
    )
    assert adapter.inspect_migration() == MigrationState(
        "registry", TARGET_REVISION, TARGET_REVISION, "ready"
    )

    first = adapter.open_uow()
    second = adapter.open_uow()
    try:
        assert first is not second
        assert first.count("synthetic-principal") == 0
        assert second.count("synthetic-principal") == 0
        first.commit()
        second.commit()
    finally:
        first.close()
        second.close()


def test_registry_upgrades_r1_transactionally(
    postgres_test_target: PostgresTestTarget,
) -> None:
    connection = connect_postgresql(
        PostgreSQLDsn(postgres_test_target.dsn),
        PostgreSQLConnectionPolicy(SCHEMA, 5, 1_000, 10_000),
    )
    try:
        connection.execute("BEGIN")
        for statement in R1_DDL:
            connection.execute(statement)
        connection.execute(
            'INSERT INTO "aipcs_registry"."aipcs_registry_meta" '
            '("singleton","adapter_id","component","applied_revision","dirty") '
            "VALUES (%s,%s,%s,%s,%s)",
            (1, ADAPTER_ID, "registry", 1, False),
        )
        connection.execute(
            'INSERT INTO "aipcs_registry"."aipcs_registry_migration" '
            '("component","revision","migration_id","checksum","applied_at") '
            "VALUES (%s,%s,%s,%s,%s)",
            (
                "registry",
                1,
                R1_MIGRATION_ID,
                R1_CHECKSUM,
                datetime(2026, 1, 1, tzinfo=UTC),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    adapter = _registry_adapter(postgres_test_target)
    assert adapter.inspect_migration() == MigrationState(
        "registry", 1, TARGET_REVISION, "outdated"
    )
    assert adapter.migrate() == MigrationState(
        "registry", TARGET_REVISION, TARGET_REVISION, "ready"
    )

    connection = connect_postgresql(
        PostgreSQLDsn(postgres_test_target.dsn),
        PostgreSQLConnectionPolicy(SCHEMA, 5, 1_000, 10_000),
    )
    try:
        relation_names = {
            row[0]
            for row in connection.execute(
                "SELECT c.relname FROM pg_catalog.pg_class AS c "
                "JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s AND c.relkind='r'",
                (SCHEMA,),
            ).fetchall()
        }
    finally:
        connection.close()
    assert "aipcs_registry_mutation" not in relation_names
    assert {
        "aipcs_registry_claim",
        "aipcs_registry_identity",
        "aipcs_registry_receipt",
    } <= relation_names


def test_existing_empty_registry_schema_is_never_adopted(
    postgres_test_target: PostgresTestTarget,
) -> None:
    _execute_test_mutation(
        postgres_test_target,
        'CREATE SCHEMA "aipcs_registry" AUTHORIZATION CURRENT_USER',
        'REVOKE ALL PRIVILEGES ON SCHEMA "aipcs_registry" FROM PUBLIC',
    )
    adapter = _registry_adapter(postgres_test_target)

    expected = MigrationState("registry", 0, TARGET_REVISION, "incompatible")
    assert adapter.inspect_migration() == expected
    assert adapter.migrate() == expected


@pytest.mark.parametrize(
    "statements",
    [
        (
            'ALTER TABLE "aipcs_registry"."aipcs_registry_service" '
            'DROP CONSTRAINT "aipcs_registry_service_domain_check"',
            'ALTER TABLE "aipcs_registry"."aipcs_registry_service" '
            'ADD CONSTRAINT "aipcs_registry_service_domain_check" CHECK (true)',
        ),
        (
            'DROP INDEX "aipcs_registry"."aipcs_registry_service_list"',
            'CREATE INDEX "aipcs_registry_service_list" '
            'ON "aipcs_registry"."aipcs_registry_service" ("service_id")',
        ),
        ('GRANT USAGE ON SCHEMA "aipcs_registry" TO PUBLIC',),
    ],
)
def test_structured_catalog_drift_is_incompatible(
    postgres_test_target: PostgresTestTarget,
    statements: tuple[str, ...],
) -> None:
    adapter = _registry_adapter(postgres_test_target)
    assert adapter.migrate().status == "ready"

    _execute_test_mutation(postgres_test_target, *statements)

    assert adapter.inspect_migration() == MigrationState(
        "registry", TARGET_REVISION, TARGET_REVISION, "incompatible"
    )


def test_completion_current_service_cross_row_drift_is_incompatible(
    postgres_test_target: PostgresTestTarget,
) -> None:
    harness = PostgreSQLRegistryHarness(postgres_test_target)
    context = ApplicationContext("semantic-principal", "storage-contract")
    harness.application(UUID(int=1)).seed(
        context,
        SeedCommand("notes", "project", "Notes", "seed-1"),
    )
    _execute_test_mutation(
        postgres_test_target,
        'UPDATE "aipcs_registry"."aipcs_registry_claim" '
        "SET \"result_json\"=jsonb_set(\"result_json\",'{domain_name}',"
        "to_jsonb('changed'::text))",
    )

    assert harness.adapter.inspect_migration() == MigrationState(
        "registry", TARGET_REVISION, TARGET_REVISION, "incompatible"
    )


def test_more_than_one_thousand_audit_rows_per_principal_is_incompatible(
    postgres_test_target: PostgresTestTarget,
) -> None:
    harness = PostgreSQLRegistryHarness(postgres_test_target)
    context = ApplicationContext("cap-principal", "storage-contract")
    harness.application(UUID(int=1)).seed(
        context,
        SeedCommand("notes", "project", "Notes", "seed-1"),
    )
    _execute_test_mutation(
        postgres_test_target,
        'INSERT INTO "aipcs_registry"."aipcs_registry_audit" '
        '("action","outcome","service_id","principal_id","created_via","at") '
        "SELECT 'seed','created','00000000-0000-0000-0000-000000000001'::uuid,"
        "'cap-principal','storage-contract',CURRENT_TIMESTAMP "
        "FROM generate_series(1,1000)",
    )

    assert harness.adapter.inspect_migration() == MigrationState(
        "registry", TARGET_REVISION, TARGET_REVISION, "incompatible"
    )


def test_registry_satisfies_backend_neutral_application_conformance(
    postgres_test_target: PostgresTestTarget,
) -> None:
    assert_registry_application_conformance(FreshHarnessFactory(postgres_test_target))


def test_registry_authority_restarts_replay_and_import_reservations_are_invisible(
    postgres_test_target: PostgresTestTarget,
) -> None:
    adapter = _registry_adapter(postgres_test_target)
    assert adapter.migrate().status == "ready"
    at = datetime(2026, 7, 23, tzinfo=UTC)
    service_id = UUID("77777777-7777-7777-7777-777777777777")
    service = Service(
        service_id,
        "authority-principal",
        "authority",
        "project",
        "restart evidence",
        at,
        at,
        at,
    )
    add = adapter.open_uow()
    try:
        add.add(service)
        add.commit()
    finally:
        add.close()

    command = SuspendCommand(
        "authority-principal", "storage-contract", service_id, 1, "suspend-restart"
    )
    prepare = adapter.open_uow()
    try:
        prepared = prepare.authority.resolve_or_prepare(command)
        assert isinstance(prepared, PreparedRegistryClaim)
        prepare.commit()
    finally:
        prepare.close()

    finalize = adapter.open_uow()
    try:
        completed = finalize.authority.finalize_operational(
            prepared.intent, datetime(2026, 7, 23, 0, 0, 1, tzinfo=UTC)
        )
        assert isinstance(completed, CompletedRegistryClaim)
        finalize.commit()
    finally:
        finalize.close()

    assert adapter.inspect_migration().status == "ready"
    replay = adapter.open_uow()
    try:
        replayed = replay.authority.resolve_or_prepare(command)
        assert replayed == completed
        assert replay.get("authority-principal", service_id) == completed.result
        replay.commit()
    finally:
        replay.close()

    import_id = UUID("88888888-8888-8888-8888-888888888888")
    identity = ImportIdentity(
        "authority-principal", import_id, "imported", f"svc_{import_id.hex}"
    )
    reserve = adapter.open_uow()
    try:
        reservation = reserve.authority.resolve_or_prepare(
            ImportCommand(
                "authority-principal",
                "storage-contract",
                identity,
                StorageBackend.POSTGRESQL,
                "a" * 64,
                "import-reservation",
            )
        )
        assert isinstance(reservation, PreparedRegistryClaim)
        reserve.commit()
    finally:
        reserve.close()

    check = adapter.open_uow()
    try:
        assert check.get("authority-principal", import_id) is None
        collision = check.authority.resolve_or_prepare(
            ImportCommand(
                "authority-principal",
                "storage-contract",
                identity,
                StorageBackend.POSTGRESQL,
                "a" * 64,
                "import-collision",
            )
        )
        assert collision == RegistryIdentityCollision(IdentityCollisionKind.SERVICE_ID)
        check.commit()
    finally:
        check.close()

    source_at = datetime(2020, 1, 1, tzinfo=UTC)
    receipt_at = datetime(2026, 7, 23, 0, 0, 3, tzinfo=UTC)
    imported = Service(
        import_id,
        "authority-principal",
        "imported",
        "project",
        "portable",
        source_at,
        source_at,
        source_at,
    )
    receipt = TransferReceipt(
        UUID("99999999-9999-9999-9999-999999999999"),
        ReceiptKind.IMPORT,
        "authority-principal",
        import_id,
        "a" * 64,
        1,
        StorageBackend.POSTGRESQL,
        1,
        "active",
        ReceiptVerification.VERIFIED,
        receipt_at,
    )
    publish = adapter.open_uow()
    try:
        publication = publish.authority.publish_import(
            reservation.intent, imported, receipt
        )
        assert isinstance(publication, CompletedRegistryClaim)
        publish.commit()
    finally:
        publish.close()

    connection = connect_postgresql(
        PostgreSQLDsn(postgres_test_target.dsn),
        PostgreSQLConnectionPolicy(SCHEMA, 5, 1_000, 10_000),
    )
    try:
        audit_at = connection.execute(
            'SELECT "at" FROM "aipcs_registry"."aipcs_registry_audit" '
            'WHERE "principal_id"=%s AND "service_id"=%s AND "action"=%s',
            ("authority-principal", str(import_id), "import"),
        ).fetchone()
        assert audit_at == (receipt_at,)
    finally:
        connection.close()

    recovery_id = UUID("aaaaaaaa-8888-4888-8888-888888888888")
    recovery_command = ImportCommand(
        "authority-principal",
        "storage-contract",
        ImportIdentity(
            "authority-principal",
            recovery_id,
            "recovery_import",
            f"svc_{recovery_id.hex}",
        ),
        StorageBackend.POSTGRESQL,
        "b" * 64,
        "import-recovery",
    )
    recover = adapter.open_uow()
    try:
        recovery_prepared = recover.authority.resolve_or_prepare(recovery_command)
        assert isinstance(recovery_prepared, PreparedRegistryClaim)
        recover.commit()
    finally:
        recover.close()
    recover = adapter.open_uow()
    try:
        recovery_required = recover.authority.mark_recovery_required(
            recovery_prepared.intent, receipt_at + timedelta(seconds=1)
        )
        assert isinstance(recovery_required, RecoveryRequiredRegistryClaim)
        recover.commit()
    finally:
        recover.close()
    assert adapter.inspect_migration().status == "ready"


def test_registry_cross_family_claims_return_typed_blockers(
    postgres_test_target: PostgresTestTarget,
) -> None:
    adapter = _registry_adapter(postgres_test_target)
    assert adapter.migrate().status == "ready"
    at = datetime(2026, 7, 23, tzinfo=UTC)
    manifest = ManifestV2.model_validate(valid_manifest())
    authority_first_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    lifecycle_first_id = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    authority_recovery_id = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
    lifecycle_recovery_id = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
    add = adapter.open_uow()
    try:
        for service_id, domain in (
            (authority_first_id, "authority_first"),
            (lifecycle_first_id, "lifecycle_first"),
            (authority_recovery_id, "authority_recovery"),
            (lifecycle_recovery_id, "lifecycle_recovery"),
        ):
            add.add(
                Service(
                    service_id,
                    "cross-family",
                    domain,
                    "project",
                    "blocker",
                    at,
                    at,
                    at,
                    manifest=manifest,
                    schema_version=1,
                )
            )
        add.commit()
    finally:
        add.close()

    authority_first = adapter.open_uow()
    try:
        authority_claim = authority_first.authority.resolve_or_prepare(
            ExportCommand(
                "cross-family",
                "storage-contract",
                authority_first_id,
                1,
                "authority-first",
            )
        )
        assert isinstance(authority_claim, PreparedRegistryClaim)
        authority_first.commit()
    finally:
        authority_first.close()

    lifecycle_blocked = adapter.open_uow()
    try:
        blocked = lifecycle_blocked.mutations.resolve_or_admit(
            MaterialiseCommand(
                "cross-family",
                "storage-contract",
                authority_first_id,
                1,
                1,
                "lifecycle-blocked",
            )
        )
        assert isinstance(blocked, OperationInProgress)
        assert (
            lifecycle_blocked.mutations.recovery_state(
                "cross-family", authority_first_id
            )
            is RecoveryState.PENDING
        )
        lifecycle_blocked.commit()
    finally:
        lifecycle_blocked.close()

    lifecycle_first = adapter.open_uow()
    try:
        lifecycle_claim = lifecycle_first.mutations.resolve_or_admit(
            MaterialiseCommand(
                "cross-family",
                "storage-contract",
                lifecycle_first_id,
                1,
                1,
                "lifecycle-first",
            )
        )
        assert isinstance(lifecycle_claim, PreparedLifecycleClaim)
        lifecycle_first.commit()
    finally:
        lifecycle_first.close()

    authority_blocked = adapter.open_uow()
    try:
        direct_blocker = authority_blocked._authority_blocker(
            "cross-family", lifecycle_first_id
        )
        assert isinstance(direct_blocker, RegistryOperationInProgress)
        blocked = authority_blocked.authority.resolve_or_prepare(
            ExportCommand(
                "cross-family",
                "storage-contract",
                lifecycle_first_id,
                1,
                "authority-blocked",
            )
        )
        assert isinstance(blocked, RegistryOperationInProgress)
        assert blocked.intent == lifecycle_claim.intent
        assert (
            authority_blocked.mutations.recovery_state(
                "cross-family", lifecycle_first_id
            )
            is RecoveryState.PENDING
        )
        authority_blocked.commit()
    finally:
        authority_blocked.close()

    authority_recovery = adapter.open_uow()
    try:
        authority_recovery_claim = authority_recovery.authority.resolve_or_prepare(
            ExportCommand(
                "cross-family",
                "storage-contract",
                authority_recovery_id,
                1,
                "authority-recovery",
            )
        )
        assert isinstance(authority_recovery_claim, PreparedRegistryClaim)
        authority_recovery.commit()
    finally:
        authority_recovery.close()
    mark_authority_recovery = adapter.open_uow()
    try:
        recovered = mark_authority_recovery.authority.complete_export(
            authority_recovery_claim.intent,
            TransferReceipt(
                UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
                ReceiptKind.EXPORT,
                "cross-family",
                authority_recovery_id,
                "e" * 64,
                1,
                StorageBackend.SQLITE,
                1,
                "active",
                ReceiptVerification.VERIFIED,
                at + timedelta(seconds=1),
            ),
        )
        assert isinstance(recovered, RecoveryRequiredRegistryClaim)
        mark_authority_recovery.commit()
    finally:
        mark_authority_recovery.close()
    lifecycle_recovery_blocked = adapter.open_uow()
    try:
        blocked = lifecycle_recovery_blocked.mutations.resolve_or_admit(
            MaterialiseCommand(
                "cross-family",
                "storage-contract",
                authority_recovery_id,
                1,
                1,
                "lifecycle-recovery-blocked",
            )
        )
        assert isinstance(blocked, OperationInProgress)
        assert (
            lifecycle_recovery_blocked.mutations.recovery_state(
                "cross-family", authority_recovery_id
            )
            is RecoveryState.RECOVERY_REQUIRED
        )
        lifecycle_recovery_blocked.commit()
    finally:
        lifecycle_recovery_blocked.close()

    lifecycle_recovery = adapter.open_uow()
    try:
        lifecycle_recovery_claim = lifecycle_recovery.mutations.resolve_or_admit(
            MaterialiseCommand(
                "cross-family",
                "storage-contract",
                lifecycle_recovery_id,
                1,
                1,
                "lifecycle-recovery",
            )
        )
        assert isinstance(lifecycle_recovery_claim, PreparedLifecycleClaim)
        lifecycle_recovery.commit()
    finally:
        lifecycle_recovery.close()
    mark_lifecycle_recovery = adapter.open_uow()
    try:
        lifecycle_recovered = (
            mark_lifecycle_recovery.mutations.finalize_recovery_required(
                lifecycle_recovery_claim.intent, at + timedelta(seconds=1)
            )
        )
        assert lifecycle_recovered.intent.phase.value == "recovery_required"
        mark_lifecycle_recovery.commit()
    finally:
        mark_lifecycle_recovery.close()
    authority_recovery_blocked = adapter.open_uow()
    try:
        blocked = authority_recovery_blocked.authority.resolve_or_prepare(
            ExportCommand(
                "cross-family",
                "storage-contract",
                lifecycle_recovery_id,
                1,
                "authority-recovery-blocked",
            )
        )
        assert isinstance(blocked, RecoveryRequiredRegistryClaim)
        assert blocked.intent == lifecycle_recovered.intent
        assert (
            authority_recovery_blocked.mutations.recovery_state(
                "cross-family", lifecycle_recovery_id
            )
            is RecoveryState.RECOVERY_REQUIRED
        )
        authority_recovery_blocked.commit()
    finally:
        authority_recovery_blocked.close()
    assert adapter.inspect_migration().status == "ready"


def test_postgresql_registry_and_service_store_preserve_component_ownership(
    postgres_test_target: PostgresTestTarget,
) -> None:
    from aipcs_mcp.storage.postgresql.service_store import PostgreSQLServiceStoreCatalog

    dsn = PostgreSQLDsn(postgres_test_target.dsn)
    registry = PostgreSQLRegistryAdapter(
        dsn,
        PostgreSQLConnectionPolicy(SCHEMA, 5, 1_000, 10_000),
    )
    catalog = PostgreSQLServiceStoreCatalog(dsn)

    assert_storage_component_ownership(registry, catalog)
