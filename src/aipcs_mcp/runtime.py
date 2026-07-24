"""The sole production composition root for configuration, storage, and MCP."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from mcp.server.lowlevel import Server

from .application.admin import (
    AdminInspectionApplication,
    AdminInspectionUnavailable,
    MigrationObservation,
)
from .application.data import DataApplication
from .application.models import ApplicationContext, MaterialisationStorage
from .application.ports import Clock, IdProvider
from .application.registry_authority import StorageBackend
from .application.services import ServiceApplication
from .configuration.models import ResolvedConfiguration
from .configuration.resolver import is_supported_sqlite_platform, is_supported_sqlite_runtime
from .lifecycle_coordinator import LifecycleCoordinator
from .mcp_server import create_server
from .portable_coordinator import PortableCoordinator
from .records import DataFailure, RecordSpecification
from .relational import compile_manifest
from .storage.contracts import (
    DomainSchemaStore,
    MigrationState,
    RegistryAdapter,
    ServiceStoreCatalog,
    ServiceStoreLocator,
)
from .storage.errors import StorageBusy, StorageMigrationError, StorageUnavailable
from .storage.postgresql import (
    PostgreSQLConnectionPolicy,
    PostgreSQLDomainSchemaStore,
    PostgreSQLMaterialisedDataStore,
    PostgreSQLRegistryAdapter,
    PostgreSQLServiceStoreCatalog,
    resolve_postgresql_dsn_for_serve,
)
from .storage.postgresql.portable import PostgreSQLPortableServiceStore
from .storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter, SQLiteServiceStoreCatalog
from .storage.sqlite.data_store import SQLiteMaterialisedDataStore
from .storage.sqlite.domain_schema import SQLiteDomainSchemaStore
from .storage.sqlite.portable import SQLitePortableServiceStore


class SQLiteAdmittedDataStore:
    """Production bridge from a detached materialisation allocation to SQLite.

    The bridge owns no registry unit of work and performs no storage I/O while
    composed.  It simply invokes one explicit method on the service-local
    adapter after ``DataApplication`` has completed fresh registry admission.
    """

    def __init__(self, store: SQLiteMaterialisedDataStore) -> None:
        self._store = store

    def execute(
        self,
        operation: str,
        storage: MaterialisationStorage,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        value: object,
    ) -> object:
        if type(storage) is not MaterialisationStorage or storage.backend != "sqlite":
            return DataFailure("storage_unavailable")
        try:
            locator = ServiceStoreLocator("sqlite", storage.namespace)
            method = getattr(self._store, operation, None)
            if not callable(method):
                return DataFailure("internal_error")
            return method(locator, principal_id, created_via, specification, value)
        except Exception:
            return DataFailure("internal_error")


class PostgreSQLAdmittedDataStore:
    """Production bridge from a detached materialisation allocation to PostgreSQL.

    As with the SQLite bridge, registry admission remains complete before this
    bridge opens a service-local connection.  The opaque locator prevents the
    data adapter from accepting a cross-backend allocation.
    """

    def __init__(self, store: PostgreSQLMaterialisedDataStore) -> None:
        self._store = store

    def execute(
        self,
        operation: str,
        storage: MaterialisationStorage,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        value: object,
    ) -> object:
        if type(storage) is not MaterialisationStorage or storage.backend != "postgresql":
            return DataFailure("storage_unavailable")
        try:
            locator = ServiceStoreLocator("postgresql", storage.namespace)
            method = getattr(self._store, operation, None)
            if not callable(method):
                return DataFailure("internal_error")
            return method(locator, principal_id, created_via, specification, value)
        except Exception:
            return DataFailure("internal_error")


class SystemUtcClock(Clock):
    """Production clock with timezone-aware UTC values only."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class Uuid4ServiceIds(IdProvider):
    """Production UUID provider; application policy validates generated values."""

    def new_service_id(self) -> UUID:
        return uuid4()


@dataclass(frozen=True, slots=True)
class AdminRuntime:
    """One resolved read-only administration composition."""

    inspection: AdminInspectionApplication
    context: ApplicationContext


class AdapterAdminStorageInspector:
    """Translate concrete adapter inspections into application-owned facts."""

    def __init__(
        self,
        backend: StorageBackend,
        registry: RegistryAdapter,
        catalog: ServiceStoreCatalog,
        domain: DomainSchemaStore,
    ) -> None:
        self._backend = backend
        self._registry = registry
        self._catalog = catalog
        self._domain = domain

    def inspect_registry(self) -> MigrationObservation:
        return self._migration("registry", self._registry.inspect_migration)

    def inspect_service(
        self, storage: MaterialisationStorage
    ) -> MigrationObservation:
        locator = self._locator(storage)
        return self._migration(
            "service_store", lambda: self._catalog.inspect_migration(locator)
        )

    def inspect_domain(
        self, storage: MaterialisationStorage, manifest: object
    ) -> str:
        locator = self._locator(storage)
        try:
            state = self._domain.inspect(locator, compile_manifest(manifest))  # type: ignore[arg-type]
        except StorageBusy:
            raise AdminInspectionUnavailable("storage_busy") from None
        except (StorageUnavailable, StorageMigrationError):
            raise AdminInspectionUnavailable("storage_unavailable") from None
        except Exception:
            raise AdminInspectionUnavailable("internal_error") from None
        return state.status

    def _migration(self, component: str, inspect: object) -> MigrationObservation:
        if not callable(inspect):
            raise AdminInspectionUnavailable("internal_error")
        try:
            state = inspect()
        except StorageBusy:
            raise AdminInspectionUnavailable("storage_busy") from None
        except (StorageUnavailable, StorageMigrationError):
            raise AdminInspectionUnavailable("storage_unavailable") from None
        except Exception:
            raise AdminInspectionUnavailable("internal_error") from None
        if (
            type(state) is not MigrationState
            or state.component != component
            or state.status not in {
                "uninitialised",
                "outdated",
                "ready",
                "incompatible",
                "dirty",
            }
        ):
            raise AdminInspectionUnavailable("internal_error")
        return MigrationObservation(
            state.component,
            state.status,
            state.applied_revision,
            state.target_revision,
        )

    def _locator(self, storage: MaterialisationStorage) -> ServiceStoreLocator:
        if (
            type(storage) is not MaterialisationStorage
            or storage.backend != self._backend.value
        ):
            raise AdminInspectionUnavailable("internal_error")
        return ServiceStoreLocator(storage.backend, storage.namespace)


def compose_admin_runtime(
    config: ResolvedConfiguration, *, environ: Mapping[str, str] | None = None
) -> AdminRuntime:
    """Compose read-only administration without migration or service-store writes."""

    if type(config) is not ResolvedConfiguration:
        raise RuntimeError("Administration configuration is invalid.")
    if config.profile == "stateless":
        return AdminRuntime(
            AdminInspectionApplication("stateless", None, None, None),
            ApplicationContext("stateless", "cli"),
        )
    if config.principal_id is None:
        raise RuntimeError("Administration principal is unavailable.")
    if config.profile == "sqlite":
        if (
            config.sqlite_data_root is None
            or not is_supported_sqlite_platform()
            or not is_supported_sqlite_runtime()
        ):
            raise RuntimeError("Unsupported runtime profile.")
        location = SQLiteLocationPolicy.from_resolved(
            config.sqlite_data_root, config.sources["sqlite_data_root"]
        )
        registry = SQLiteRegistryAdapter(
            location, busy_timeout_ms=config.sqlite_busy_timeout_ms
        )
        catalog = SQLiteServiceStoreCatalog(
            location, busy_timeout_ms=config.sqlite_busy_timeout_ms
        )
        domain = SQLiteDomainSchemaStore(
            location, busy_timeout_ms=config.sqlite_busy_timeout_ms
        )
        clock = SystemUtcClock()
        services = ServiceApplication(registry.open_uow, clock, Uuid4ServiceIds())
        store = SQLiteMaterialisedDataStore(
            location,
            busy_timeout_ms=config.sqlite_busy_timeout_ms,
            clock=clock.now,
            record_ids=uuid4,
            branch_ids=uuid4,
        )
        data = DataApplication(registry.open_uow, SQLiteAdmittedDataStore(store))
        inspection = AdapterAdminStorageInspector(
            StorageBackend.SQLITE, registry, catalog, domain
        )
        return AdminRuntime(
            AdminInspectionApplication(
                "sqlite", inspection, services, data
            ),
            ApplicationContext(config.principal_id, "cli"),
        )
    if config.profile == "postgresql":
        try:
            environment = os.environ if environ is None else environ
            dsn = resolve_postgresql_dsn_for_serve(config, environment)
            policy = PostgreSQLConnectionPolicy.from_configuration(config)
            registry = PostgreSQLRegistryAdapter(dsn, policy)
            catalog = PostgreSQLServiceStoreCatalog(
                dsn,
                connect_timeout_seconds=config.postgres_connect_timeout_seconds,
                lock_timeout_ms=config.postgres_lock_timeout_ms,
                statement_timeout_ms=config.postgres_statement_timeout_ms,
            )
            domain = PostgreSQLDomainSchemaStore(
                dsn,
                connect_timeout_seconds=config.postgres_connect_timeout_seconds,
                lock_timeout_ms=config.postgres_lock_timeout_ms,
                statement_timeout_ms=config.postgres_statement_timeout_ms,
                service_store_catalog=catalog,
            )
            clock = SystemUtcClock()
            services = ServiceApplication(registry.open_uow, clock, Uuid4ServiceIds())
            store = PostgreSQLMaterialisedDataStore(
                dsn,
                connect_timeout_seconds=config.postgres_connect_timeout_seconds,
                lock_timeout_ms=config.postgres_lock_timeout_ms,
                statement_timeout_ms=config.postgres_statement_timeout_ms,
                service_store_catalog=catalog,
                domain_schema_store=domain,
                clock=clock.now,
                record_ids=uuid4,
                branch_ids=uuid4,
            )
            data = DataApplication(registry.open_uow, PostgreSQLAdmittedDataStore(store))
            inspection = AdapterAdminStorageInspector(
                StorageBackend.POSTGRESQL, registry, catalog, domain
            )
            return AdminRuntime(
                AdminInspectionApplication(
                    "postgresql", inspection, services, data
                ),
                ApplicationContext(config.principal_id, "cli"),
            )
        except Exception:
            raise RuntimeError(
                "PostgreSQL administration runtime could not be composed."
            ) from None
    raise RuntimeError("Unsupported runtime profile.")


def compose_server(
    config: ResolvedConfiguration, *, environ: Mapping[str, str] | None = None
) -> Server:
    """Compose a finite server from one resolved snapshot and one ready registry."""

    if config.profile == "stateless":
        return create_server()
    if config.profile == "sqlite" and (
        not is_supported_sqlite_platform() or not is_supported_sqlite_runtime()
    ):
        raise RuntimeError("Unsupported runtime profile.")
    if config.profile == "postgresql":
        return _compose_postgresql(config, environ=os.environ if environ is None else environ)
    if config.profile != "sqlite" or config.principal_id is None or config.sqlite_data_root is None:
        raise RuntimeError("Unsupported runtime profile.")
    location = SQLiteLocationPolicy.from_resolved(
        config.sqlite_data_root, config.sources["sqlite_data_root"]
    )
    adapter = SQLiteRegistryAdapter(location, busy_timeout_ms=config.sqlite_busy_timeout_ms)
    state = adapter.migrate()
    if not _ready_registry(state):
        raise RuntimeError("Registry is not ready.")
    clock = SystemUtcClock()
    application = ServiceApplication(adapter.open_uow, clock, Uuid4ServiceIds())
    catalog = SQLiteServiceStoreCatalog(location, busy_timeout_ms=config.sqlite_busy_timeout_ms)
    domain = SQLiteDomainSchemaStore(location, busy_timeout_ms=config.sqlite_busy_timeout_ms)
    coordinator = LifecycleCoordinator(adapter.open_uow, clock, catalog, domain)
    data_store = SQLiteMaterialisedDataStore(
        location,
        busy_timeout_ms=config.sqlite_busy_timeout_ms,
        clock=clock.now,
        record_ids=uuid4,
        branch_ids=uuid4,
    )
    data_application = DataApplication(adapter.open_uow, SQLiteAdmittedDataStore(data_store))
    return create_server(
        application=application,
        principal_id=config.principal_id,
        registry_lifecycle=True,
        lifecycle_executor=coordinator,
        data_application=data_application,
    )


def _compose_postgresql(config: ResolvedConfiguration, *, environ: Mapping[str, str]) -> Server:
    """Compose one homogeneous, ready PostgreSQL runtime without exposing its DSN.

    This is intentionally the only production boundary that resolves the
    environment-variable reference.  All child adapters receive the same
    redacted in-memory descriptor, but retain their own connections.
    """

    if config.principal_id is None:
        raise RuntimeError("Unsupported runtime profile.")
    try:
        dsn = resolve_postgresql_dsn_for_serve(config, environ)
        policy = PostgreSQLConnectionPolicy.from_configuration(config)
        adapter = PostgreSQLRegistryAdapter(dsn, policy)
        state = adapter.migrate()
        if not _ready_registry(state):
            raise ValueError("registry is not ready")
        clock = SystemUtcClock()
        application = ServiceApplication(adapter.open_uow, clock, Uuid4ServiceIds())
        catalog = PostgreSQLServiceStoreCatalog(
            dsn,
            connect_timeout_seconds=config.postgres_connect_timeout_seconds,
            lock_timeout_ms=config.postgres_lock_timeout_ms,
            statement_timeout_ms=config.postgres_statement_timeout_ms,
        )
        domain = PostgreSQLDomainSchemaStore(
            dsn,
            connect_timeout_seconds=config.postgres_connect_timeout_seconds,
            lock_timeout_ms=config.postgres_lock_timeout_ms,
            statement_timeout_ms=config.postgres_statement_timeout_ms,
            service_store_catalog=catalog,
        )
        coordinator = LifecycleCoordinator(adapter.open_uow, clock, catalog, domain)
        data_store = PostgreSQLMaterialisedDataStore(
            dsn,
            connect_timeout_seconds=config.postgres_connect_timeout_seconds,
            lock_timeout_ms=config.postgres_lock_timeout_ms,
            statement_timeout_ms=config.postgres_statement_timeout_ms,
            service_store_catalog=catalog,
            domain_schema_store=domain,
            clock=clock.now,
            record_ids=uuid4,
            branch_ids=uuid4,
        )
        data_application = DataApplication(adapter.open_uow, PostgreSQLAdmittedDataStore(data_store))
        return create_server(
            application=application,
            principal_id=config.principal_id,
            registry_lifecycle=True,
            lifecycle_executor=coordinator,
            data_application=data_application,
        )
    except Exception:
        # Driver and connection details, including any DSN content, never
        # escape composition.  The CLI renders one fixed public envelope.
        raise RuntimeError("PostgreSQL runtime could not be composed.") from None


def _ready_registry(state: MigrationState) -> bool:
    return (
        state.component == "registry"
        and state.status == "ready"
        and state.applied_revision == state.target_revision
    )


def _compose_portable_coordinator(
    config: ResolvedConfiguration, *, environ: Mapping[str, str] | None = None
) -> PortableCoordinator:
    """Compose the private V1-10 stream coordinator for exactly one configured backend."""

    clock = SystemUtcClock()
    if config.profile == "sqlite":
        if config.sqlite_data_root is None:
            raise RuntimeError("Unsupported runtime profile.")
        location = SQLiteLocationPolicy.from_resolved(
            config.sqlite_data_root, config.sources["sqlite_data_root"]
        )
        registry = SQLiteRegistryAdapter(
            location, busy_timeout_ms=config.sqlite_busy_timeout_ms
        )
        if not _ready_registry(registry.migrate()):
            raise RuntimeError("Registry is not ready.")
        store = SQLitePortableServiceStore(
            location, busy_timeout_ms=config.sqlite_busy_timeout_ms, clock=clock.now
        )
        return PortableCoordinator(
            registry.open_uow, clock, uuid4, StorageBackend.SQLITE, store
        )
    if config.profile == "postgresql":
        try:
            environment = os.environ if environ is None else environ
            dsn = resolve_postgresql_dsn_for_serve(config, environment)
            policy = PostgreSQLConnectionPolicy.from_configuration(config)
            registry = PostgreSQLRegistryAdapter(dsn, policy)
            if not _ready_registry(registry.migrate()):
                raise ValueError
            catalog = PostgreSQLServiceStoreCatalog(
                dsn,
                connect_timeout_seconds=config.postgres_connect_timeout_seconds,
                lock_timeout_ms=config.postgres_lock_timeout_ms,
                statement_timeout_ms=config.postgres_statement_timeout_ms,
            )
            domain = PostgreSQLDomainSchemaStore(
                dsn,
                connect_timeout_seconds=config.postgres_connect_timeout_seconds,
                lock_timeout_ms=config.postgres_lock_timeout_ms,
                statement_timeout_ms=config.postgres_statement_timeout_ms,
                service_store_catalog=catalog,
            )
            store = PostgreSQLPortableServiceStore(
                dsn,
                connect_timeout_seconds=config.postgres_connect_timeout_seconds,
                lock_timeout_ms=config.postgres_lock_timeout_ms,
                statement_timeout_ms=config.postgres_statement_timeout_ms,
                service_store_catalog=catalog,
                domain_schema_store=domain,
                clock=clock.now,
            )
            return PortableCoordinator(
                registry.open_uow,
                clock,
                uuid4,
                StorageBackend.POSTGRESQL,
                store,
            )
        except Exception:
            raise RuntimeError(
                "PostgreSQL portable runtime could not be composed."
            ) from None
    raise RuntimeError("Unsupported runtime profile.")
