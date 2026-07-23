"""PostgreSQL registry adapter and transactional R1 migration."""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from aipcs_mcp.application.ports import RegistryUnitOfWork
from aipcs_mcp.storage.codecs import encode_time
from aipcs_mcp.storage.contracts import MigrationState, StorageAdapterInfo
from aipcs_mcp.storage.errors import StorageBusy, StorageMigrationError, StorageUnavailable

from .connection import (
    PostgreSQLConnectionPolicy,
    PostgreSQLDsn,
    connect_postgresql,
)
from .registry_inspection import inspect_registry
from .registry_migrations import (
    ADAPTER_ID,
    CHECKSUM,
    DDL,
    MIGRATION_ID,
    SCHEMA,
    TARGET_REVISION,
)
from .registry_uow import PostgreSQLRegistryUnitOfWork
from .result_codes import is_postgresql_busy


class PostgreSQLRegistryAdapter:
    """Registry owner for one operator-provisioned PostgreSQL database."""

    def __init__(self, dsn: PostgreSQLDsn, policy: PostgreSQLConnectionPolicy) -> None:
        if (
            type(dsn) is not PostgreSQLDsn
            or type(policy) is not PostgreSQLConnectionPolicy
            or policy.schema != SCHEMA
        ):
            raise StorageUnavailable()
        self._dsn = dsn
        self._policy = policy

    def __repr__(self) -> str:
        return "PostgreSQLRegistryAdapter(<redacted>)"

    def info(self) -> StorageAdapterInfo:
        return StorageAdapterInfo("postgresql", frozenset({"registry"}))

    def inspect_migration(self) -> MigrationState:
        connection: object | None = None
        try:
            connection = self._connect()
            _execute(connection, "BEGIN TRANSACTION READ ONLY")
            state = inspect_registry(connection)
            _rollback(connection)
            _close_strict(connection)
            connection = None
            return state
        except (StorageBusy, StorageUnavailable, StorageMigrationError) as error:
            raise _bounded(error, phase="read") from None
        except Exception as error:
            raise _bounded(error, phase="read") from None
        finally:
            _close_safely(connection)

    def migrate(self) -> MigrationState:
        connection: object | None = None
        phase = "read"
        try:
            connection = self._connect()
            _execute(connection, "BEGIN TRANSACTION")
            try:
                _execute(
                    connection,
                    "SELECT pg_catalog.pg_advisory_xact_lock("
                    "pg_catalog.hashtext('aipcs.registry'),"
                    "pg_catalog.hashtext('migration'))",
                ).fetchone()
            except Exception as error:
                raise _bounded(error, phase="read") from None

            state = inspect_registry(connection)
            if state.status == "ready":
                _rollback(connection)
                _close_strict(connection)
                connection = None
                return state
            if state.status != "uninitialised":
                _rollback(connection)
                _close_strict(connection)
                connection = None
                return state

            for statement in DDL:
                _execute(connection, statement)
            _execute(
                connection,
                'INSERT INTO "aipcs_registry"."aipcs_registry_meta" '
                '("singleton","adapter_id","component","applied_revision","dirty") '
                "VALUES (%s,%s,%s,%s,%s)",
                (1, ADAPTER_ID, "registry", TARGET_REVISION, False),
            )
            _execute(
                connection,
                'INSERT INTO "aipcs_registry"."aipcs_registry_migration" '
                '("component","revision","migration_id","checksum","applied_at") '
                "VALUES (%s,%s,%s,%s,%s)",
                (
                    "registry",
                    TARGET_REVISION,
                    MIGRATION_ID,
                    CHECKSUM,
                    encode_time(datetime.now(UTC)),
                ),
            )
            phase = "commit"
            _commit(connection)
            _close_strict(connection)
            connection = None
            return self.inspect_migration()
        except (StorageBusy, StorageUnavailable, StorageMigrationError) as error:
            raise _bounded(error, phase=phase) from None
        except Exception as error:
            raise _bounded(error, phase=phase) from None
        finally:
            _close_safely(connection)

    def open_uow(self) -> RegistryUnitOfWork:
        connection: object | None = None
        try:
            connection = self._connect()
            _execute(connection, "BEGIN TRANSACTION READ ONLY")
            if inspect_registry(connection).status != "ready":
                raise StorageMigrationError()
            _rollback(connection)
            result = PostgreSQLRegistryUnitOfWork(connection)
            connection = None
            return result
        except (StorageBusy, StorageUnavailable, StorageMigrationError) as error:
            raise _bounded(error, phase="read") from None
        except Exception as error:
            raise _bounded(error, phase="read") from None
        finally:
            _close_safely(connection)

    def _connect(self) -> object:
        try:
            return connect_postgresql(self._dsn, self._policy)
        except StorageUnavailable:
            raise StorageUnavailable() from None
        except Exception:
            raise StorageUnavailable() from None


def _bounded(error: BaseException, *, phase: str) -> Exception:
    if phase == "commit":
        return StorageMigrationError()
    if isinstance(error, StorageUnavailable) or _is_connection_error(error):
        return StorageUnavailable()
    if isinstance(error, StorageBusy) or is_postgresql_busy(error):
        return StorageBusy()
    return StorageMigrationError()


def _is_connection_error(error: BaseException) -> bool:
    state = getattr(error, "sqlstate", None)
    return type(state) is str and state.startswith("08")


def _execute(connection: object, sql: str, params: tuple[object, ...] = ()) -> Any:
    execute = getattr(connection, "execute", None)
    if not callable(execute):
        raise StorageMigrationError()
    return execute(sql, params)


def _commit(connection: object) -> None:
    commit = getattr(connection, "commit", None)
    if not callable(commit):
        raise StorageMigrationError()
    commit()


def _rollback(connection: object) -> None:
    rollback = getattr(connection, "rollback", None)
    if not callable(rollback):
        raise StorageMigrationError()
    rollback()


def _close_strict(connection: object) -> None:
    close = getattr(connection, "close", None)
    if not callable(close):
        raise StorageMigrationError()
    close()


def _close_safely(connection: object | None) -> None:
    if connection is not None:
        with suppress(Exception):
            _close_strict(connection)
