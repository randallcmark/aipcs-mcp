"""SYNTHETIC_FIXTURE. Service-store R2 WAL crash/recovery matrix.

All predecessor bytes, ids, checksums, and SQL below are frozen literals.  Do
not import a production descriptor to manufacture a historical fixture.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from aipcs_mcp.storage import MigrationState
from aipcs_mcp.storage.errors import StorageBusy, StorageMigrationError, StorageUnavailable
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteServiceStoreCatalog
from aipcs_mcp.storage.sqlite import service_store as catalog_module
from aipcs_mcp.storage.sqlite.codecs import encode_time

_META = "__aipcs_service_store_meta"
_MIGRATION = "__aipcs_service_store_migration"
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


def _catalog(root: Path) -> SQLiteServiceStoreCatalog:
    return SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root))


def _locator() -> UUID:
    return UUID(int=1)


def _seed_r1_delete(root: Path, *, prepared: bool = False, wal: bool = False) -> tuple[SQLiteServiceStoreCatalog, object, Path]:
    catalog = _catalog(root)
    locator = catalog.allocate(_locator())
    database = root / "service-stores" / f"{locator.namespace}.sqlite"
    database.parent.mkdir(mode=0o700, parents=True)
    root.chmod(0o700)
    with sqlite3.connect(database) as connection:
        for statement in _R1_DDL:
            connection.execute(statement)
        connection.execute(f'INSERT INTO "{_META}" VALUES (1,?,?,?,?,?)', ("aipcs.sqlite.service_store", "service_store", locator.namespace, 1, int(prepared)))
        connection.execute(f'INSERT INTO "{_MIGRATION}" VALUES (?,?,?,?,?)', ("service_store", 1, _R1_ID, _R1_CHECKSUM, encode_time(datetime.now(UTC))))
        connection.execute('CREATE TABLE "note" ("id" INTEGER PRIMARY KEY, "body" TEXT) STRICT')
        connection.execute('INSERT INTO "note" VALUES (1, \'preserve me\')')
        if prepared:
            connection.execute(_POLICY_DDL)
            connection.execute(f'INSERT INTO "{_POLICY}" VALUES (?,?,?,?)', (1, _POLICY_ID, _R2_CHECKSUM, "prepared"))
    if wal:
        with sqlite3.connect(database) as connection:
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    database.chmod(0o600)
    return catalog, locator, database


def _raw_state(database: Path) -> tuple[str, int, int, tuple[object, ...] | None, tuple[tuple[object, ...], ...]]:
    with sqlite3.connect(database) as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        revision, dirty = connection.execute(f'SELECT applied_revision,dirty FROM "{_META}"').fetchone()
        exists = connection.execute("SELECT 1 FROM sqlite_schema WHERE name=?", (_POLICY,)).fetchone()
        row = None if exists is None else connection.execute(f'SELECT singleton,policy_id,policy_checksum,phase FROM "{_POLICY}" ORDER BY singleton').fetchone()
        history = tuple(connection.execute(f'SELECT revision,migration_id,checksum FROM "{_MIGRATION}" ORDER BY revision'))
    return mode, revision, dirty, row, history


def _assert_recoverable_or_ready(catalog: SQLiteServiceStoreCatalog, locator: object, database: Path) -> None:
    state = catalog.inspect_migration(locator)  # type: ignore[arg-type]
    mode, revision, dirty, row, history = _raw_state(database)
    if state.status == "ready":
        assert (mode, revision, dirty, row) == (
            "wal",
            3,
            0,
            (1, _POLICY_ID, _R2_CHECKSUM, "ready"),
        )
        assert history[-2:] == ((2, _R2_ID, _R2_CHECKSUM), history[-1])
        assert history[-1][0] == 3
    else:
        assert state in {
            MigrationState("service_store", 1, 3, "dirty"),
            MigrationState("service_store", 1, 3, "incompatible"),
            MigrationState("service_store", 2, 3, "outdated"),
        }
        assert revision == 1
        if row is None:
            assert (mode, dirty, history[-1]) == ("delete", 0, (1, _R1_ID, _R1_CHECKSUM))
        else:
            assert (dirty, row) == (1, (1, _POLICY_ID, _R2_CHECKSUM, "prepared"))
            assert mode in {"delete", "wal"}
    assert catalog.migrate(locator) == MigrationState("service_store", 3, 3, "ready")  # type: ignore[arg-type]
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT body FROM "note"').fetchone() == ("preserve me",)


_SQL = {
    "policy_ddl": f'CREATE TABLE "{_POLICY}"', "policy_row": f'INSERT INTO "{_POLICY}"',
    "dirty": f'UPDATE "{_META}" SET "dirty"=1', "journal_mode": "PRAGMA journal_mode=WAL",
    "policy_ready": f'UPDATE "{_POLICY}" SET "phase"=\'ready\'', "history": f'INSERT INTO "{_MIGRATION}"',
    "revision_dirty_clear": f'UPDATE "{_META}" SET "applied_revision"=', "checkpoint": "PRAGMA wal_checkpoint(PASSIVE)",
}


class _FaultConnection:
    def __init__(self, wrapped: sqlite3.Connection, point: str, after: bool) -> None:
        self._wrapped, self._point, self._after = wrapped, point, after
        self._begins = self._commits = 0
        self._target_committed = self._fired = False

    def __getattr__(self, name: str):
        return getattr(self._wrapped, name)

    def _raise(self) -> None:
        self._fired = True
        raise sqlite3.OperationalError("synthetic C5 boundary fault")

    def execute(self, statement: str, *args, **kwargs):
        if statement == "BEGIN IMMEDIATE":
            self._begins += 1
        match = (statement == "BEGIN IMMEDIATE" and self._point == "final_begin" and self._begins == 2) or statement.startswith(_SQL.get(self._point, ""))
        match = match and not self._fired
        if match and not self._after:
            self._raise()
        result = self._wrapped.execute(statement, *args, **kwargs)
        if match and self._after:
            if self._point == "journal_mode":
                # Model a post-pragma crash, which releases SQLite's writer
                # slot before the independent recovery observation.
                result.fetchall()
                self._wrapped.close()
            self._raise()
        return result

    def commit(self) -> None:
        self._commits += 1
        match = not self._fired and (
            self._point == "prepared_commit" and self._commits == 1
            or self._point == "final_commit" and self._commits == 2
        )
        if match and not self._after:
            self._raise()
        self._wrapped.commit()
        if self._commits >= 2:
            self._target_committed = True
        if match and self._after:
            self._raise()

    def close(self) -> None:
        match = not self._fired and self._point == "close" and self._target_committed
        if match and not self._after:
            self._raise()
        self._wrapped.close()
        if match and self._after:
            self._raise()


_BOUNDARIES = ("policy_ddl", "policy_row", "dirty", "journal_mode", "final_begin", "policy_ready", "history", "revision_dirty_clear", "prepared_commit", "final_commit", "checkpoint", "close")


@pytest.mark.parametrize("point", _BOUNDARIES)
@pytest.mark.parametrize("after", (False, True), ids=("before", "after"))
def test_r2_fault_matrix_reinspects_and_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str, after: bool) -> None:
    root = tmp_path / f"{point}-{after}"
    catalog, locator, database = _seed_r1_delete(root)
    real_connect = catalog_module.connect
    monkeypatch.setattr(catalog_module, "connect", lambda *args, **kwargs: _FaultConnection(real_connect(*args, **kwargs), point, after))
    with pytest.raises((StorageMigrationError, StorageUnavailable, StorageBusy)):
        catalog.migrate(locator)
    monkeypatch.setattr(catalog_module, "connect", real_connect)
    _assert_recoverable_or_ready(catalog, locator, database)


@pytest.mark.parametrize("after", (False, True), ids=("before", "after"))
def test_r2_final_reinspection_fault_never_rewrites_ready_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after: bool) -> None:
    root = tmp_path / f"reinspection-{after}"
    catalog, locator, database = _seed_r1_delete(root)
    real_inspect = catalog.inspect_migration

    def faulting_inspect(current):
        if not after:
            raise RuntimeError("synthetic final reinspection fault")
        real_inspect(current)
        raise RuntimeError("synthetic post-reinspection fault")

    monkeypatch.setattr(catalog, "inspect_migration", faulting_inspect)
    with pytest.raises(StorageMigrationError):
        catalog.migrate(locator)
    monkeypatch.setattr(catalog, "inspect_migration", real_inspect)
    _assert_recoverable_or_ready(catalog, locator, database)


@pytest.mark.parametrize("prepared,wal", ((False, False), (True, False), (True, True)))
def test_predecessor_and_exact_prepared_states_preserve_domain_data(tmp_path: Path, prepared: bool, wal: bool) -> None:
    catalog, locator, database = _seed_r1_delete(tmp_path / f"{prepared}-{wal}", prepared=prepared, wal=wal)
    assert catalog.migrate(locator) == MigrationState("service_store", 3, 3, "ready")
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT body FROM "note"').fetchone() == ("preserve me",)


@pytest.mark.parametrize("mutation", ("old_wal", "target_delete", "missing_row", "extra_row", "wrong_checksum", "wrong_phase", "arbitrary_dirty", "future_revision", "altered_history"))
def test_r2_adversarial_states_fail_closed(tmp_path: Path, mutation: str) -> None:
    catalog, locator, database = _seed_r1_delete(tmp_path / mutation)
    assert catalog.migrate(locator).status == "ready"
    with sqlite3.connect(database) as connection:
        if mutation == "old_wal":
            connection.execute(f'DROP TABLE "{_POLICY}"')
            connection.execute(f'DELETE FROM "{_MIGRATION}" WHERE revision=2')
            connection.execute(f'UPDATE "{_META}" SET applied_revision=1,dirty=0')
        elif mutation == "target_delete":
            assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
        elif mutation == "missing_row":
            connection.execute(f'DELETE FROM "{_POLICY}"')
        elif mutation == "extra_row":
            connection.execute(f'DROP TABLE "{_POLICY}"')
            connection.execute(f'CREATE TABLE "{_POLICY}" (singleton INTEGER, policy_id TEXT, policy_checksum TEXT, phase TEXT)')
            connection.executemany(f'INSERT INTO "{_POLICY}" VALUES (?,?,?,?)', ((1, _POLICY_ID, _R2_CHECKSUM, "ready"), (2, _POLICY_ID, _R2_CHECKSUM, "ready")))
        elif mutation == "wrong_checksum":
            connection.execute(f'UPDATE "{_POLICY}" SET policy_checksum=?', ("0" * 64,))
        elif mutation == "wrong_phase":
            connection.execute(f'UPDATE "{_POLICY}" SET phase="prepared"')
            connection.execute(f'UPDATE "{_META}" SET dirty=0')
        elif mutation == "arbitrary_dirty":
            connection.execute(f'UPDATE "{_META}" SET dirty=1')
        elif mutation == "future_revision":
            connection.execute(f'UPDATE "{_META}" SET applied_revision=4')
        else:
            connection.execute(f'UPDATE "{_MIGRATION}" SET migration_id="altered" WHERE revision=2')
    before = _raw_state(database)
    first = catalog.migrate(locator)
    assert first.status in {"dirty", "incompatible"}
    assert catalog.inspect_migration(locator) == first
    assert catalog.migrate(locator) == first
    assert _raw_state(database) == before


def test_checkpoint_busy_preserves_ready_target_for_a_later_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog, locator, database = _seed_r1_delete(tmp_path / "checkpoint")
    with monkeypatch.context() as context:
        context.setattr(catalog_module, "_checkpoint_passive", lambda connection: (_ for _ in ()).throw(StorageBusy()))
        with pytest.raises(StorageBusy):
            catalog.migrate(locator)
    _assert_recoverable_or_ready(catalog, locator, database)
