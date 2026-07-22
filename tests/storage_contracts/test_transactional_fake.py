"""SYNTHETIC_FIXTURE

Synthetic fixture provenance: deterministic transactional registry contract harness.
"""

from __future__ import annotations

from application.fakes import FakeRegistry, FixedClock, SequentialIds

from aipcs_mcp.application.ports import RegistryUnitOfWork
from aipcs_mcp.application.services import ServiceApplication
from aipcs_mcp.storage import (
    MigrationState,
    ServiceStoreLocator,
    StorageAdapterInfo,
    StorageContractError,
)

from .conformance import (
    RegistryHarness,
    assert_registry_application_conformance,
    assert_storage_component_ownership,
)


class ContractRegistryAdapter:
    def info(self) -> StorageAdapterInfo:
        return StorageAdapterInfo("sqlite", frozenset({"registry"}))

    def inspect_migration(self) -> MigrationState:
        return MigrationState("registry", 1, 1, "ready")

    def migrate(self) -> MigrationState:
        return self.inspect_migration()

    def open_uow(self) -> RegistryUnitOfWork:
        raise AssertionError("UoW construction is outside the ownership contract test.")


class ContractServiceStoreCatalog:
    def info(self) -> StorageAdapterInfo:
        return StorageAdapterInfo("sqlite", frozenset({"service_store"}))

    def allocate(self, service_id) -> ServiceStoreLocator:
        return ServiceStoreLocator.for_service("sqlite", service_id)

    def inspect_migration(self, locator: ServiceStoreLocator) -> MigrationState:
        self._require_backend(locator)
        return MigrationState("service_store", 0, 1, "uninitialised")

    def migrate(self, locator: ServiceStoreLocator) -> MigrationState:
        self._require_backend(locator)
        return MigrationState("service_store", 1, 1, "ready")

    @staticmethod
    def _require_backend(locator: ServiceStoreLocator) -> None:
        if locator.backend != "sqlite":
            raise StorageContractError()


class TransactionalFakeHarness(RegistryHarness):
    def __init__(self) -> None:
        self.state = FakeRegistry()

    def application(self, *ids) -> ServiceApplication:
        return ServiceApplication(self.state.uow, FixedClock(), SequentialIds(*ids))

    def restart(self, *ids) -> ServiceApplication:
        return self.application(*ids)

    def traces(self):
        return self.state.uows

    def fail(self, boundary: str | None) -> None:
        self.state.fail_at = boundary


def test_deterministic_transactional_fake_satisfies_registry_conformance() -> None:
    assert_registry_application_conformance(TransactionalFakeHarness)


def test_contract_fakes_satisfy_component_and_backend_ownership() -> None:
    assert_storage_component_ownership(ContractRegistryAdapter(), ContractServiceStoreCatalog())
