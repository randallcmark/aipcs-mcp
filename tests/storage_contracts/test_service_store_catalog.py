"""SYNTHETIC_FIXTURE. Backend-neutral service-store catalog conformance proof."""

from __future__ import annotations

from uuid import UUID

from aipcs_mcp.storage import (
    MigrationState,
    ServiceStoreLocator,
    StorageAdapterInfo,
    StorageContractError,
)

from .conformance import assert_service_store_catalog_conformance


class ContractServiceStoreCatalog:
    """Deterministic test-only catalog with durable state supplied by its harness."""

    def __init__(self, ready: set[str]) -> None:
        self._ready = ready

    def info(self) -> StorageAdapterInfo:
        return StorageAdapterInfo("sqlite", frozenset({"service_store"}))

    def allocate(self, service_id: UUID) -> ServiceStoreLocator:
        return ServiceStoreLocator.for_service("sqlite", service_id)

    def inspect_migration(self, locator: ServiceStoreLocator) -> MigrationState:
        self._require_locator(locator)
        revision = int(locator.namespace in self._ready)
        status = "ready" if revision else "uninitialised"
        return MigrationState("service_store", revision, 1, status)

    def migrate(self, locator: ServiceStoreLocator) -> MigrationState:
        self._require_locator(locator)
        self._ready.add(locator.namespace)
        return MigrationState("service_store", 1, 1, "ready")

    @staticmethod
    def _require_locator(locator: ServiceStoreLocator) -> None:
        if type(locator) is not ServiceStoreLocator or locator.backend != "sqlite":
            raise StorageContractError()


def test_contract_fake_satisfies_service_store_catalog_conformance() -> None:
    ready: set[str] = set()
    assert_service_store_catalog_conformance(
        lambda: ContractServiceStoreCatalog(ready), backend="sqlite"
    )
