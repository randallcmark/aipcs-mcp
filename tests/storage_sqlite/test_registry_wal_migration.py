"""SYNTHETIC_FIXTURE. Registry R3 physical-policy crash/recovery matrix.

The predecessor is deliberately constructed through a finished target and an
explicit test-only downgrade.  It is not a descriptor fixture: historical
identifiers and checksums below are frozen literals, so these tests cannot
silently follow a future descriptor change.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from aipcs_mcp.storage import MigrationState
from aipcs_mcp.storage.errors import StorageBusy, StorageMigrationError, StorageUnavailable
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter
from aipcs_mcp.storage.sqlite import adapter as adapter_module

_R2_ID = "registry-0002-durable-intent"
_R2_CHECKSUM = "b6190247d4f709728bab59cb4eb5fd149d4b7424472615f377b83f5191e0d8ea"
_R3_ID = "registry-0003-wal-policy"
_R3_CHECKSUM = "661d993c71dfe09a237e8f8018a9636b154ae5f9d39deabcbb8a55b6e49baade"
_POLICY = "aipcs_registry_storage_policy"
_AT = "2026-07-23T00:00:00.000000Z"


def _adapter(root: Path) -> SQLiteRegistryAdapter:
    return SQLiteRegistryAdapter(SQLiteLocationPolicy(root))


def _clean_r2_delete(root: Path) -> Path:
    """Create a clean predecessor with a row whose bytes/data must survive."""

    adapter = _adapter(root)
    assert adapter.migrate() == MigrationState("registry", 3, 3, "ready")
    database = root / "registry.sqlite"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
        connection.execute(f'DROP TABLE "{_POLICY}"')
        connection.execute('DELETE FROM "aipcs_registry_migration" WHERE revision=3')
        connection.execute(
            'UPDATE "aipcs_registry_meta" SET applied_revision=2,dirty=0 WHERE singleton=1'
        )
        connection.execute(
            'INSERT INTO "aipcs_registry_service" VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                "00000000-0000-0000-0000-000000000001", "p", "notes", "project", "preserve me",
                _AT, _AT, _AT, None, None, "seeded", "active", 1, None, None, None,
            ),
        )
    return database


def _raw_state(database: Path) -> tuple[str, int, int, tuple[object, ...] | None, tuple[tuple[object, ...], ...]]:
    with sqlite3.connect(database) as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        revision, dirty = connection.execute(
            'SELECT applied_revision,dirty FROM "aipcs_registry_meta"'
        ).fetchone()
        policy = connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name=?", (_POLICY,)
        ).fetchone()
        row = None if policy is None else connection.execute(
            f'SELECT singleton,policy_id,policy_checksum,phase FROM "{_POLICY}" ORDER BY singleton'
        ).fetchone()
        history = tuple(connection.execute(
            'SELECT revision,migration_id,checksum FROM "aipcs_registry_migration" ORDER BY revision'
        ))
    return mode, revision, dirty, row, history


def _assert_recoverable_or_ready(adapter: SQLiteRegistryAdapter, database: Path) -> None:
    """Reacquire rather than trusting the error and prove the exact durable fork."""

    try:
        state = adapter.inspect_migration()
    except StorageUnavailable:
        # An injected post-pragma exception can interrupt SQLite's own
        # last-close cleanup.  The first fresh inspection is then bounded
        # unavailable; raw evidence still proves the only durable state is
        # prepared/WAL, and migrate must reacquire and resume it.
        mode, revision, dirty, row, history = _raw_state(database)
        assert (mode, revision, dirty, row, history[-1]) == (
            "wal", 2, 1, (1, "aipcs.sqlite.wal.v1", _R3_CHECKSUM, "prepared"),
            (2, _R2_ID, _R2_CHECKSUM),
        )
        assert adapter.migrate() == MigrationState("registry", 3, 3, "ready")
        state = adapter.inspect_migration()
    mode, revision, dirty, row, history = _raw_state(database)
    if state.status == "ready":
        assert (mode, revision, dirty, row, history[-1]) == (
            "wal", 3, 0, (1, "aipcs.sqlite.wal.v1", _R3_CHECKSUM, "ready"),
            (3, _R3_ID, _R3_CHECKSUM),
        )
    else:
        assert state == MigrationState("registry", 2, 3, "dirty") or state == MigrationState(
            "registry", 2, 3, "incompatible"
        )
        assert revision == 2
        if row is None:
            assert (mode, dirty, history[-1]) == ("delete", 0, (2, _R2_ID, _R2_CHECKSUM))
        else:
            assert (dirty, row) == (1, (1, "aipcs.sqlite.wal.v1", _R3_CHECKSUM, "prepared"))
            assert mode in {"delete", "wal"}
    assert adapter.migrate() == MigrationState("registry", 3, 3, "ready")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            'SELECT intent_description FROM "aipcs_registry_service"'
        ).fetchone() == ("preserve me",)


_SQL = {
    "policy_ddl": f'CREATE TABLE "{_POLICY}"',
    "policy_row": f'INSERT INTO "{_POLICY}"',
    "dirty": 'UPDATE "aipcs_registry_meta" SET "dirty"=1',
    "journal_mode": "PRAGMA journal_mode=WAL",
    "policy_ready": f'UPDATE "{_POLICY}" SET "phase"=\'ready\'',
    "history": 'INSERT INTO "aipcs_registry_migration"',
    "revision_dirty_clear": 'UPDATE "aipcs_registry_meta" SET "applied_revision"=',
    "checkpoint": "PRAGMA wal_checkpoint(PASSIVE)",
}


class _FaultConnection:
    def __init__(self, wrapped: sqlite3.Connection, point: str, after: bool, plan: dict[str, object]) -> None:
        self._wrapped, self._point, self._after = wrapped, point, after
        self._plan = plan

    def __getattr__(self, name: str):
        return getattr(self._wrapped, name)

    def _matches(self, statement: str) -> bool:
        if self._point == "final_begin":
            return statement == "BEGIN IMMEDIATE" and self._plan["begins"] == 2
        return statement.startswith(_SQL.get(self._point, ""))

    def _raise(self) -> None:
        self._plan["fired"] = True
        raise sqlite3.OperationalError("synthetic C5 boundary fault")

    def execute(self, statement: str, *args, **kwargs):
        if statement == "BEGIN IMMEDIATE":
            self._plan["begins"] = int(self._plan["begins"]) + 1
        match = not self._plan["fired"] and self._matches(statement)
        if match and not self._after:
            self._raise()
        result = self._wrapped.execute(statement, *args, **kwargs)
        if match and self._after:
            if self._point == "journal_mode":
                # This boundary models a process crash after SQLite made the
                # header durable, not an exception retaining a writer lock.
                result.fetchall()
                self._wrapped.close()
            self._raise()
        return result

    def commit(self) -> None:
        self._plan["commits"] = int(self._plan["commits"]) + 1
        match = not self._plan["fired"] and (
            self._point == "prepared_commit" and self._plan["commits"] == 1
            or self._point == "final_commit" and self._plan["commits"] == 2
        )
        if match and not self._after:
            self._raise()
        self._wrapped.commit()
        if self._plan["commits"] >= 2:
            self._plan["target_committed"] = True
        if match and self._after:
            self._raise()

    def close(self) -> None:
        match = not self._plan["fired"] and self._point == "close" and self._plan["target_committed"]
        if match and not self._after:
            self._raise()
        self._wrapped.close()
        if match and self._after:
            self._raise()


_BOUNDARIES = (
    "policy_ddl", "policy_row", "dirty", "journal_mode", "final_begin", "policy_ready",
    "history", "revision_dirty_clear", "prepared_commit", "final_commit", "checkpoint", "close",
)


@pytest.mark.parametrize("point", _BOUNDARIES)
@pytest.mark.parametrize("after", (False, True), ids=("before", "after"))
def test_r3_fault_matrix_reinspects_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str, after: bool
) -> None:
    root = tmp_path / f"{point}-{after}"
    database = _clean_r2_delete(root)
    adapter = _adapter(root)
    real_connect = adapter_module.connect
    plan: dict[str, object] = {"begins": 0, "commits": 0, "target_committed": False, "fired": False}
    monkeypatch.setattr(
        adapter_module, "connect", lambda *args, **kwargs: _FaultConnection(real_connect(*args, **kwargs), point, after, plan)
    )
    with pytest.raises((StorageMigrationError, StorageUnavailable, StorageBusy)):
        adapter.migrate()
    monkeypatch.setattr(adapter_module, "connect", real_connect)
    _assert_recoverable_or_ready(adapter, database)


@pytest.mark.parametrize("after", (False, True), ids=("before", "after"))
def test_r3_final_reinspection_fault_never_rewrites_ready_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after: bool
) -> None:
    root = tmp_path / f"reinspection-{after}"
    database = _clean_r2_delete(root)
    adapter = _adapter(root)
    real_inspect = adapter.inspect_migration

    def faulting_inspect() -> MigrationState:
        if not after:
            raise RuntimeError("synthetic final reinspection fault")
        real_inspect()
        raise RuntimeError("synthetic post-reinspection fault")

    monkeypatch.setattr(adapter, "inspect_migration", faulting_inspect)
    with pytest.raises(StorageMigrationError):
        adapter.migrate()
    monkeypatch.setattr(adapter, "inspect_migration", real_inspect)
    _assert_recoverable_or_ready(adapter, database)


@pytest.mark.parametrize(
    "mutation",
    (
        "old_wal", "target_delete", "missing_row", "extra_row", "wrong_checksum", "wrong_phase",
        "arbitrary_dirty", "future_revision", "altered_history",
    ),
)
def test_r3_adversarial_states_fail_closed(tmp_path: Path, mutation: str) -> None:
    root = tmp_path / mutation
    database = _clean_r2_delete(root)
    adapter = _adapter(root)
    assert adapter.migrate().status == "ready"
    with sqlite3.connect(database) as connection:
        if mutation == "old_wal":
            connection.execute(f'DROP TABLE "{_POLICY}"')
            connection.execute('DELETE FROM "aipcs_registry_migration" WHERE revision=3')
            connection.execute('UPDATE "aipcs_registry_meta" SET applied_revision=2,dirty=0')
        elif mutation == "target_delete":
            assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
        elif mutation == "missing_row":
            connection.execute(f'DELETE FROM "{_POLICY}"')
        elif mutation == "extra_row":
            # Drop/recreate is intentional: the exact singleton DDL prevents an extra row otherwise.
            connection.execute(f'DROP TABLE "{_POLICY}"')
            connection.execute(f'CREATE TABLE "{_POLICY}" (singleton INTEGER, policy_id TEXT, policy_checksum TEXT, phase TEXT)')
            connection.executemany(f'INSERT INTO "{_POLICY}" VALUES (?,?,?,?)', ((1, "aipcs.sqlite.wal.v1", _R3_CHECKSUM, "ready"), (2, "aipcs.sqlite.wal.v1", _R3_CHECKSUM, "ready")))
        elif mutation == "wrong_checksum":
            connection.execute(f'UPDATE "{_POLICY}" SET policy_checksum=?', ("0" * 64,))
        elif mutation == "wrong_phase":
            connection.execute(f'UPDATE "{_POLICY}" SET phase="prepared"')
            connection.execute('UPDATE "aipcs_registry_meta" SET dirty=0')
        elif mutation == "arbitrary_dirty":
            connection.execute('UPDATE "aipcs_registry_meta" SET dirty=1')
        elif mutation == "future_revision":
            connection.execute('UPDATE "aipcs_registry_meta" SET applied_revision=4')
        else:
            connection.execute('UPDATE "aipcs_registry_migration" SET migration_id="altered" WHERE revision=3')
    before = _raw_state(database)
    first = adapter.migrate()
    assert first.status in {"dirty", "incompatible"}
    assert adapter.inspect_migration() == first
    assert adapter.migrate() == first
    assert _raw_state(database) == before
