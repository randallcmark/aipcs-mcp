from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from application.fakes import FixedClock, SequentialIds
from storage_contracts.conformance import assert_registry_application_conformance

from aipcs_mcp.application.errors import InternalFailure
from aipcs_mcp.application.models import ApplicationContext, SeedCommand
from aipcs_mcp.application.services import ServiceApplication
from aipcs_mcp.storage import MigrationState
from aipcs_mcp.storage.errors import StorageMigrationError, StorageUnavailable
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter
from aipcs_mcp.storage.sqlite.migrations import DDL


class TraceUow:
    def __init__(self, wrapped, harness: Harness) -> None:
        self.wrapped, self.harness = wrapped, harness
        self.calls: list[str] = []
        self.close_count = 0
        self.services = self
        self.mutations = self
        self.audits = self

    def _call(self, name, *args):
        self.calls.append(name)
        if self.harness.failure == name:
            raise RuntimeError("sqlite-secret")
        return getattr(self.wrapped, name)(*args)

    def find_domain(self, *args):
        return self._call("find_domain", *args)

    def get(self, *args):
        return self._call("get", *args)

    def list(self, *args):
        return self._call("list", *args)

    def add(self, *args):
        return self._call("add", *args)

    def save(self, *args):
        return self._call("save", *args)

    def claim(self, *args):
        return self._call("claim", *args)

    def complete(self, *args):
        return self._call("complete", *args)

    def append(self, *args):
        return self._call("append", *args)

    def commit(self):
        return self._call("commit")

    def rollback(self):
        return self._call("rollback")

    def close(self):
        self.close_count += 1
        self.calls.append("close")
        self.wrapped.close()
        if self.harness.failure == "close":
            raise RuntimeError("sqlite-secret")


class Harness:
    def __init__(self, root: Path) -> None:
        self.adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root / "registry-root"))
        assert self.adapter.migrate().status == "ready"
        self.failure: str | None = None
        self.items: list[TraceUow] = []

    def application(self, *ids: UUID) -> ServiceApplication:
        def factory():
            uow = TraceUow(self.adapter.open_uow(), self)
            self.items.append(uow)
            return uow

        return ServiceApplication(
            factory, FixedClock(datetime(2026, 1, 1, tzinfo=UTC)), SequentialIds(*ids)
        )

    restart = application

    def traces(self):
        return self.items

    def fail(self, boundary):
        self.failure = boundary


def _directory_snapshot(directory: Path) -> tuple[tuple[str, int, int, int], ...]:
    return tuple(
        sorted(
            (
                entry.name,
                entry.stat().st_mode,
                entry.stat().st_size,
                entry.stat().st_mtime_ns,
            )
            for entry in directory.iterdir()
        )
    )


def _leave_wal_header_without_sidecars(database: Path, *, malformed: bool) -> None:
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    for suffix in ("-wal", "-shm"):
        Path(str(database) + suffix).unlink(missing_ok=True)
    if malformed:
        descriptor = os.open(database, os.O_WRONLY)
        try:
            assert os.pwrite(descriptor, b"\x00", 21) == 1
        finally:
            os.close(descriptor)
    database.chmod(0o600)


def test_sqlite_adapter_satisfies_unchanged_registry_conformance(tmp_path: Path) -> None:
    counter = 0

    def factory() -> Harness:
        nonlocal counter
        counter += 1
        base = tmp_path / str(counter)
        base.mkdir()
        return Harness(base)

    assert_registry_application_conformance(factory)


def test_inspection_is_read_only_and_migration_is_explicit(tmp_path: Path) -> None:
    root = tmp_path / "root"
    policy = SQLiteLocationPolicy(root)
    adapter = SQLiteRegistryAdapter(policy)
    assert adapter.inspect_migration() == MigrationState("registry", 0, 1, "uninitialised")
    assert not root.exists()
    assert adapter.migrate().status == "ready"
    database = root / "registry.sqlite"
    before = database.stat().st_mtime_ns
    assert adapter.inspect_migration().status == "ready"
    assert database.stat().st_mtime_ns == before
    assert not (root / "registry.sqlite-wal").exists()


def test_location_rejects_symlinked_database(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / "registry.sqlite").symlink_to(tmp_path / "outside")
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    with pytest.raises(Exception) as captured:
        adapter.inspect_migration()
    assert "outside" not in str(captured.value)


def test_zero_byte_store_is_uninitialised_and_migratable(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    database = root / "registry.sqlite"
    descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    assert adapter.inspect_migration().status == "uninitialised"
    assert adapter.migrate().status == "ready"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_hostile_sidecars_fail_closed(tmp_path: Path, suffix: str) -> None:
    root = tmp_path / "root"
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    assert adapter.migrate().status == "ready"
    sidecar = root / f"registry.sqlite{suffix}"
    descriptor = os.open(sidecar, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(descriptor, b"synthetic-sidecar")
    os.close(descriptor)
    with pytest.raises(StorageUnavailable):
        adapter.inspect_migration()
    if suffix == "-journal":
        assert adapter.migrate().status == "ready"
        assert not sidecar.exists()


@pytest.mark.parametrize("malformed", [False, True], ids=["valid", "malformed"])
def test_wal_header_without_sidecars_never_changes_registry_layout(
    tmp_path: Path, malformed: bool
) -> None:
    root = tmp_path / "root"
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    assert adapter.migrate().status == "ready"
    _leave_wal_header_without_sidecars(root / "registry.sqlite", malformed=malformed)
    before = _directory_snapshot(root)

    with pytest.raises(StorageUnavailable):
        adapter.inspect_migration()
    assert _directory_snapshot(root) == before
    with pytest.raises(StorageUnavailable):
        adapter.migrate()
    assert _directory_snapshot(root) == before
    with pytest.raises(StorageUnavailable):
        adapter.open_uow()
    assert _directory_snapshot(root) == before


def test_readiness_rejects_dangling_foreign_key(tmp_path: Path) -> None:
    root = tmp_path / "root"
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    adapter.migrate()
    with sqlite3.connect(root / "registry.sqlite") as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            'INSERT INTO "aipcs_registry_audit"('
            "action,outcome,service_id,principal_id,created_via,at) VALUES (?,?,?,?,?,?)",
            ("seed", "created", str(UUID(int=99)), "p", "test", "2026-01-01T00:00:00.000000Z"),
        )
    assert adapter.inspect_migration().status == "incompatible"


def test_dirty_known_partial_layout_is_classified_dirty(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    database = root / "registry.sqlite"
    descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with sqlite3.connect(database) as connection:
        connection.execute(DDL[0])
        connection.execute(
            'INSERT INTO "aipcs_registry_meta" VALUES (1,?,?,?,?)',
            ("aipcs.sqlite.registry", "registry", 0, 1),
        )
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    assert adapter.inspect_migration() == MigrationState("registry", 0, 1, "dirty")


def test_corrupt_service_and_replay_rows_fail_with_bounded_storage_error(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    context = ApplicationContext("p", "test")
    command = SeedCommand("notes", "project", "Notes", "key")
    created = harness.application(UUID(int=1)).seed(context, command)
    root = tmp_path / "registry-root"
    with sqlite3.connect(root / "registry.sqlite") as connection:
        connection.execute(
            'UPDATE "aipcs_registry_service" SET domain_name=? WHERE service_id=?',
            ("UPPER", str(created.service_id)),
        )
    uow = harness.adapter.open_uow()
    with pytest.raises(StorageMigrationError) as captured:
        uow.services.list("p", 100)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    uow.close()


def test_location_rejects_traversal_and_hides_paths(tmp_path: Path) -> None:
    with pytest.raises(StorageUnavailable):
        SQLiteLocationPolicy(Path("/tmp/safe/../other"))
    policy = SQLiteLocationPolicy(tmp_path / "root")
    assert not hasattr(policy, "root") and not hasattr(policy, "database_path")


def test_fresh_uow_is_query_only_until_write_gate(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    uow = harness.adapter.open_uow()
    assert uow._connection.execute("PRAGMA query_only").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        uow._connection.execute('DELETE FROM "aipcs_registry_service"')
    uow.rollback()
    uow.close()


def test_commit_after_durable_exception_replays_from_real_ledger(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    context = ApplicationContext("p", "test")
    command = SeedCommand("notes", "project", "Notes", "key")
    uow = harness.adapter.open_uow()

    class CommitAfterDurable:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def commit(self):
            self.wrapped.commit()
            raise sqlite3.OperationalError("synthetic commit uncertainty")

    uow._connection = CommitAfterDurable(uow._connection)
    uncertain = ServiceApplication(
        lambda: uow,
        FixedClock(datetime(2026, 1, 1, tzinfo=UTC)),
        SequentialIds(UUID(int=1)),
    )
    with pytest.raises(InternalFailure) as captured:
        uncertain.seed(context, command)
    assert captured.value.__cause__ is None and captured.value.__context__ is None
    replay = harness.restart(UUID(int=2)).seed(context, command)
    assert replay.service_id == UUID(int=1)


def test_lock_timeout_is_bounded_and_does_not_leak_path(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    database = tmp_path / "registry-root" / "registry.sqlite"
    blocker = sqlite3.connect(database, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises((StorageUnavailable, StorageMigrationError)) as captured:
            harness.adapter.inspect_migration()
        assert str(database) not in str(captured.value)
    finally:
        blocker.rollback()
        blocker.close()


def test_nonzero_invalid_database_and_unsafe_root_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    database = root / "registry.sqlite"
    descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(descriptor, b"not-sqlite")
    os.close(descriptor)
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    with pytest.raises((StorageUnavailable, StorageMigrationError)):
        adapter.inspect_migration()
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    with pytest.raises(StorageUnavailable):
        SQLiteRegistryAdapter(SQLiteLocationPolicy(unsafe)).inspect_migration()


def test_malformed_replay_is_bounded(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    context = ApplicationContext("p", "test")
    harness.application(UUID(int=1)).seed(context, SeedCommand("notes", "project", "Notes", "key"))
    database = tmp_path / "registry-root" / "registry.sqlite"
    with sqlite3.connect(database) as connection:
        fingerprint = connection.execute(
            'SELECT fingerprint FROM "aipcs_registry_mutation" WHERE idempotency_key=?', ("key",)
        ).fetchone()[0]
        connection.execute(
            'UPDATE "aipcs_registry_mutation" SET result_json=? WHERE idempotency_key=?',
            ('{"unexpected":true}', "key"),
        )
    uow = harness.adapter.open_uow()
    with pytest.raises(StorageMigrationError) as captured:
        uow.mutations.claim("p", "key", fingerprint)
    assert captured.value.__cause__ is None and captured.value.__context__ is None
    uow.rollback()
    uow.close()


def test_explicit_migrate_recovers_real_hot_rollback_journal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    adapter.migrate()
    database = root / "registry.sqlite"
    script = (
        "import os,sqlite3,sys;"
        "c=sqlite3.connect(sys.argv[1],isolation_level=None);"
        "c.execute('PRAGMA journal_mode=DELETE');"
        "c.execute('PRAGMA synchronous=FULL');"
        "c.execute('PRAGMA cache_size=1');"
        "c.execute('BEGIN EXCLUSIVE');"
        "c.execute('UPDATE aipcs_registry_meta SET dirty=1');"
        "c.execute('CREATE TABLE crash_probe(value BLOB)');"
        "c.execute('INSERT INTO crash_probe VALUES (zeroblob(1000000))');"
        "os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", script, str(database)], check=True)
    journal = root / "registry.sqlite-journal"
    assert journal.exists()
    os.chmod(journal, 0o600)
    with pytest.raises(StorageUnavailable):
        adapter.inspect_migration()
    assert adapter.migrate().status == "ready"
    assert not journal.exists()
