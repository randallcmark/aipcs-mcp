"""Package-private exact inspection for the SQLite service-store foundation."""

from __future__ import annotations

import sqlite3
from contextlib import suppress

from aipcs_mcp.storage.contracts import MigrationState
from aipcs_mcp.storage.errors import StorageMigrationError

from .codecs import decode_time
from .service_store_migrations import (
    CHECKSUM,
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


def inspect_connection(
    connection: sqlite3.Connection, namespace: str, *, integrity: bool
) -> MigrationState:
    """Classify one already-open connection against exact foundation revision 1."""

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
