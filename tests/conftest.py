"""Shared test policy for supported and unsupported SQLite runtimes."""

from __future__ import annotations

import pytest

from aipcs_mcp.configuration.resolver import (
    is_supported_sqlite_platform,
    is_supported_sqlite_runtime,
)

_SQLITE_REASON = (
    "requires SQLite 3.51.3 or newer because older runtimes contain the WAL-reset "
    "data-integrity defect; use the managed Python 3.14 test path"
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Keep non-SQLite tests useful when a local runtime is intentionally unsupported."""

    if is_supported_sqlite_platform() and is_supported_sqlite_runtime():
        return
    skip = pytest.mark.skip(reason=_SQLITE_REASON)
    for item in items:
        if item.nodeid.startswith("tests/storage_sqlite/") or "requires_sqlite" in item.keywords:
            item.add_marker(skip)
