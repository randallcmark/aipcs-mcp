"""Backend-neutral registry application conformance cases.

Future adapters provide a test-only harness with fresh application instances;
these cases intentionally assert public behaviour and tracing only.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast
from uuid import UUID

from fixtures import valid_manifest

from aipcs_mcp.application.errors import Conflict, InternalFailure
from aipcs_mcp.application.models import (
    ApplicationContext,
    DesignCommand,
    SeedCommand,
)
from aipcs_mcp.application.services import ServiceApplication
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.storage import (
    MigrationState,
    RegistryAdapter,
    ServiceStoreCatalog,
    ServiceStoreLocator,
    StorageContractError,
)


class UowTrace(Protocol):
    close_count: int
    calls: list[str]


class RegistryHarness(Protocol):
    def application(self, *ids: UUID) -> ServiceApplication: ...

    def restart(self, *ids: UUID) -> ServiceApplication: ...

    def traces(self) -> list[UowTrace]: ...

    def fail(self, boundary: str | None) -> None: ...


RegistryHarnessFactory = Callable[[], RegistryHarness]
ServiceStoreCatalogFactory = Callable[[], ServiceStoreCatalog]


def assert_storage_component_ownership(
    registry: RegistryAdapter, catalog: ServiceStoreCatalog
) -> None:
    registry_info = registry.info()
    catalog_info = catalog.info()
    assert "registry" in registry_info.supported_components
    assert "service_store" in catalog_info.supported_components
    assert registry_info.backend == catalog_info.backend
    assert registry.inspect_migration().component == "registry"
    assert registry.migrate().component == "registry"

    locator = catalog.allocate(UUID(int=1))
    assert locator.backend == catalog_info.backend
    assert catalog.inspect_migration(locator).component == "service_store"
    assert catalog.migrate(locator).component == "service_store"

    other_backend = "postgresql" if locator.backend == "sqlite" else "sqlite"
    mismatch = ServiceStoreLocator(other_backend, locator.namespace)
    _expect(StorageContractError, lambda: catalog.inspect_migration(mismatch))
    _expect(StorageContractError, lambda: catalog.migrate(mismatch))


def assert_service_store_catalog_conformance(
    factory: ServiceStoreCatalogFactory, *, backend: str
) -> None:
    """Assert backend-neutral logical allocation and migration-state behavior."""

    catalog = factory()
    info = catalog.info()
    assert info.backend == backend
    assert info.supported_components == frozenset({"service_store"})

    first_id = UUID(int=1)
    second_id = UUID(int=2)
    first = catalog.allocate(first_id)
    assert first == ServiceStoreLocator.for_service(backend, first_id)  # type: ignore[arg-type]
    assert catalog.allocate(first_id) == first
    second = catalog.allocate(second_id)
    assert second == ServiceStoreLocator.for_service(backend, second_id)  # type: ignore[arg-type]
    assert second != first

    expected_uninitialised = MigrationState("service_store", 0, 1, "uninitialised")
    expected_ready = MigrationState("service_store", 1, 1, "ready")
    assert catalog.inspect_migration(first) == expected_uninitialised
    assert catalog.inspect_migration(second) == expected_uninitialised
    assert catalog.migrate(first) == expected_ready
    assert catalog.inspect_migration(first) == expected_ready
    assert catalog.migrate(first) == expected_ready
    assert catalog.inspect_migration(second) == expected_uninitialised

    restarted = factory()
    assert restarted.inspect_migration(first) == expected_ready
    assert restarted.inspect_migration(second) == expected_uninitialised

    other_backend = "postgresql" if backend == "sqlite" else "sqlite"
    mismatch = ServiceStoreLocator(other_backend, first.namespace)  # type: ignore[arg-type]

    class LocatorSubclass(ServiceStoreLocator):
        pass

    subclassed = LocatorSubclass(first.backend, first.namespace)
    for operation in (catalog.inspect_migration, catalog.migrate):
        _expect(StorageContractError, lambda current=operation: current(mismatch))
        _expect(StorageContractError, lambda current=operation: current(subclassed))
        _expect(
            StorageContractError,
            lambda current=operation: current(cast(ServiceStoreLocator, object())),
        )

    for invalid_id in (UUID(int=0), "not-a-uuid", True):
        _expect(
            StorageContractError,
            lambda current=invalid_id: catalog.allocate(cast(UUID, current)),
        )


def assert_registry_application_conformance(factory: RegistryHarnessFactory) -> None:
    """Assert behaviour that SQLite and PostgreSQL adapters must later share."""

    harness = factory()
    context = ApplicationContext("contract-principal", "storage-contract")
    command = SeedCommand("notes", "project", "Contract notes", "seed-1")
    created = harness.application(UUID(int=1)).seed(context, command)
    replay = harness.restart(UUID(int=2)).seed(context, command)
    assert replay.model_dump(mode="json", by_alias=True) == created.model_dump(
        mode="json", by_alias=True
    )
    assert harness.restart().inspect(context, created.service_id).service_id == created.service_id
    assert harness.traces()[-1].calls == ["get", "close"]

    duplicate = harness.restart(UUID(int=2)).seed(
        context,
        SeedCommand("notes", "project", "Contract notes", "seed-2"),
    )
    assert duplicate.service_id == created.service_id

    requested = ManifestV2.model_validate(valid_manifest())
    designed = harness.restart().design(
        context,
        DesignCommand(created.service_id, requested, "design-1"),
    )
    expected_design = designed.model_dump(mode="json", by_alias=True)
    requested.entities[0].description = "caller mutation"
    designed.schema_.entities[0].description = "result mutation"
    assert (
        harness.restart()
        .inspect(context, created.service_id)
        .model_dump(mode="json", by_alias=True)
        == expected_design
    )
    design_replay = harness.restart().design(
        context,
        DesignCommand(
            created.service_id,
            ManifestV2.model_validate(valid_manifest()),
            "design-1",
        ),
    )
    assert design_replay.model_dump(mode="json", by_alias=True) == expected_design
    design_replay.schema_.entities[0].description = "replay mutation"
    listed = harness.restart().list(context)
    listed[0].schema_.entities[0].description = "list mutation"
    assert (
        harness.restart()
        .inspect(context, created.service_id)
        .model_dump(mode="json", by_alias=True)
        == expected_design
    )

    _expect(
        Conflict,
        lambda: harness.restart().seed(
            context,
            SeedCommand("different", "project", "Changed payload", "seed-1"),
        ),
    )

    other = ApplicationContext("other-principal", "storage-contract")
    other_created = harness.restart(UUID(int=3)).seed(other, command)
    assert other_created.service_id != created.service_id
    assert [item.service_id for item in harness.restart().list(context)] == [created.service_id]
    assert [item.service_id for item in harness.restart().list(other)] == [other_created.service_id]

    harness.restart(UUID(int=4)).seed(
        context,
        SeedCommand("zeta", "project", "Zeta", "seed-zeta"),
    )
    harness.restart(UUID(int=2)).seed(
        context,
        SeedCommand("alpha", "project", "Alpha", "seed-alpha"),
    )
    assert [item.service_id for item in harness.restart().list(context)] == [
        UUID(int=1),
        UUID(int=2),
        UUID(int=4),
    ]
    assert all(trace.close_count == 1 for trace in harness.traces())

    for boundary in ("claim", "find_domain", "add", "append", "complete"):
        failed = factory()
        failed.fail(boundary)
        captured = _expect(
            InternalFailure,
            lambda current=failed: current.application(UUID(int=5)).seed(context, command),
        )
        assert "secret" not in str(captured)
        failed_trace = failed.traces()[-1]
        failed.fail(None)
        assert failed.restart().list(context) == []
        assert failed_trace.calls[-2:] == ["rollback", "close"]
        assert failed_trace.close_count == 1

    uncertain = factory()
    uncertain.fail("commit")
    _expect(InternalFailure, lambda: uncertain.application(UUID(int=6)).seed(context, command))
    assert uncertain.traces()[-1].calls[-3:] == ["commit", "rollback", "close"]
    uncertain.fail(None)
    retried = uncertain.restart(UUID(int=6)).seed(context, command)
    assert uncertain.restart().inspect(context, retried.service_id).service_id == retried.service_id

    close_failed = factory()
    close_failed.fail("close")
    _expect(InternalFailure, lambda: close_failed.application(UUID(int=7)).seed(context, command))
    assert close_failed.traces()[-1].close_count == 1
    close_failed.fail(None)
    close_replay = close_failed.restart(UUID(int=8)).seed(context, command)
    assert [item.service_id for item in close_failed.restart().list(context)] == [
        close_replay.service_id
    ]


def _expect(error_type: type[Exception], operation: Callable[[], object]) -> Exception:
    try:
        operation()
    except error_type as error:
        assert error.__cause__ is None
        assert error.__context__ is None
        return error
    except Exception as error:
        raise AssertionError(
            f"Expected {error_type.__name__}, got {type(error).__name__}."
        ) from None
    raise AssertionError(f"Expected {error_type.__name__}.")
