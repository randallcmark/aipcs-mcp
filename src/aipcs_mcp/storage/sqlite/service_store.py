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

from .codecs import decode_time, encode_time
from .connection import connect
from .location import AnchoredLocation, SQLiteLocationPolicy, create_database
from .service_store_migrations import (
    CHECKSUM,
    DDL,
    EXPECTED_SQL,
    INDEX_LIST,
    INDEX_XINFO,
    META,
    MIGRATION,
    MIGRATION_ID,
    RESERVED_PREFIX,
    TABLE_XINFO,
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
            return _inspect(connection, namespace, integrity=True)
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
                state = _inspect(connection, namespace, integrity=True)
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
            if _inspect(connection, namespace, integrity=True).status != "ready":
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


def _inspect(connection: sqlite3.Connection, namespace: str, *, integrity: bool) -> MigrationState:
    objects = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE substr(name,1,7) <> 'sqlite_'"
    ).fetchall()
    if not objects:
        return _state(0, "uninitialised")
    meta = _bound_meta(connection, objects, namespace)
    revision = meta[0] if meta is not None else 0
    reserved = {row["name"]: row for row in objects if _is_reserved(row["name"])}
    if set(reserved) != set(EXPECTED_SQL):
        return _state(revision, "incompatible")
    if any(
        row["type"] != "table" or row["sql"] != EXPECTED_SQL[name] for name, row in reserved.items()
    ):
        return _state(revision, "incompatible")
    if not _signatures(connection):
        return _state(revision, "incompatible")
    if _has_forbidden_objects(connection, objects):
        return _state(revision, "incompatible")
    if meta is None:
        return _state(0, "incompatible")
    revision, dirty = meta
    if revision > TARGET_REVISION:
        return _state(revision, "incompatible")
    if not _history(connection, revision):
        return _state(revision, "incompatible")
    if dirty == 1 and revision == TARGET_REVISION:
        return _state(revision, "dirty")
    if dirty != 0 or revision != TARGET_REVISION:
        return _state(revision, "incompatible")
    if integrity:
        _quick_check(connection)
    return _state(revision, "ready")


def _bound_meta(
    connection: sqlite3.Connection, objects: list[sqlite3.Row], namespace: str
) -> tuple[int, int] | None:
    row = next((current for current in objects if current["name"] == META), None)
    if row is None or row["type"] != "table" or row["sql"] != EXPECTED_SQL[META]:
        return None
    try:
        signature = tuple(map(tuple, connection.execute(f'PRAGMA table_xinfo("{META}")')))
    except Exception:
        return None
    if signature != TABLE_XINFO[META]:
        return None
    return _meta(connection, namespace)


def _has_forbidden_objects(connection: sqlite3.Connection, objects: list[sqlite3.Row]) -> bool:
    try:
        for row in objects:
            name = row["name"]
            if _is_reserved(name):
                continue
            object_type = row["type"]
            if object_type in {"view", "trigger"}:
                return True
            if object_type == "index" and _is_reserved(row["tbl_name"]):
                return True
            if object_type == "table":
                quoted = name.replace('"', '""')
                foreign_keys = connection.execute(f'PRAGMA foreign_key_list("{quoted}")')
                if any(_is_reserved(foreign_key[2]) for foreign_key in foreign_keys):
                    return True
    except Exception:
        return True
    return False


def _is_reserved(name: object) -> bool:
    return isinstance(name, str) and name.lower().startswith(RESERVED_PREFIX)


def _meta(connection: sqlite3.Connection, namespace: str) -> tuple[int, int] | None:
    try:
        rows = connection.execute(
            f'SELECT singleton,adapter_id,component,namespace,applied_revision,dirty FROM "{META}"'
        ).fetchall()
        if len(rows) != 1 or tuple(rows[0][:4]) != (
            1,
            "aipcs.sqlite.service_store",
            "service_store",
            namespace,
        ):
            return None
        revision, dirty = rows[0][4], rows[0][5]
        if type(revision) is not int or revision < 0 or dirty not in (0, 1):
            return None
        return revision, dirty
    except Exception:
        return None


def _history(connection: sqlite3.Connection, revision: int) -> bool:
    try:
        rows = connection.execute(
            f'SELECT component,revision,migration_id,checksum,applied_at FROM "{MIGRATION}" '
            "ORDER BY revision"
        ).fetchall()
        if revision != 1 or len(rows) != 1:
            return False
        if tuple(rows[0][:4]) != ("service_store", 1, MIGRATION_ID, CHECKSUM):
            return False
        decode_time(rows[0][4])
    except Exception:
        return False
    return True


def _signatures(connection: sqlite3.Connection) -> bool:
    try:
        for table, expected in TABLE_XINFO.items():
            rows = connection.execute(f'PRAGMA table_xinfo("{table}")')
            if tuple(map(tuple, rows)) != expected:
                return False
        for table, expected in INDEX_LIST.items():
            rows = connection.execute(f'PRAGMA index_list("{table}")')
            actual = tuple(sorted((row[1], row[2], row[3], row[4]) for row in rows))
            if actual != expected:
                return False
        for index, expected in INDEX_XINFO.items():
            rows = connection.execute(f'PRAGMA index_xinfo("{index}")')
            if tuple(map(tuple, rows)) != expected:
                return False
        return True
    except Exception:
        return False


def _quick_check(connection: sqlite3.Connection) -> None:
    budget = 100_000

    def progress() -> int:
        nonlocal budget
        budget -= 1_000
        return int(budget <= 0)

    try:
        connection.set_progress_handler(progress, 1_000)
        row = connection.execute("PRAGMA quick_check(1)").fetchone()
        if row is None or row[0] != "ok":
            raise ValueError
    except Exception:
        raise StorageMigrationError() from None
    finally:
        with suppress(Exception):
            connection.set_progress_handler(None, 0)


def _state(revision: int, status: str) -> MigrationState:
    return MigrationState("service_store", revision, TARGET_REVISION, status)  # type: ignore[arg-type]


def _close(connection: sqlite3.Connection | None, location: AnchoredLocation | None) -> None:
    if connection is not None:
        with suppress(Exception):
            connection.close()
    if location is not None:
        with suppress(Exception):
            location.close()
