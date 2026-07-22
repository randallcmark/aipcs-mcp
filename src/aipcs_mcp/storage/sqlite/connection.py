"""Hardened SQLite connection construction."""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from urllib.parse import quote

from aipcs_mcp.storage.errors import StorageUnavailable

from .location import AnchoredLocation


def connect(location: AnchoredLocation, mode: str, *, query_only: bool) -> sqlite3.Connection:
    if sqlite3.sqlite_version_info < (3, 37, 0) or mode not in {"ro", "rw"}:
        raise StorageUnavailable()
    location.verify_database_identity()
    path = location._sqlite_path()
    uri = "file:" + quote(str(path), safe="/") + "?mode=" + mode
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.enable_load_extension(False)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA recursive_triggers=OFF")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(f"PRAGMA query_only={'ON' if query_only else 'OFF'}")
        _require_pragma(connection, "foreign_keys", 1)
        _require_pragma(connection, "trusted_schema", 0)
        _require_pragma(connection, "recursive_triggers", 0)
        _require_pragma(connection, "query_only", 1 if query_only else 0)
        if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise ValueError
        location.verify_database_identity()
        return connection
    except Exception:
        if connection is not None:
            with suppress(Exception):
                connection.close()
        failure = StorageUnavailable()
    raise failure from None


def set_query_only(connection: sqlite3.Connection, value: bool) -> None:
    try:
        connection.execute(f"PRAGMA query_only={'ON' if value else 'OFF'}")
        _require_pragma(connection, "query_only", 1 if value else 0)
    except Exception:
        pass
    else:
        return
    raise StorageUnavailable() from None


def _require_pragma(connection: sqlite3.Connection, name: str, expected: object) -> None:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    if row is None or row[0] != expected:
        raise ValueError
