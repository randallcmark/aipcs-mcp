"""Hardened SQLite connection construction."""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import cast
from urllib.parse import quote

from aipcs_mcp.storage.errors import StorageBusy, StorageTransientJournal, StorageUnavailable

from .location import AnchoredLocation, SQLiteHeaderMode
from .result_codes import is_sqlite_busy


@dataclass(frozen=True, slots=True)
class SQLiteConnectionPolicy:
    """Per-connection SQLite settings that are safe to share as a value."""

    busy_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        if type(self.busy_timeout_ms) is not int or not 1 <= self.busy_timeout_ms <= 30_000:
            raise StorageUnavailable()


class SQLiteConnectionPurpose(Enum):
    """The only SQLite phases permitted to open a connection."""

    READY_INSPECTION = ("ro", True, frozenset({"wal"}))
    POLICY_INSPECTION = ("ro", True, frozenset({"delete", "wal"}))
    READY_UNIT_OF_WORK = ("rw", True, frozenset({"wal"}))
    READY_OPERATION = ("rw", False, frozenset({"wal"}))
    MIGRATION = ("rw", False, frozenset({"delete", "wal"}))

    @property
    def mode(self) -> str:
        return self.value[0]

    @property
    def query_only(self) -> bool:
        return self.value[1]

    @property
    def journal_modes(self) -> frozenset[str]:
        return self.value[2]


DEFAULT_CONNECTION_POLICY = SQLiteConnectionPolicy()


def connect(
    location: AnchoredLocation,
    mode: str,
    *,
    query_only: bool,
    policy: SQLiteConnectionPolicy = DEFAULT_CONNECTION_POLICY,
    purpose: SQLiteConnectionPurpose,
) -> sqlite3.Connection:
    """Open one purpose-bound SQLite connection with a bounded busy handler."""

    require_supported_sqlite_runtime()
    if type(policy) is not SQLiteConnectionPolicy:
        raise StorageUnavailable()
    if type(purpose) is not SQLiteConnectionPurpose:
        raise StorageUnavailable()
    if (mode, query_only) != (purpose.mode, purpose.query_only):
        raise StorageUnavailable()
    location.verify_database_identity()
    path = location._sqlite_path()
    uri = "file:" + quote(str(path), safe="/") + "?mode=" + purpose.mode
    connection: sqlite3.Connection | None = None
    sidecars_adopted = False
    allow_journal = purpose is SQLiteConnectionPurpose.MIGRATION
    try:
        connection = sqlite3.connect(
            uri,
            uri=True,
            isolation_level=None,
            timeout=policy.busy_timeout_ms / 1_000,
        )
        # Python exposes a pathname-based SQLite API. Recheck the retained
        # descriptor anchor immediately after the handoff, before issuing any
        # PRAGMA or application statement, so an observable accidental
        # replacement cannot bind this connection to a different database.
        location.verify_live_database_identity()
        connection.row_factory = sqlite3.Row
        connection.enable_load_extension(False)
        connection.execute(f"PRAGMA busy_timeout={policy.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA recursive_triggers=OFF")
        connection.execute("PRAGMA locking_mode=NORMAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA wal_autocheckpoint=1000")
        connection.execute(f"PRAGMA query_only={'ON' if purpose.query_only else 'OFF'}")
        _require_pragma(connection, "busy_timeout", policy.busy_timeout_ms)
        _require_pragma(connection, "foreign_keys", 1)
        _require_pragma(connection, "trusted_schema", 0)
        _require_pragma(connection, "recursive_triggers", 0)
        _require_pragma(connection, "locking_mode", "normal")
        _require_pragma(connection, "synchronous", 2)
        _require_pragma(connection, "wal_autocheckpoint", 1_000)
        _require_pragma(connection, "query_only", 1 if purpose.query_only else 0)
        header_mode = _require_journal_mode(connection, purpose.journal_modes)
        location.verify_live_database_identity()
        location.record_connection_header_mode(header_mode)
        location.adopt_live_sidecars(allow_journal=allow_journal)
        sidecars_adopted = True
        location.verify_live_sidecars(allow_journal=allow_journal)
        location.verify_live_database_identity()
        return connection
    except Exception as error:
        unsafe_sidecars = False
        transient_journal = isinstance(error, StorageTransientJournal)
        if connection is not None:
            try:
                if sidecars_adopted:
                    location.verify_live_sidecars(allow_journal=allow_journal)
                else:
                    location.adopt_live_sidecars(allow_journal=allow_journal)
            except StorageTransientJournal:
                pass
            except Exception:
                unsafe_sidecars = True
            with suppress(Exception):
                connection.close()
        try:
            location.verify_closed_sidecars(allow_journal=allow_journal)
        except StorageTransientJournal:
            pass
        except Exception:
            unsafe_sidecars = True
        if unsafe_sidecars:
            failure = StorageUnavailable()
        elif transient_journal:
            failure = StorageTransientJournal()
        elif isinstance(error, StorageBusy) or is_sqlite_busy(error):
            failure = StorageBusy()
        else:
            failure = StorageUnavailable()
    raise failure from None


def set_query_only(connection: sqlite3.Connection, value: bool) -> None:
    try:
        connection.execute(f"PRAGMA query_only={'ON' if value else 'OFF'}")
        _require_pragma(connection, "query_only", 1 if value else 0)
    except Exception as error:
        failure: Exception = (
            StorageBusy()
            if isinstance(error, StorageBusy) or is_sqlite_busy(error)
            else StorageUnavailable()
        )
    else:
        return
    raise failure from None


def require_supported_sqlite_runtime() -> None:
    """Fail before location access on runtimes affected by the WAL-reset bug."""

    version = getattr(sqlite3, "sqlite_version_info", None)
    if (
        type(version) is not tuple
        or len(version) < 3
        or any(type(part) is not int for part in version[:3])
        or version[:3] < (3, 51, 3)
    ):
        raise StorageUnavailable()


def _require_pragma(connection: sqlite3.Connection, name: str, expected: object) -> None:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    if row is None or row[0] != expected:
        raise ValueError


def _require_journal_mode(
    connection: sqlite3.Connection, allowed: frozenset[str]
) -> SQLiteHeaderMode:
    row = connection.execute("PRAGMA journal_mode").fetchone()
    if (
        row is None
        or type(row[0]) is not str
        or row[0].lower() not in {"delete", "wal"}
        or row[0].lower() not in allowed
    ):
        raise ValueError
    return cast(SQLiteHeaderMode, row[0].lower())
