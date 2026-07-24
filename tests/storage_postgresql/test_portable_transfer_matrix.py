"""C7 same- and cross-backend logical transfer matrix on pinned PostgreSQL."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from storage_contracts.portable_conformance import (
    FixedClock,
    PortableInstallation,
    install,
    logical_fixture,
    transfer,
)

from aipcs_mcp.application.registry_authority import StorageBackend
from aipcs_mcp.portable_coordinator import PortableCoordinator
from aipcs_mcp.storage.postgresql import (
    PostgreSQLConnectionPolicy,
    PostgreSQLDsn,
    PostgreSQLPortableServiceStore,
    PostgreSQLRegistryAdapter,
)
from aipcs_mcp.storage.postgresql.registry_migrations import SCHEMA
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter
from aipcs_mcp.storage.sqlite.portable import SQLitePortableServiceStore

from .container import (
    DisposablePostgres,
    PostgresContainerSettings,
    PostgresTestTarget,
)


@pytest.fixture
def second_postgres_target(
    postgres_test_target: PostgresTestTarget,
) -> PostgresTestTarget:
    del postgres_test_target
    settings = PostgresContainerSettings.optional_from_environ(os.environ)
    assert settings is not None
    fixture = DisposablePostgres(settings)
    try:
        yield fixture.start()
    finally:
        fixture.close()


def _postgres(
    target: PostgresTestTarget, principal: str
) -> PortableInstallation:
    dsn = PostgreSQLDsn(target.dsn)
    registry = PostgreSQLRegistryAdapter(
        dsn,
        PostgreSQLConnectionPolicy(SCHEMA, 5, 1_000, 10_000),
    )
    assert registry.migrate().status == "ready"
    store = PostgreSQLPortableServiceStore(dsn, clock=FixedClock().now)
    coordinator = PortableCoordinator(
        registry.open_uow,
        FixedClock(),
        uuid4,
        StorageBackend.POSTGRESQL,
        store,
    )
    return PortableInstallation(
        StorageBackend.POSTGRESQL,
        principal,
        registry.open_uow,
        store,
        coordinator,
    )


def _sqlite(root, principal: str) -> PortableInstallation:  # type: ignore[no-untyped-def]
    location = SQLiteLocationPolicy(root)
    registry = SQLiteRegistryAdapter(location)
    assert registry.migrate().status == "ready"
    store = SQLitePortableServiceStore(location, clock=FixedClock().now)
    coordinator = PortableCoordinator(
        registry.open_uow,
        FixedClock(),
        uuid4,
        StorageBackend.SQLITE,
        store,
    )
    return PortableInstallation(
        StorageBackend.SQLITE,
        principal,
        registry.open_uow,
        store,
        coordinator,
    )


def test_all_four_transfer_directions_preserve_normalized_logical_state(
    postgres_test_target: PostgresTestTarget,
    second_postgres_target: PostgresTestTarget,
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    sqlite_source = _sqlite(tmp_path / "sqlite-source", "sqlite-source")
    sqlite_destination = _sqlite(
        tmp_path / "sqlite-destination", "sqlite-destination"
    )
    postgres_source = _postgres(postgres_test_target, "postgres-source")
    postgres_destination = _postgres(
        second_postgres_target, "postgres-destination"
    )
    cases = (
        (
            sqlite_source,
            sqlite_destination,
            UUID("81000000-0000-4000-8000-000000000001"),
            "sqlite_to_sqlite",
        ),
        (
            postgres_source,
            postgres_destination,
            UUID("82000000-0000-4000-8000-000000000001"),
            "postgres_to_postgres",
        ),
        (
            sqlite_source,
            postgres_destination,
            UUID("83000000-0000-4000-8000-000000000001"),
            "sqlite_to_postgres",
        ),
        (
            postgres_source,
            sqlite_destination,
            UUID("84000000-0000-4000-8000-000000000001"),
            "postgres_to_sqlite",
        ),
    )
    artifacts: list[bytes] = []
    for source, destination, service_id, domain_name in cases:
        fixture = logical_fixture(service_id, domain_name)
        install(source, fixture)
        _imported, artifact = transfer(
            source,
            destination,
            fixture,
            key=domain_name.replace("_to_", "-"),
        )
        artifacts.append(artifact)

    assert b'"source_backend":"sqlite"' in artifacts[0]
    assert b'"source_backend":"postgresql"' in artifacts[1]
    assert b'"source_backend":"sqlite"' in artifacts[2]
    assert b'"source_backend":"postgresql"' in artifacts[3]
