"""Public backend-neutral contracts; private adapters live in implementation subpackages."""

from .contracts import (
    DomainSchemaState,
    DomainSchemaStatus,
    DomainSchemaStore,
    MigrationState,
    RegistryAdapter,
    ServiceStoreCatalog,
    ServiceStoreLocator,
    StorageAdapterInfo,
    StorageBackend,
    StorageComponent,
)
from .errors import StorageContractError, StorageError, StorageMigrationError, StorageUnavailable

__all__ = [
    "DomainSchemaState",
    "DomainSchemaStatus",
    "DomainSchemaStore",
    "MigrationState",
    "RegistryAdapter",
    "ServiceStoreCatalog",
    "ServiceStoreLocator",
    "StorageAdapterInfo",
    "StorageBackend",
    "StorageComponent",
    "StorageContractError",
    "StorageError",
    "StorageMigrationError",
    "StorageUnavailable",
]
