"""SYNTHETIC_FIXTURE. Synthetic fixture provenance: PostgreSQL R1 service foundation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from uuid import UUID

import pytest
from storage_contracts.conformance import (
    assert_service_store_catalog_conformance,
    assert_storage_component_ownership,
)

from aipcs_mcp.storage import (
    MigrationState,
    ServiceStoreLocator,
    StorageBusy,
    StorageMigrationError,
)
from aipcs_mcp.storage.postgresql.connection import PostgreSQLConnectionPolicy, PostgreSQLDsn
from aipcs_mcp.storage.postgresql.registry import PostgreSQLRegistryAdapter
from aipcs_mcp.storage.postgresql.registry_migrations import SCHEMA
from aipcs_mcp.storage.postgresql.service_store import PostgreSQLServiceStoreCatalog
from aipcs_mcp.storage.postgresql.service_store_migrations import (
    ADAPTER_ID,
    CHECKSUM,
    COMPONENT,
    META,
    MIGRATION,
    MIGRATION_ID,
    TARGET_REVISION,
    ddl,
    qualified,
    quote_identifier,
)

from .container import PostgresTestTarget


def _catalog(target: PostgresTestTarget) -> PostgreSQLServiceStoreCatalog:
    return PostgreSQLServiceStoreCatalog(PostgreSQLDsn(target.dsn))


def _locator(value: int = 1) -> ServiceStoreLocator:
    return ServiceStoreLocator.for_service("postgresql", UUID(int=value))


def _execute(target: PostgresTestTarget, statement: str, parameters: tuple[object, ...] = ()) -> None:
    driver = import_module("psycopg")
    with driver.connect(target.dsn) as connection:
        connection.execute(statement, parameters)


def _relation_count(target: PostgresTestTarget, namespace: str) -> int:
    driver = import_module("psycopg")
    with driver.connect(target.dsn) as connection:
        row = connection.execute(
            """SELECT count(*)
                 FROM pg_catalog.pg_class AS class
                 JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = class.relnamespace
                 WHERE namespace.nspname = %s""",
            (namespace,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_postgresql_service_catalog_allocates_and_materialises_r1(
    postgres_test_target: PostgresTestTarget,
) -> None:
    catalog = _catalog(postgres_test_target)
    locator = _locator()

    assert catalog.allocate(UUID(int=1)) == locator
    assert catalog.inspect_migration(locator) == MigrationState(
        "service_store", 0, TARGET_REVISION, "uninitialised"
    )
    assert catalog.migrate(locator) == MigrationState(
        "service_store", TARGET_REVISION, TARGET_REVISION, "ready"
    )
    assert catalog.migrate(locator) == MigrationState(
        "service_store", TARGET_REVISION, TARGET_REVISION, "ready"
    )


def test_postgresql_service_catalog_satisfies_shared_catalog_conformance(
    postgres_test_target: PostgresTestTarget,
) -> None:
    assert_service_store_catalog_conformance(
        lambda: _catalog(postgres_test_target), backend="postgresql"
    )


def test_postgresql_storage_components_satisfy_distinct_ownership_contract(
    postgres_test_target: PostgresTestTarget,
) -> None:
    registry = PostgreSQLRegistryAdapter(
        PostgreSQLDsn(postgres_test_target.dsn),
        PostgreSQLConnectionPolicy(SCHEMA, 5, 1_000, 10_000),
    )
    assert_storage_component_ownership(registry, _catalog(postgres_test_target))


def test_absent_namespace_inspection_has_no_schema_or_relation_side_effect(
    postgres_test_target: PostgresTestTarget,
) -> None:
    catalog = _catalog(postgres_test_target)
    locator = _locator(3)

    assert catalog.inspect_migration(locator).status == "uninitialised"
    assert _relation_count(postgres_test_target, locator.namespace) == 0


def test_existing_empty_or_partial_namespace_is_a_collision_not_an_adoption(
    postgres_test_target: PostgresTestTarget,
) -> None:
    catalog = _catalog(postgres_test_target)
    empty, partial = _locator(4), _locator(5)
    _execute(postgres_test_target, f"CREATE SCHEMA {quote_identifier(empty.namespace)}")
    _execute(postgres_test_target, f"CREATE SCHEMA {quote_identifier(partial.namespace)}")
    _execute(
        postgres_test_target,
        f"CREATE TABLE {qualified(partial.namespace, '__aipcs_collision')} (id bigint)",
    )

    for locator in (empty, partial):
        assert catalog.inspect_migration(locator) == MigrationState(
            "service_store", 0, TARGET_REVISION, "incompatible"
        )
        assert catalog.migrate(locator) == MigrationState(
            "service_store", 0, TARGET_REVISION, "incompatible"
        )
    assert _relation_count(postgres_test_target, empty.namespace) == 0


def test_catalog_rejects_copied_namespace_future_checksum_and_extra_reserved_object(
    postgres_test_target: PostgresTestTarget,
) -> None:
    catalog = _catalog(postgres_test_target)
    source, copied, altered, future, temporal, hostile = (
        _locator(6),
        _locator(7),
        _locator(8),
        _locator(9),
        _locator(10),
        _locator(11),
    )
    assert catalog.migrate(source).status == "ready"
    _execute(postgres_test_target, f"CREATE SCHEMA {quote_identifier(copied.namespace)}")
    for statement in ddl(copied.namespace):
        _execute(postgres_test_target, statement)
    _execute(
        postgres_test_target,
        f"INSERT INTO {qualified(copied.namespace, META)} "
        "(singleton, adapter_id, component, namespace, applied_revision) VALUES (%s, %s, %s, %s, %s)",
        (True, ADAPTER_ID, COMPONENT, source.namespace, TARGET_REVISION),
    )
    _execute(
        postgres_test_target,
        f"INSERT INTO {qualified(copied.namespace, MIGRATION)} "
        "(component, revision, migration_id, checksum, applied_at) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)",
        (COMPONENT, TARGET_REVISION, MIGRATION_ID, CHECKSUM),
    )
    assert catalog.inspect_migration(copied).status == "incompatible"

    assert catalog.migrate(altered).status == "ready"
    _execute(
        postgres_test_target,
        f"UPDATE {qualified(altered.namespace, MIGRATION)} SET checksum = %s",
        ("0" * 64,),
    )
    assert catalog.inspect_migration(altered).status == "incompatible"

    assert catalog.migrate(future).status == "ready"
    _execute(
        postgres_test_target,
        f"UPDATE {qualified(future.namespace, META)} SET applied_revision = 2",
    )
    assert catalog.inspect_migration(future) == MigrationState(
        "service_store", 2, TARGET_REVISION, "incompatible"
    )

    assert catalog.migrate(temporal).status == "ready"
    _execute(
        postgres_test_target,
        f"UPDATE {qualified(temporal.namespace, MIGRATION)} "
        "SET applied_at = 'infinity'::timestamptz",
    )
    assert catalog.inspect_migration(temporal).status == "incompatible"

    assert catalog.migrate(hostile).status == "ready"
    _execute(
        postgres_test_target,
        f"CREATE TABLE {qualified(hostile.namespace, '__aipcs_hostile')} (id bigint)",
    )
    assert catalog.inspect_migration(hostile).status == "incompatible"


def test_catalog_rejects_semantically_drifted_constraint_and_index(
    postgres_test_target: PostgresTestTarget,
) -> None:
    catalog = _catalog(postgres_test_target)
    constraint_drift, index_drift, opclass_drift, collation_drift, option_drift, predicate_drift = (
        _locator(12),
        _locator(13),
        _locator(14),
        _locator(15),
        _locator(16),
        _locator(17),
    )

    assert catalog.migrate(constraint_drift).status == "ready"
    _execute(
        postgres_test_target,
        f"ALTER TABLE {qualified(constraint_drift.namespace, '__aipcs_memory_branch')} "
        "DROP CONSTRAINT \"__aipcs_memory_branch_status\"",
    )
    _execute(
        postgres_test_target,
        f"ALTER TABLE {qualified(constraint_drift.namespace, '__aipcs_memory_branch')} "
        "ADD CONSTRAINT \"__aipcs_memory_branch_status\" "
        "CHECK (status IN ('active', 'archived', 'superseded') AND true)",
    )
    assert catalog.inspect_migration(constraint_drift).status == "incompatible"

    assert catalog.migrate(index_drift).status == "ready"
    index_name = "__aipcs_memory_branch_principal_status_updated"
    _execute(postgres_test_target, f"DROP INDEX {qualified(index_drift.namespace, index_name)}")
    _execute(
        postgres_test_target,
        f"CREATE INDEX {quote_identifier(index_name)} ON "
        f"{qualified(index_drift.namespace, '__aipcs_memory_branch')} (principal_id)",
    )
    assert catalog.inspect_migration(index_drift).status == "incompatible"

    for locator, definition in (
        (opclass_drift, "principal_id text_pattern_ops, status, updated_at"),
        (collation_drift, 'principal_id COLLATE "default", status, updated_at'),
        (option_drift, "principal_id DESC, status, updated_at"),
    ):
        assert catalog.migrate(locator).status == "ready"
        _execute(postgres_test_target, f"DROP INDEX {qualified(locator.namespace, index_name)}")
        _execute(
            postgres_test_target,
            f"CREATE INDEX {quote_identifier(index_name)} ON "
            f"{qualified(locator.namespace, '__aipcs_memory_branch')} ({definition})",
        )
        assert catalog.inspect_migration(locator).status == "incompatible"

    partial_name = "__aipcs_record_branch_primary"
    assert catalog.migrate(predicate_drift).status == "ready"
    _execute(postgres_test_target, f"DROP INDEX {qualified(predicate_drift.namespace, partial_name)}")
    _execute(
        postgres_test_target,
        f"CREATE UNIQUE INDEX {quote_identifier(partial_name)} ON "
        f"{qualified(predicate_drift.namespace, '__aipcs_record_branch')} "
        "(principal_id, entity_name, record_id) WHERE role = 'related'",
    )
    assert catalog.inspect_migration(predicate_drift).status == "incompatible"


def test_concurrent_fresh_catalogs_converge_under_the_transaction_lock(
    postgres_test_target: PostgresTestTarget,
) -> None:
    locator = _locator(18)

    with ThreadPoolExecutor(max_workers=2) as workers:
        states = list(workers.map(lambda _number: _catalog(postgres_test_target).migrate(locator), range(2)))

    assert states == [MigrationState("service_store", TARGET_REVISION, TARGET_REVISION, "ready")] * 2


class _SqlStateError(Exception):
    def __init__(self, state: str) -> None:
        self.sqlstate = state


def test_revision_hint_preserves_transient_catalog_failures() -> None:
    from aipcs_mcp.storage.postgresql import service_store as catalog_module

    class BusyConnection:
        def execute(self, _statement: str, _parameters: tuple[object, ...] = ()) -> object:
            raise _SqlStateError("55P03")

    with pytest.raises(_SqlStateError) as captured:
        catalog_module._revision_hint(BusyConnection(), _locator().namespace, ((META, "r"),))
    assert isinstance(catalog_module._inspection_failure(captured.value), StorageBusy)


def test_commit_connection_loss_is_uncertain_after_commit_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aipcs_mcp.storage.postgresql import service_store as catalog_module

    connection = object()
    catalog = PostgreSQLServiceStoreCatalog(
        PostgreSQLDsn("postgresql://synthetic:secret@127.0.0.1/synthetic"),
        connection_factory=lambda _dsn, _policy: connection,
    )
    monkeypatch.setattr(catalog_module, "_begin", lambda _connection, *, read_only: None)
    monkeypatch.setattr(catalog_module, "_require_supported_server", lambda _connection: None)
    monkeypatch.setattr(catalog_module, "_acquire_advisory_lock", lambda _connection, _namespace: None)
    monkeypatch.setattr(
        catalog_module,
        "_inspect",
        lambda _connection, _namespace: MigrationState("service_store", 1, 1, "ready"),
    )
    monkeypatch.setattr(catalog_module, "_rollback", lambda _connection: None)
    monkeypatch.setattr(catalog_module, "_close", lambda _connection: None)
    monkeypatch.setattr(catalog_module, "_commit", lambda _connection: (_ for _ in ()).throw(_SqlStateError("08006")))

    with pytest.raises(StorageMigrationError):
        catalog.migrate(_locator())
