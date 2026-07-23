"""Minimal storage vocabulary shared by future reference adapters.

These values deliberately describe logical backend state only. They do not
accept or retain a filesystem path, connection location, credential, SQL
identifier, driver object, or arbitrary plugin name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from aipcs_mcp.application.ports import RegistryUnitOfWork
from aipcs_mcp.relational import RelationalSpecification, RelationalTransition

from .errors import StorageContractError

StorageBackend = Literal["sqlite", "postgresql"]
StorageComponent = Literal["registry", "service_store"]
MigrationStatus = Literal["uninitialised", "ready", "incompatible", "dirty"]
DomainSchemaStatus = Literal["unmaterialised", "ready", "incompatible"]

_BACKENDS = frozenset({"sqlite", "postgresql"})
_COMPONENTS = frozenset({"registry", "service_store"})
_STATUSES = frozenset({"uninitialised", "ready", "incompatible", "dirty"})
_DOMAIN_SCHEMA_STATUSES = frozenset({"unmaterialised", "ready", "incompatible"})
_LOCATOR_NAMESPACE = re.compile(r"^svc_[0-9a-f]{32}$")


@dataclass(frozen=True)
class ServiceStoreLocator:
    """Opaque logical allocation for one materialised service store."""

    backend: StorageBackend
    namespace: str

    def __post_init__(self) -> None:
        _validate_backend(self.backend)
        if (
            type(self.namespace) is not str
            or not _LOCATOR_NAMESPACE.fullmatch(self.namespace)
            or self.namespace == f"svc_{'0' * 32}"
        ):
            raise StorageContractError()

    @classmethod
    def for_service(cls, backend: StorageBackend, service_id: UUID) -> ServiceStoreLocator:
        if not isinstance(service_id, UUID) or service_id.int == 0:
            raise StorageContractError()
        return cls(backend=backend, namespace=f"svc_{service_id.hex}")


@dataclass(frozen=True)
class StorageAdapterInfo:
    """Static adapter ownership, deliberately distinct from migration readiness."""

    backend: StorageBackend
    supported_components: frozenset[StorageComponent]

    def __post_init__(self) -> None:
        _validate_backend(self.backend)
        if type(self.supported_components) is not frozenset or not self.supported_components:
            raise StorageContractError()
        if any(
            type(component) is not str or component not in _COMPONENTS
            for component in self.supported_components
        ):
            raise StorageContractError()


@dataclass(frozen=True)
class MigrationState:
    """Read-only migration readiness for one adapter-owned storage component."""

    component: StorageComponent
    applied_revision: int
    target_revision: int
    status: MigrationStatus

    def __post_init__(self) -> None:
        if type(self.component) is not str or self.component not in _COMPONENTS:
            raise StorageContractError()
        if not _positive_revision(self.target_revision):
            raise StorageContractError()
        if not _revision(self.applied_revision):
            raise StorageContractError()
        if type(self.status) is not str or self.status not in _STATUSES:
            raise StorageContractError()
        if self.status == "uninitialised" and self.applied_revision != 0:
            raise StorageContractError()
        if self.status == "ready" and self.applied_revision != self.target_revision:
            raise StorageContractError()


@dataclass(frozen=True)
class DomainSchemaState:
    """Physical schema fidelity relative to one supplied relational specification."""

    status: DomainSchemaStatus

    def __post_init__(self) -> None:
        if type(self.status) is not str or self.status not in _DOMAIN_SCHEMA_STATUSES:
            raise StorageContractError()


class RegistryAdapter(Protocol):
    """Registry owner; info includes registry and migration states are registry-only."""

    def info(self) -> StorageAdapterInfo: ...

    def inspect_migration(self) -> MigrationState: ...

    def migrate(self) -> MigrationState: ...

    def open_uow(self) -> RegistryUnitOfWork: ...


class ServiceStoreCatalog(Protocol):
    """Service-store owner; locators and migration states match its backend/component."""

    def info(self) -> StorageAdapterInfo: ...

    def allocate(self, service_id: UUID) -> ServiceStoreLocator: ...

    def inspect_migration(self, locator: ServiceStoreLocator) -> MigrationState: ...

    def migrate(self, locator: ServiceStoreLocator) -> MigrationState: ...


class DomainSchemaStore(Protocol):
    """Private relational schema boundary with no lifecycle or location mechanics."""

    def inspect(
        self,
        locator: ServiceStoreLocator,
        specification: RelationalSpecification,
    ) -> DomainSchemaState: ...

    def materialise(
        self,
        locator: ServiceStoreLocator,
        specification: RelationalSpecification,
    ) -> DomainSchemaState: ...

    def evolve(
        self,
        locator: ServiceStoreLocator,
        transition: RelationalTransition,
    ) -> DomainSchemaState: ...


def _validate_backend(backend: object) -> None:
    if type(backend) is not str or backend not in _BACKENDS:
        raise StorageContractError()


def _positive_revision(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _revision(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
