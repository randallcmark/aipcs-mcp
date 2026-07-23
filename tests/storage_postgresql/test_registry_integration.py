"""SYNTHETIC_FIXTURE. Opt-in disposable PostgreSQL registry R1 proof."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from application.fakes import FixedClock, SequentialIds
from storage_contracts.conformance import (
    assert_registry_application_conformance,
    assert_storage_component_ownership,
)

from aipcs_mcp.application.models import ApplicationContext, SeedCommand
from aipcs_mcp.application.services import ServiceApplication
from aipcs_mcp.storage.contracts import MigrationState
from aipcs_mcp.storage.postgresql.connection import (
    PostgreSQLConnectionPolicy,
    PostgreSQLDsn,
    connect_postgresql,
)
from aipcs_mcp.storage.postgresql.registry import PostgreSQLRegistryAdapter
from aipcs_mcp.storage.postgresql.registry_migrations import SCHEMA

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

    assert adapter.inspect_migration() == MigrationState("registry", 0, 1, "uninitialised")
    assert adapter.migrate() == MigrationState("registry", 1, 1, "ready")
    assert adapter.inspect_migration() == MigrationState("registry", 1, 1, "ready")

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


def test_existing_empty_registry_schema_is_never_adopted(
    postgres_test_target: PostgresTestTarget,
) -> None:
    _execute_test_mutation(
        postgres_test_target,
        'CREATE SCHEMA "aipcs_registry" AUTHORIZATION CURRENT_USER',
        'REVOKE ALL PRIVILEGES ON SCHEMA "aipcs_registry" FROM PUBLIC',
    )
    adapter = _registry_adapter(postgres_test_target)

    expected = MigrationState("registry", 0, 1, "incompatible")
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

    assert adapter.inspect_migration() == MigrationState("registry", 1, 1, "incompatible")


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
        'UPDATE "aipcs_registry"."aipcs_registry_mutation" '
        "SET \"result_json\"=jsonb_set(\"result_json\",'{domain_name}',"
        "to_jsonb('changed'::text))",
    )

    assert harness.adapter.inspect_migration() == MigrationState(
        "registry", 1, 1, "incompatible"
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
        "registry", 1, 1, "incompatible"
    )


def test_registry_satisfies_backend_neutral_application_conformance(
    postgres_test_target: PostgresTestTarget,
) -> None:
    assert_registry_application_conformance(FreshHarnessFactory(postgres_test_target))


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
