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
    service,
    transfer,
)

from aipcs_mcp.application.operational_lifecycle import (
    ArchiveCommand,
    ResumeCommand,
    SuspendCommand,
)
from aipcs_mcp.application.portable import PortableResultCategory
from aipcs_mcp.application.registry_authority import (
    PurgeAuthority,
    PurgeAuthorityKind,
    PurgeCommand,
    StorageBackend,
)
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


def test_installed_shape_sqlite_to_postgresql_lifecycle_purge_and_restart(
    postgres_test_target: PostgresTestTarget,
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    source = _sqlite(tmp_path / "source", "installed-source")
    destination = _postgres(postgres_test_target, "installed-destination")
    fixture = logical_fixture(
        UUID("85000000-0000-4000-8000-000000000001"),
        "installed_shape",
    )
    install(source, fixture)
    imported, _artifact = transfer(
        source,
        destination,
        fixture,
        key="installed-shape",
    )
    for command in (
        ResumeCommand(
            destination.principal,
            "installed-verifier",
            imported.service.service_id,
            5,
            "installed-resume",
        ),
        SuspendCommand(
            destination.principal,
            "installed-verifier",
            imported.service.service_id,
            6,
            "installed-suspend",
        ),
        ArchiveCommand(
            destination.principal,
            "installed-verifier",
            imported.service.service_id,
            7,
            "installed-archive",
        ),
    ):
        assert (
            destination.coordinator.transition(command).category
            is PortableResultCategory.COMPLETED
        )
    archived = service(destination, imported.service.service_id)
    assert archived is not None
    assert archived.operational_status == "archived"
    purged = destination.coordinator.purge(
        PurgeCommand(
            destination.principal,
            "installed-verifier",
            archived.service_id,
            8,
            "installed-purge",
            PurgeAuthority(PurgeAuthorityKind.EXPLICIT_OVERRIDE),
        )
    )
    assert purged.category is PortableResultCategory.COMPLETED
    assert purged.tombstone is not None

    restarted = _postgres(postgres_test_target, destination.principal)
    replay = restarted.coordinator.purge(
        PurgeCommand(
            destination.principal,
            "installed-verifier",
            archived.service_id,
            8,
            "installed-purge",
            PurgeAuthority(PurgeAuthorityKind.EXPLICIT_OVERRIDE),
        )
    )
    assert replay.category is PortableResultCategory.COMPLETED
    assert replay.tombstone == purged.tombstone
