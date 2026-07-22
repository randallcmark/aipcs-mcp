from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from aipcs_mcp.storage import MigrationState
from aipcs_mcp.storage.errors import StorageMigrationError, StorageUnavailable
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter
from aipcs_mcp.storage.sqlite import adapter as adapter_module
from aipcs_mcp.storage.sqlite.migrations import CHECKSUM, DDL


def _adapter(tmp_path: Path, name: str = "root") -> tuple[SQLiteRegistryAdapter, Path]:
    root = tmp_path / name
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    assert adapter.migrate().status == "ready"
    return adapter, root / "registry.sqlite"


def _assert_bounded(error: BaseException) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "secret" not in str(error)


@pytest.mark.parametrize(
    "statement",
    [
        'CREATE VIEW "sqliteevil" AS SELECT singleton FROM "aipcs_registry_meta"',
        'CREATE TRIGGER "sqliteevil" AFTER UPDATE ON "aipcs_registry_meta" BEGIN SELECT 1; END',
    ],
)
def test_unexpected_non_reserved_catalog_objects_are_incompatible(
    tmp_path: Path, statement: str
) -> None:
    adapter, database = _adapter(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(statement)
    assert adapter.inspect_migration().status == "incompatible"


def test_migration_timestamp_must_decode_to_canonical_utc(tmp_path: Path) -> None:
    adapter, database = _adapter(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            'UPDATE "aipcs_registry_migration" SET applied_at=?',
            ("2026-99-99T99:99:99.999999Z",),
        )
    assert adapter.inspect_migration().status == "incompatible"


def test_missing_history_columns_are_incompatible(tmp_path: Path) -> None:
    adapter, database = _adapter(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute('DROP TABLE "aipcs_registry_migration"')
        connection.execute('CREATE TABLE "aipcs_registry_migration" ("component" TEXT) STRICT')
    assert adapter.inspect_migration() == MigrationState("registry", 1, 1, "incompatible")


@pytest.mark.parametrize(
    ("mapping_name", "key", "replacement"),
    [
        ("TABLE_XINFO", "aipcs_registry_meta", ()),
        ("FOREIGN_KEYS", "aipcs_registry_mutation", ()),
        ("INDEX_LIST", "aipcs_registry_service", ()),
        ("INDEX_XINFO", "aipcs_registry_service_list", ()),
    ],
)
def test_every_exact_pragma_signature_participates_in_readiness(
    tmp_path: Path, monkeypatch, mapping_name: str, key: str, replacement: tuple
) -> None:
    adapter, _ = _adapter(tmp_path)
    mapping = getattr(adapter_module, mapping_name)
    monkeypatch.setitem(mapping, key, replacement)
    assert adapter.inspect_migration().status == "incompatible"


def test_open_uow_acquires_and_connects_once_without_quick_check(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "root"
    policy = SQLiteLocationPolicy(root)
    adapter = SQLiteRegistryAdapter(policy)
    adapter.migrate()
    acquire = policy.acquire
    connect = adapter_module.connect
    calls: dict[str, object] = {"acquire": 0, "connect": 0, "sql": []}

    class TrackedConnection:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def execute(self, statement, *args, **kwargs):
            calls["sql"].append(statement)  # type: ignore[union-attr]
            return self.wrapped.execute(statement, *args, **kwargs)

    def tracked_acquire(**kwargs):
        calls["acquire"] = int(calls["acquire"]) + 1
        return acquire(**kwargs)

    def tracked_connect(*args, **kwargs):
        calls["connect"] = int(calls["connect"]) + 1
        return TrackedConnection(connect(*args, **kwargs))

    def forbidden_quick_check(connection):
        raise AssertionError(connection)

    monkeypatch.setattr(policy, "acquire", tracked_acquire)
    monkeypatch.setattr(adapter_module, "connect", tracked_connect)
    monkeypatch.setattr(adapter_module, "_quick_check", forbidden_quick_check)
    uow = adapter.open_uow()
    uow.close()
    assert calls["acquire"] == 1 and calls["connect"] == 1
    assert not any(
        statement in {"PRAGMA foreign_key_check", "PRAGMA quick_check(1)"}
        for statement in calls["sql"]
    )


def test_initial_empty_store_hot_journal_is_recovered_only_by_migrate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    database = root / "registry.sqlite"
    descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    script = (
        "import os,sqlite3,sys;"
        "c=sqlite3.connect(sys.argv[1],isolation_level=None);"
        "c.execute('PRAGMA journal_mode=DELETE');"
        "c.execute('PRAGMA synchronous=FULL');"
        "c.execute('PRAGMA cache_size=1');"
        "c.execute('BEGIN EXCLUSIVE');"
        "c.execute('CREATE TABLE crash_probe(value BLOB)');"
        "c.execute('INSERT INTO crash_probe VALUES (zeroblob(1000000))');"
        "os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", script, str(database)], check=True)
    journal = root / "registry.sqlite-journal"
    assert journal.exists()
    journal.chmod(0o600)
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    with pytest.raises(StorageUnavailable):
        adapter.inspect_migration()
    assert adapter.migrate().status == "ready"
    assert not journal.exists()


@pytest.mark.parametrize("fault_index", range(len(DDL)))
def test_every_ddl_failure_rolls_back_to_uninitialised(
    tmp_path: Path, monkeypatch, fault_index: int
) -> None:
    root = tmp_path / "root"
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    faulty = list(DDL)
    faulty[fault_index] = "NOT VALID SQL"
    with monkeypatch.context() as context:
        context.setattr(adapter_module, "DDL", tuple(faulty))
        with pytest.raises(StorageMigrationError) as captured:
            adapter.migrate()
        _assert_bounded(captured.value)
    assert adapter.inspect_migration().status == "uninitialised"


def test_precommit_readiness_failure_rolls_back(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "root"
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    with monkeypatch.context() as context:
        context.setattr(
            adapter_module,
            "_inspect_connection",
            lambda connection, *, integrity: MigrationState("registry", 1, 1, "incompatible"),
        )
        with pytest.raises(StorageMigrationError) as captured:
            adapter.migrate()
        _assert_bounded(captured.value)
    assert adapter.inspect_migration().status == "uninitialised"


def test_exception_after_durable_migration_commit_leaves_ready_history(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "root"
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    real_connect = adapter_module.connect

    class CommitAfterDurable:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def commit(self):
            self.wrapped.commit()
            raise sqlite3.OperationalError("secret commit uncertainty")

    with monkeypatch.context() as context:
        context.setattr(
            adapter_module,
            "connect",
            lambda *args, **kwargs: CommitAfterDurable(real_connect(*args, **kwargs)),
        )
        with pytest.raises(StorageMigrationError) as captured:
            adapter.migrate()
        _assert_bounded(captured.value)
    assert adapter.inspect_migration().status == "ready"
    assert adapter.migrate().status == "ready"


@pytest.mark.parametrize("mutation", ["checksum", "newer", "missing_history", "dirty_bad"])
def test_history_truth_table_rejects_drift(tmp_path: Path, mutation: str) -> None:
    adapter, database = _adapter(tmp_path)
    with sqlite3.connect(database) as connection:
        if mutation == "checksum":
            replacement = "0" * 64 if CHECKSUM != "0" * 64 else "1" * 64
            connection.execute('UPDATE "aipcs_registry_migration" SET checksum=?', (replacement,))
        elif mutation == "newer":
            connection.execute('UPDATE "aipcs_registry_meta" SET applied_revision=2')
        elif mutation == "missing_history":
            connection.execute('DELETE FROM "aipcs_registry_migration"')
        else:
            connection.execute('UPDATE "aipcs_registry_meta" SET dirty=1')
            connection.execute('UPDATE "aipcs_registry_migration" SET checksum=?', ("0" * 64,))
    assert adapter.inspect_migration().status == "incompatible"


def test_known_complete_dirty_revision_is_distinguished(tmp_path: Path) -> None:
    adapter, database = _adapter(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute('UPDATE "aipcs_registry_meta" SET dirty=1')
    assert adapter.inspect_migration().status == "dirty"


def test_quick_check_progress_cancellation_is_bounded() -> None:
    class Cursor:
        def fetchone(self):
            return ("ok",)

    class InterruptingConnection:
        progress = None
        cleared = False

        def set_progress_handler(self, callback, steps):
            if callback is None:
                self.cleared = steps == 0
            else:
                self.progress = callback

        def execute(self, statement):
            assert statement == "PRAGMA quick_check(1)"
            assert self.progress is not None
            while not self.progress():
                pass
            raise sqlite3.OperationalError("secret interrupted")

    connection = InterruptingConnection()
    with pytest.raises(StorageMigrationError) as captured:
        adapter_module._quick_check(connection)  # type: ignore[arg-type]
    _assert_bounded(captured.value)
    assert connection.cleared


def test_direct_inspection_failure_has_no_exception_chain(tmp_path: Path, monkeypatch) -> None:
    adapter, _ = _adapter(tmp_path)

    def fail(connection, *, integrity):
        raise RuntimeError(f"secret {connection} {integrity}")

    monkeypatch.setattr(adapter_module, "_inspect_connection", fail)
    with pytest.raises(StorageMigrationError) as captured:
        adapter.inspect_migration()
    _assert_bounded(captured.value)
