"""The sole production composition root for configuration, storage, and MCP."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from mcp.server.lowlevel import Server

from .application.ports import Clock, IdProvider
from .application.services import ServiceApplication
from .configuration.models import ResolvedConfiguration
from .configuration.resolver import is_supported_sqlite_platform, is_supported_sqlite_runtime
from .mcp_server import create_server
from .storage.contracts import MigrationState
from .storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter


class SystemUtcClock(Clock):
    """Production clock with timezone-aware UTC values only."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class Uuid4ServiceIds(IdProvider):
    """Production UUID provider; application policy validates generated values."""

    def new_service_id(self) -> UUID:
        return uuid4()


def compose_server(config: ResolvedConfiguration) -> Server:
    """Compose a finite server from one resolved snapshot and one ready registry."""

    if config.profile == "stateless":
        return create_server()
    if config.profile == "sqlite" and (
        not is_supported_sqlite_platform() or not is_supported_sqlite_runtime()
    ):
        raise RuntimeError("Unsupported runtime profile.")
    if config.profile != "sqlite" or config.principal_id is None or config.sqlite_data_root is None:
        raise RuntimeError("Unsupported runtime profile.")
    location = SQLiteLocationPolicy.from_resolved(
        config.sqlite_data_root, config.sources["sqlite_data_root"]
    )
    adapter = SQLiteRegistryAdapter(location, busy_timeout_ms=config.sqlite_busy_timeout_ms)
    state = adapter.migrate()
    if not _ready_registry(state):
        raise RuntimeError("Registry is not ready.")
    application = ServiceApplication(adapter.open_uow, SystemUtcClock(), Uuid4ServiceIds())
    return create_server(
        application=application, principal_id=config.principal_id, registry_lifecycle=True
    )


def _ready_registry(state: MigrationState) -> bool:
    return (
        state.component == "registry"
        and state.status == "ready"
        and state.applied_revision == state.target_revision
    )
