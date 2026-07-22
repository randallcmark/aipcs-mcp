"""Backend-neutral storage contracts; no adapter implementation is supplied."""

from .contracts import (
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
