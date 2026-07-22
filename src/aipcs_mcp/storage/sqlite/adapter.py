"""Private SQLite registry adapter and migration state machine."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from functools import wraps

from aipcs_mcp.application.ports import RegistryUnitOfWork
from aipcs_mcp.storage.contracts import MigrationState, StorageAdapterInfo
from aipcs_mcp.storage.errors import StorageMigrationError, StorageUnavailable

from .codecs import decode_time, encode_time
from .connection import connect
from .location import AnchoredLocation, SQLiteLocationPolicy, create_database
from .migrations import (
    CHECKSUM,
    DDL,
    EXPECTED_SQL,
    FOREIGN_KEYS,
    INDEX_LIST,
    INDEX_XINFO,
    MIGRATION_ID,
    TABLE_XINFO,
    TARGET_REVISION,
)
from .uow import SQLiteRegistryUnitOfWork

_EXPECTED_NAMES = frozenset(EXPECTED_SQL)


def _bounded_adapter[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return method(*args, **kwargs)
        except StorageUnavailable:
            failure = StorageUnavailable()
        except StorageMigrationError:
            failure = StorageMigrationError()
        except Exception:
            failure = StorageMigrationError()
        raise failure from None

    return wrapped


class SQLiteRegistryAdapter:
    def __init__(self, location: SQLiteLocationPolicy) -> None:
        if not isinstance(location, SQLiteLocationPolicy):
            raise StorageUnavailable()
        self._location = location

    def __repr__(self) -> str:
        return "SQLiteRegistryAdapter(<redacted>)"

    def info(self) -> StorageAdapterInfo:
        return StorageAdapterInfo("sqlite", frozenset({"registry"}))

    @_bounded_adapter
    def inspect_migration(self) -> MigrationState:
        anchored: AnchoredLocation | None = None
        connection: sqlite3.Connection | None = None
        try:
            anchored = self._location.acquire(create_root=False, allow_journal=False)
            if anchored is None or anchored._database_stat is None:
                return _state(0, "uninitialised")
            if anchored._database_stat.st_size == 0:
                return _state(0, "uninitialised")
            connection = connect(anchored, "ro", query_only=True)
            return _inspect_connection(connection, integrity=True)
        finally:
            _close(connection, anchored)

    @_bounded_adapter
    def migrate(self) -> MigrationState:
        anchored: AnchoredLocation | None = None
        connection: sqlite3.Connection | None = None
        try:
            anchored = self._location.acquire(create_root=True, allow_journal=True)
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
                state = self.inspect_migration()
                if state.status != "uninitialised":
                    return state
                anchored = self._location.acquire(create_root=True, allow_journal=False)
                if anchored is None:
                    raise StorageUnavailable()
            if anchored._database_stat is None:
                anchored._database_stat = create_database(anchored._root_fd)
            elif anchored._database_stat.st_size:
                connection = connect(anchored, "rw", query_only=False)
                state = _inspect_connection(connection, integrity=True)
                connection.close()
                connection = None
                if state.status != "uninitialised":
                    return state
            connection = connect(anchored, "rw", query_only=False)
            connection.execute("BEGIN EXCLUSIVE")
            for statement in DDL:
                connection.execute(statement)
            connection.execute(
                'INSERT INTO "aipcs_registry_meta" VALUES (1,?,?,?,?)',
                ("aipcs.sqlite.registry", "registry", 1, 1),
            )
            connection.execute(
                'INSERT INTO "aipcs_registry_migration" VALUES (?,?,?,?,?)',
                ("registry", 1, MIGRATION_ID, CHECKSUM, encode_time(datetime.now(UTC))),
            )
            connection.execute('UPDATE "aipcs_registry_meta" SET "dirty"=0 WHERE "singleton"=1')
            if _inspect_connection(connection, integrity=True).status != "ready":
                raise StorageMigrationError()
            connection.commit()
            connection.close()
            connection = None
            anchored.verify_database_identity()
            anchored.close()
            anchored = None
            return self.inspect_migration()
        finally:
            _close(connection, anchored)

    @_bounded_adapter
    def open_uow(self) -> RegistryUnitOfWork:
        anchored: AnchoredLocation | None = None
        connection: sqlite3.Connection | None = None
        try:
            anchored = self._location.acquire(create_root=False, allow_journal=False)
            if anchored is None or anchored._database_stat is None:
                raise StorageUnavailable()
            connection = connect(anchored, "rw", query_only=True)
            if _inspect_connection(connection, integrity=False).status != "ready":
                raise StorageMigrationError()
            anchored.close()
            anchored = None
            result = SQLiteRegistryUnitOfWork(connection)
            connection = None
            return result
        finally:
            _close(connection, anchored)


def _inspect_connection(connection: sqlite3.Connection, *, integrity: bool) -> MigrationState:
    objects = connection.execute(
        "SELECT type,name,sql FROM sqlite_schema WHERE substr(name,1,7) <> 'sqlite_'"
    ).fetchall()
    if not objects:
        return _state(0, "uninitialised")
    by_name = {row["name"]: row for row in objects}
    meta = _meta(connection, by_name)
    if meta is None:
        return _state(0, "incompatible")
    revision, dirty = meta
    if revision > TARGET_REVISION:
        return _state(revision, "incompatible")
    history_ok = _history_prefix(connection, by_name, revision)
    hostile = set(by_name) - _EXPECTED_NAMES
    expected_partial = all(
        row["sql"] == EXPECTED_SQL[name] for name, row in by_name.items() if name in EXPECTED_SQL
    )
    if dirty == 1 and history_ok and not hostile and expected_partial:
        return _state(revision, "dirty")
    if dirty != 0 or revision != TARGET_REVISION or not history_ok:
        return _state(revision, "incompatible")
    if set(by_name) != _EXPECTED_NAMES or not expected_partial:
        return _state(revision, "incompatible")
    if not _signatures(connection):
        return _state(revision, "incompatible")
    if integrity:
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            return _state(revision, "incompatible")
        _quick_check(connection)
    return _state(revision, "ready")


def _meta(
    connection: sqlite3.Connection, objects: dict[str, sqlite3.Row]
) -> tuple[int, int] | None:
    if "aipcs_registry_meta" not in objects:
        return None
    try:
        rows = connection.execute(
            'SELECT singleton,adapter_id,component,applied_revision,dirty FROM "aipcs_registry_meta"'
        ).fetchall()
        if len(rows) != 1 or tuple(rows[0][:3]) != (1, "aipcs.sqlite.registry", "registry"):
            return None
        revision, dirty = rows[0][3], rows[0][4]
        if type(revision) is not int or revision < 0 or dirty not in (0, 1):
            return None
        return revision, dirty
    except Exception:
        return None


def _history_prefix(
    connection: sqlite3.Connection,
    objects: dict[str, sqlite3.Row],
    revision: int,
) -> bool:
    try:
        if revision == 0:
            return (
                "aipcs_registry_migration" not in objects
                or not connection.execute(
                    'SELECT 1 FROM "aipcs_registry_migration" LIMIT 1'
                ).fetchone()
            )
        if "aipcs_registry_migration" not in objects:
            return False
        rows = connection.execute(
            "SELECT component,revision,migration_id,checksum,applied_at "
            'FROM "aipcs_registry_migration" ORDER BY revision'
        ).fetchall()
        if (
            revision != 1
            or len(rows) != 1
            or tuple(rows[0][:4]) != ("registry", 1, MIGRATION_ID, CHECKSUM)
        ):
            return False
        decode_time(rows[0][4])
    except StorageMigrationError:
        return False
    except sqlite3.OperationalError as error:
        if str(error).startswith(("no such column:", "no such table:")):
            return False
        raise
    return True


def _signatures(connection: sqlite3.Connection) -> bool:
    try:
        for table, expected in TABLE_XINFO.items():
            rows = connection.execute(f'PRAGMA table_xinfo("{table}")')
            if tuple(map(tuple, rows)) != expected:
                return False
        for table, expected in FOREIGN_KEYS.items():
            rows = connection.execute(f'PRAGMA foreign_key_list("{table}")')
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
        failure = StorageMigrationError()
    else:
        return
    finally:
        with suppress(Exception):
            connection.set_progress_handler(None, 0)
    raise failure from None


def _state(revision: int, status: str) -> MigrationState:
    return MigrationState("registry", revision, TARGET_REVISION, status)  # type: ignore[arg-type]


def _close(connection: sqlite3.Connection | None, location: AnchoredLocation | None) -> None:
    if connection is not None:
        with suppress(Exception):
            connection.close()
    if location is not None:
        with suppress(Exception):
            location.close()
