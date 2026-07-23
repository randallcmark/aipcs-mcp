"""Private numeric SQLite result-code classification."""

from __future__ import annotations

import sqlite3


def is_sqlite_busy(error: BaseException) -> bool:
    """Return whether an exception carries a SQLite BUSY-family primary code."""

    if not isinstance(error, sqlite3.Error):
        return False
    code = getattr(error, "sqlite_errorcode", None)
    return type(code) is int and (code & 0xFF) == sqlite3.SQLITE_BUSY
