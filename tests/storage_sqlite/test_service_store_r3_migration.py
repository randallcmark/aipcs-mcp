"""SYNTHETIC_FIXTURE. Exact R2-to-R3 service-store foundation migration proof."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from aipcs_mcp.storage import MigrationState
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteServiceStoreCatalog
from aipcs_mcp.storage.sqlite.codecs import encode_time
from aipcs_mcp.storage.sqlite.service_store_migrations import (
    META,
    MIGRATION,
    R3,
    R3_BRANCH_TABLE,
    R3_DDL,
    R3_HISTORY_TABLE,
    R3_MUTATION_TABLE,
    R3_RECORD_BRANCH_TABLE,
)

_POLICY = "__aipcs_service_store_policy"
_R1_ID = "service-store-0001-foundation"
_R1_CHECKSUM = "e56f32848bf4f5b762abe4a164e5869658360408d908da73a6b75d8f52c42f1b"
_R2_ID = "service-store-0002-wal-policy"
_R2_CHECKSUM = "d639ef1e1a897e6eaa5109676417f4ab1bc9bbd7a56ce798eee8e2816ce4d8f5"
_POLICY_ID = "aipcs.sqlite.wal.v1"
_R1_DDL = (
    '''CREATE TABLE "__aipcs_service_store_meta" (
    "singleton" INTEGER PRIMARY KEY NOT NULL CHECK ("singleton" = 1),
    "adapter_id" TEXT NOT NULL CHECK ("adapter_id" = 'aipcs.sqlite.service_store'),
    "component" TEXT NOT NULL CHECK ("component" = 'service_store'),
    "namespace" TEXT NOT NULL CHECK (
        length("namespace") = 36 AND substr("namespace", 1, 4) = 'svc_'
        AND "namespace" = lower("namespace")
        AND substr("namespace", 5) NOT GLOB '*[^0-9a-f]*'
    ),
    "applied_revision" INTEGER NOT NULL CHECK ("applied_revision" >= 0),
    "dirty" INTEGER NOT NULL CHECK ("dirty" IN (0, 1))
) STRICT''',
    '''CREATE TABLE "__aipcs_service_store_migration" (
    "component" TEXT NOT NULL CHECK ("component" = 'service_store'),
    "revision" INTEGER NOT NULL CHECK ("revision" > 0),
    "migration_id" TEXT NOT NULL CHECK (length("migration_id") BETWEEN 1 AND 96),
    "checksum" TEXT NOT NULL CHECK (
        length("checksum") = 64 AND "checksum" = lower("checksum")
        AND "checksum" NOT GLOB '*[^0-9a-f]*'
    ),
    "applied_at" TEXT NOT NULL CHECK (
        length("applied_at") = 27 AND substr("applied_at", 11, 1) = 'T'
        AND substr("applied_at", 20, 1) = '.' AND substr("applied_at", 27, 1) = 'Z'
    ),
    PRIMARY KEY ("component", "revision"),
    UNIQUE ("component", "migration_id")
) STRICT''',
)
_POLICY_DDL = '''CREATE TABLE "__aipcs_service_store_policy" (
    "singleton" INTEGER PRIMARY KEY NOT NULL CHECK ("singleton" = 1),
    "policy_id" TEXT NOT NULL CHECK ("policy_id" = 'aipcs.sqlite.wal.v1'),
    "policy_checksum" TEXT NOT NULL CHECK (
        length("policy_checksum") = 64
        AND "policy_checksum" = lower("policy_checksum")
        AND "policy_checksum" NOT GLOB '*[^0-9a-f]*'
    ),
    "phase" TEXT NOT NULL CHECK ("phase" IN ('prepared', 'ready'))
) STRICT'''


def _seed_exact_r2(root: Path) -> tuple[SQLiteServiceStoreCatalog, object, Path]:
    catalog = SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root))
    locator = catalog.allocate(UUID(int=1))
    database = root / "service-stores" / f"{locator.namespace}.sqlite"
    database.parent.mkdir(mode=0o700, parents=True)
    root.chmod(0o700)
    at = encode_time(datetime.now(UTC))
    with sqlite3.connect(database) as connection:
        for statement in _R1_DDL:
            connection.execute(statement)
        connection.execute(_POLICY_DDL)
        connection.execute(
            f'INSERT INTO "{META}" VALUES (1,?,?,?,?,?)',
            ("aipcs.sqlite.service_store", "service_store", locator.namespace, 2, 0),
        )
        connection.executemany(
            f'INSERT INTO "{MIGRATION}" VALUES (?,?,?,?,?)',
            (
                ("service_store", 1, _R1_ID, _R1_CHECKSUM, at),
                ("service_store", 2, _R2_ID, _R2_CHECKSUM, at),
            ),
        )
        connection.execute(
            f'INSERT INTO "{_POLICY}" VALUES (?,?,?,?)',
            (1, _POLICY_ID, _R2_CHECKSUM, "ready"),
        )
        connection.execute('CREATE TABLE "note" ("id" INTEGER PRIMARY KEY, "body" TEXT) STRICT')
        connection.execute('INSERT INTO "note" VALUES (1, "preserve me")')
        connection.commit()
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    database.chmod(0o600)
    return catalog, locator, database


def _r3_history(database: Path) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(database) as connection:
        return tuple(
            connection.execute(
                f'SELECT revision,migration_id,checksum FROM "{MIGRATION}" ORDER BY revision'
            )
        )


def test_exact_r2_read_inspection_is_pure_then_migrates_to_r3(tmp_path: Path) -> None:
    catalog, locator, database = _seed_exact_r2(tmp_path / "r2")
    before = (database.read_bytes(), database.stat().st_mtime_ns)

    assert catalog.inspect_migration(locator) == MigrationState("service_store", 2, 3, "outdated")
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before

    assert catalog.migrate(locator) == MigrationState("service_store", 3, 3, "ready")
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT body FROM "note"').fetchone() == ("preserve me",)
        assert connection.execute(
            f'SELECT applied_revision,dirty FROM "{META}"'
        ).fetchone() == (3, 0)
    assert _r3_history(database) == (
        (1, _R1_ID, _R1_CHECKSUM),
        (2, _R2_ID, _R2_CHECKSUM),
        (3, R3.migration_id, R3.checksum),
    )


def test_exact_r3_prepared_state_retries_without_read_side_ddl(tmp_path: Path) -> None:
    catalog, locator, database = _seed_exact_r2(tmp_path / "prepared")
    with sqlite3.connect(database) as connection:
        connection.execute(f'UPDATE "{META}" SET dirty=1')
        for statement in R3_DDL:
            connection.execute(statement)
    before = (database.read_bytes(), database.stat().st_mtime_ns)

    assert catalog.inspect_migration(locator) == MigrationState("service_store", 2, 3, "dirty")
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before
    assert catalog.migrate(locator) == MigrationState("service_store", 3, 3, "ready")


def test_r3_enforces_byte_bounds_and_one_role_per_record_branch(tmp_path: Path) -> None:
    catalog, locator, database = _seed_exact_r2(tmp_path / "invariants")
    assert catalog.migrate(locator) == MigrationState("service_store", 3, 3, "ready")
    at = "2026-01-01T00:00:00.000000Z"
    principal = "principal"
    branch_id = "00000000-0000-0000-0000-000000000001"
    record_id = "00000000-0000-0000-0000-000000000002"
    with sqlite3.connect(database) as connection:
        connection.execute(
            f'INSERT INTO "{R3_BRANCH_TABLE}" VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                branch_id,
                principal,
                "one-branch",
                "One branch",
                "Test one role per mapping.",
                None,
                None,
                "active",
                None,
                1,
                at,
                at,
            ),
        )
        connection.execute(
            f'INSERT INTO "{R3_RECORD_BRANCH_TABLE}" VALUES (?,?,?,?,?,?)',
            (principal, "note", record_id, branch_id, "primary", at),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f'INSERT INTO "{R3_RECORD_BRANCH_TABLE}" VALUES (?,?,?,?,?,?)',
                (principal, "note", record_id, branch_id, "related", at),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f'INSERT INTO "{R3_MUTATION_TABLE}" VALUES (?,?,?,?,?,?)',
                (principal, "key", "record_create", "a" * 64, "é" * 600_000, at),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f'''INSERT INTO "{R3_HISTORY_TABLE}"
                (principal_id,entity_name,record_id,record_version,event_kind,before_json,after_json,occurred_at)
                VALUES (?,?,?,?,?,?,?,?)''',
                (principal, "note", record_id, 1, "update", "é" * 40_000, None, at),
            )


@pytest.mark.parametrize("mutation", ("extra_reserved", "altered_prepared"))
def test_r3_extra_or_altered_foundation_fails_closed(tmp_path: Path, mutation: str) -> None:
    catalog, locator, database = _seed_exact_r2(tmp_path / mutation)
    with sqlite3.connect(database) as connection:
        if mutation == "extra_reserved":
            connection.execute('CREATE TABLE "__aipcs_hostile" ("id" INTEGER) STRICT')
        else:
            connection.execute(f'UPDATE "{META}" SET dirty=1')
            for statement in R3_DDL:
                connection.execute(statement)
            connection.execute('DROP INDEX "__aipcs_record_branch_branch_lookup"')
    before = database.read_bytes()

    expected = MigrationState("service_store", 2, 3, "incompatible")
    assert catalog.inspect_migration(locator) == expected
    assert catalog.migrate(locator) == expected
    assert database.read_bytes() == before
    if mutation == "altered_prepared":
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name=?", (R3_RECORD_BRANCH_TABLE,)
            ).fetchone() == (1,)
