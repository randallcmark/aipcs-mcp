"""Private, fixed PostgreSQL SQLSTATE classification."""

from __future__ import annotations

_BUSY_SQLSTATES = frozenset({"40001", "40P01", "55P03", "57014"})


def is_postgresql_busy(error: BaseException) -> bool:
    """Return only known retryable transaction/timeout SQLSTATE outcomes."""

    state = getattr(error, "sqlstate", None)
    return type(state) is str and state in _BUSY_SQLSTATES
