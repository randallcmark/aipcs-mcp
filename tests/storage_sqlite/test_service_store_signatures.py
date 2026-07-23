"""SYNTHETIC_FIXTURE. Compiled service-store metadata signature checks."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteServiceStoreCatalog
from aipcs_mcp.storage.sqlite.service_store_migrations import (
    INDEX_LIST,
    INDEX_XINFO,
    META,
    MIGRATION,
    R2_POLICY,
    R3_BRANCH_TABLE,
    R3_HISTORY_TABLE,
    R3_MUTATION_TABLE,
    R3_RECORD_BRANCH_TABLE,
    TABLE_XINFO,
)


def test_compiled_reserved_table_and_index_signatures_match_fresh_store(tmp_path: Path) -> None:
    root = tmp_path / "root"
    catalog = SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root))
    locator = catalog.allocate(UUID(int=1))
    assert catalog.migrate(locator).status == "ready"
    database = root / "service-stores" / f"{locator.namespace}.sqlite"
    with sqlite3.connect(database) as connection:
        for table, expected in TABLE_XINFO.items():
            assert (
                tuple(map(tuple, connection.execute(f'PRAGMA table_xinfo("{table}")'))) == expected
            )
        for table, expected in INDEX_LIST.items():
            rows = connection.execute(f'PRAGMA index_list("{table}")')
            actual = tuple(sorted((row[1], row[2], row[3], row[4]) for row in rows))
            assert actual == expected
        for index, expected in INDEX_XINFO.items():
            assert (
                tuple(map(tuple, connection.execute(f'PRAGMA index_xinfo("{index}")'))) == expected
            )
    assert {
        META,
        MIGRATION,
        R2_POLICY.table_name,
        R3_MUTATION_TABLE,
        R3_HISTORY_TABLE,
        R3_BRANCH_TABLE,
        R3_RECORD_BRANCH_TABLE,
    } == set(TABLE_XINFO)
