"""Private SQLite service-store catalogue; uncomposed by the public runtime."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from functools import wraps
from uuid import UUID

from aipcs_mcp.storage.contracts import (
    MigrationState,
    ServiceStoreLocator,
    StorageAdapterInfo,
)
from aipcs_mcp.storage.errors import StorageContractError, StorageMigrationError, StorageUnavailable

from . import service_store_inspection
from .codecs import encode_time
from .connection import connect
from .location import AnchoredLocation, SQLiteLocationPolicy, create_database
from .service_store_migrations import (
    CHECKSUM,
    DDL,
    META,
    MIGRATION,
    MIGRATION_ID,
    TARGET_REVISION,
)


def _bounded[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return method(*args, **kwargs)
        except StorageContractError:
            raise
        except StorageUnavailable:
            failure = StorageUnavailable()
        except StorageMigrationError:
            failure = StorageMigrationError()
        except Exception:
            failure = StorageMigrationError()
        raise failure from None

    return wrapped


class SQLiteServiceStoreCatalog:
    """Own opaque per-service database migration state, not domain materialisation."""

    def __init__(self, location: SQLiteLocationPolicy) -> None:
        if not isinstance(location, SQLiteLocationPolicy):
            raise StorageUnavailable()
        self._location = location

    def __repr__(self) -> str:
        return "SQLiteServiceStoreCatalog(<redacted>)"

    def info(self) -> StorageAdapterInfo:
        return StorageAdapterInfo("sqlite", frozenset({"service_store"}))

    def allocate(self, service_id: UUID) -> ServiceStoreLocator:
        return ServiceStoreLocator.for_service("sqlite", service_id)

    @_bounded
    def inspect_migration(self, locator: ServiceStoreLocator) -> MigrationState:
        namespace = _namespace(locator)
        anchored: AnchoredLocation | None = None
        connection: sqlite3.Connection | None = None
        try:
            anchored = self._location.acquire_service_store(
                namespace, create=False, allow_journal=False
            )
            if anchored is None or anchored._database_stat is None:
                return _state(0, "uninitialised")
            if anchored._database_stat.st_size == 0:
                return _state(0, "uninitialised")
            connection = connect(anchored, "ro", query_only=True)
            return service_store_inspection.inspect_connection(connection, namespace, integrity=True)
        finally:
            _close(connection, anchored)

    @_bounded
    def migrate(self, locator: ServiceStoreLocator) -> MigrationState:
        namespace = _namespace(locator)
        anchored: AnchoredLocation | None = None
        connection: sqlite3.Connection | None = None
        try:
            anchored = self._location.acquire_service_store(
                namespace, create=True, allow_journal=True
            )
            if anchored is None:
                raise StorageUnavailable()
            if anchored.journal_present:
                if anchored._database_stat is None:
                    raise StorageUnavailable()
                connection = connect(anchored, "rw", query_only=False)
                connection.execute("SELECT 1").fetchone()
                connection.close()
                connection = None
                anchored.close()
                anchored = None
                state = self.inspect_migration(locator)
                if state.status != "uninitialised":
                    return state
                anchored = self._location.acquire_service_store(
                    namespace, create=True, allow_journal=False
                )
                if anchored is None:
                    raise StorageUnavailable()
            if anchored._database_stat is None:
                anchored._database_stat = create_database(
                    anchored._root_fd, anchored._database_name
                )
            elif anchored._database_stat.st_size:
                connection = connect(anchored, "rw", query_only=False)
                state = service_store_inspection.inspect_connection(
                    connection, namespace, integrity=True
                )
                connection.close()
                connection = None
                if state.status != "uninitialised":
                    return state
            connection = connect(anchored, "rw", query_only=False)
            connection.execute("BEGIN EXCLUSIVE")
            for statement in DDL:
                connection.execute(statement)
            connection.execute(
                f'INSERT INTO "{META}" VALUES (1,?,?,?,?,?)',
                ("aipcs.sqlite.service_store", "service_store", namespace, 1, 1),
            )
            connection.execute(
                f'INSERT INTO "{MIGRATION}" VALUES (?,?,?,?,?)',
                ("service_store", 1, MIGRATION_ID, CHECKSUM, encode_time(datetime.now(UTC))),
            )
            connection.execute(f'UPDATE "{META}" SET "dirty"=0 WHERE "singleton"=1')
            if (
                service_store_inspection.inspect_connection(
                    connection, namespace, integrity=True
                ).status
                != "ready"
            ):
                raise StorageMigrationError()
            anchored.verify_database_identity()
            connection.commit()
            connection.close()
            connection = None
            anchored.verify_database_identity()
            anchored.close()
            anchored = None
            return self.inspect_migration(locator)
        finally:
            _close(connection, anchored)


def _namespace(locator: ServiceStoreLocator) -> str:
    if type(locator) is not ServiceStoreLocator or locator.backend != "sqlite":
        raise StorageContractError()
    try:
        checked = ServiceStoreLocator("sqlite", locator.namespace)
    except Exception:
        raise StorageContractError() from None
    return checked.namespace


def _state(revision: int, status: str) -> MigrationState:
    return MigrationState("service_store", revision, TARGET_REVISION, status)  # type: ignore[arg-type]


def _close(connection: sqlite3.Connection | None, location: AnchoredLocation | None) -> None:
    if connection is not None:
        with suppress(Exception):
            connection.close()
    if location is not None:
        with suppress(Exception):
            location.close()
