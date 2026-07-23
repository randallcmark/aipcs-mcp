"""Spawn-process proof for the SQLite WAL contention contract.

The raw SQLite holder is deliberately limited to creating a deterministic
writer lock. Every contender and persistent-state observation uses the
product adapters, so this remains a test of the configured storage policy.
"""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
from collections import Counter
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from multiprocessing.connection import Connection
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4

import pytest

from aipcs_mcp.application.errors import Conflict
from aipcs_mcp.application.models import ApplicationContext, SeedCommand, Service
from aipcs_mcp.application.services import ServiceApplication
from aipcs_mcp.storage.errors import StorageBusy
from aipcs_mcp.storage.sqlite import (
    SQLiteLocationPolicy,
    SQLiteRegistryAdapter,
    SQLiteServiceStoreCatalog,
)
from aipcs_mcp.storage.sqlite.codecs import encode_time
from aipcs_mcp.storage.sqlite.service_store_migrations import META as SERVICE_STORE_META
from aipcs_mcp.storage.sqlite.service_store_migrations import MIGRATION as SERVICE_STORE_MIGRATION
from aipcs_mcp.storage.sqlite.service_store_migrations import R1 as SERVICE_STORE_R1
from aipcs_mcp.storage.sqlite.service_store_migrations import R2 as SERVICE_STORE_R2
from aipcs_mcp.storage.sqlite.service_store_migrations import R3 as SERVICE_STORE_R3

_WATCHDOG_SECONDS = 8.0
_ALLOWED_STATES = frozenset(
    {"READY", "LOCK_HELD", "BEGIN_ATTEMPT", "COMMIT_RETURNED", "RESULT", "RELEASE", "EXIT"}
)


def _send(peer: Connection, state: str, **values: object) -> None:
    assert state in _ALLOWED_STATES
    peer.send({"state": state, **values})


def _wait_for_release(peer: Connection) -> None:
    message = peer.recv()
    if not isinstance(message, dict) or message.get("state") != "RELEASE":
        raise RuntimeError("unexpected process control message")
    _send(peer, "RELEASE")


def _locked_writer_process(database: str, peer: Connection) -> None:
    """Hold a reserved WAL writer lock without importing application state."""

    connection: sqlite3.Connection | None = None
    try:
        _send(peer, "READY")
        connection = sqlite3.connect(database, isolation_level=None, timeout=1.0)
        connection.execute("PRAGMA busy_timeout=1000")
        connection.execute("BEGIN IMMEDIATE")
        _send(peer, "LOCK_HELD")
        _wait_for_release(peer)
        connection.rollback()
        _send(peer, "EXIT", outcome="released")
    finally:
        if connection is not None:
            connection.close()
        peer.close()


def _service(service_id: UUID) -> Service:
    at = datetime(2026, 7, 23, tzinfo=UTC)
    return Service(
        service_id=service_id,
        principal_id="process-test",
        domain_name=f"process_{service_id.hex[:12]}",
        domain_class="test",
        intent_description="Spawn process contention fixture",
        created_at=at,
        updated_at=at,
        last_activity_at=at,
    )


def _writer_process(
    root: str,
    peer: Connection,
    service_id: str,
    busy_timeout_ms: int,
    crash_after: str | None = None,
) -> None:
    """Use the public registry-UoW boundary for a single writer attempt."""

    adapter = SQLiteRegistryAdapter(
        SQLiteLocationPolicy(Path(root)), busy_timeout_ms=busy_timeout_ms
    )
    uow = None
    started = monotonic()
    try:
        _send(peer, "READY")
        uow = adapter.open_uow()
        _send(peer, "BEGIN_ATTEMPT")
        uow.services.add(_service(UUID(service_id)))
        _send(peer, "LOCK_HELD")
        if crash_after == "before_commit":
            _wait_for_release(peer)
            raise RuntimeError("parent was expected to terminate this process")
        uow.commit()
        _send(peer, "COMMIT_RETURNED")
        if crash_after == "after_commit":
            _wait_for_release(peer)
            raise RuntimeError("parent was expected to terminate this process")
        uow.close()
        uow = None
        _send(peer, "RESULT", outcome="committed", elapsed=monotonic() - started)
    except StorageBusy:
        if uow is not None:
            with suppress(Exception):
                uow.close()
        _send(peer, "RESULT", outcome="storage_busy", elapsed=monotonic() - started)
    except Exception as error:
        if uow is not None:
            with suppress(Exception):
                uow.close()
        _send(peer, "RESULT", outcome="error", error=type(error).__name__)
    finally:
        _send(peer, "EXIT")
        peer.close()


def _reader_process(root: str, peer: Connection, busy_timeout_ms: int) -> None:
    adapter = SQLiteRegistryAdapter(
        SQLiteLocationPolicy(Path(root)), busy_timeout_ms=busy_timeout_ms
    )
    started = monotonic()
    try:
        _send(peer, "READY")
        _send(peer, "BEGIN_ATTEMPT")
        state = adapter.inspect_migration()
        _send(peer, "RESULT", outcome=state.status, elapsed=monotonic() - started)
    except Exception as error:
        _send(peer, "RESULT", outcome="error", error=type(error).__name__)
    finally:
        _send(peer, "EXIT")
        peer.close()


def _service_store_migration_process(
    root: str,
    peer: Connection,
    service_id: str,
    busy_timeout_ms: int,
    wait_for_start: bool = False,
) -> None:
    catalog = SQLiteServiceStoreCatalog(
        SQLiteLocationPolicy(Path(root)), busy_timeout_ms=busy_timeout_ms
    )
    try:
        _send(peer, "READY")
        if wait_for_start:
            _wait_for_release(peer)
        _send(peer, "BEGIN_ATTEMPT")
        state = catalog.migrate(catalog.allocate(UUID(service_id)))
        _send(peer, "COMMIT_RETURNED")
        _send(peer, "RESULT", outcome=state.status)
    except Exception as error:
        _send(peer, "RESULT", outcome="error", error=type(error).__name__)
    finally:
        _send(peer, "EXIT")
        peer.close()


def _snapshot_reader_process(root: str, peer: Connection, busy_timeout_ms: int) -> None:
    """Hold one adapter-owned read transaction across an independently committed write."""

    adapter = SQLiteRegistryAdapter(
        SQLiteLocationPolicy(Path(root)), busy_timeout_ms=busy_timeout_ms
    )
    uow = None
    try:
        _send(peer, "READY")
        uow = adapter.open_uow()
        initial = len(uow.services.list("process-test", 100))
        _send(peer, "BEGIN_ATTEMPT")
        _send(peer, "LOCK_HELD", count=initial)
        _wait_for_release(peer)
        repeated = len(uow.services.list("process-test", 100))
        uow.close()
        uow = None
        _send(peer, "RESULT", outcome="snapshot", counts=(initial, repeated))
    except Exception as error:
        if uow is not None:
            with suppress(Exception):
                uow.close()
        _send(peer, "RESULT", outcome="error", error=type(error).__name__)
    finally:
        _send(peer, "EXIT")
        peer.close()


def _service_count_reader_process(root: str, peer: Connection, busy_timeout_ms: int) -> None:
    adapter = SQLiteRegistryAdapter(
        SQLiteLocationPolicy(Path(root)), busy_timeout_ms=busy_timeout_ms
    )
    uow = None
    try:
        _send(peer, "READY")
        uow = adapter.open_uow()
        _send(peer, "BEGIN_ATTEMPT")
        count = len(uow.services.list("process-test", 100))
        uow.close()
        uow = None
        _send(peer, "RESULT", outcome="count", count=count)
    except Exception as error:
        if uow is not None:
            with suppress(Exception):
                uow.close()
        _send(peer, "RESULT", outcome="error", error=type(error).__name__)
    finally:
        _send(peer, "EXIT")
        peer.close()


def _cas_writer_process(
    root: str, peer: Connection, service_id: str, label: str, busy_timeout_ms: int
) -> None:
    """Take a detached R1 snapshot, then prove the R1 CAS cannot overwrite a peer."""

    adapter = SQLiteRegistryAdapter(
        SQLiteLocationPolicy(Path(root)), busy_timeout_ms=busy_timeout_ms
    )
    uow = None
    try:
        uow = adapter.open_uow()
        original = uow.services.get("process-test", UUID(service_id))
        uow.close()
        uow = None
        if original is None:
            raise RuntimeError("missing CAS fixture")
        candidate = replace(
            original,
            intent_description=f"CAS update from {label}",
            updated_at=original.updated_at + timedelta(seconds=1),
            last_activity_at=original.last_activity_at + timedelta(seconds=1),
            service_revision=original.service_revision + 1,
        )
        _send(peer, "READY")
        _wait_for_release(peer)
        _send(peer, "BEGIN_ATTEMPT")
        outcome = _save_cas(adapter, candidate, original.service_revision)
        _send(peer, "COMMIT_RETURNED")
        _send(peer, "RESULT", outcome=outcome)
    except StorageBusy:
        _send(peer, "RESULT", outcome="storage_busy")
        _wait_for_release(peer)
        outcome = _save_cas(adapter, candidate, original.service_revision)
        _send(peer, "COMMIT_RETURNED")
        _send(peer, "RESULT", outcome=outcome)
    except Exception as error:
        if uow is not None:
            with suppress(Exception):
                uow.close()
        _send(peer, "RESULT", outcome="error", error=type(error).__name__)
    finally:
        _send(peer, "EXIT")
        peer.close()


def _save_cas(
    adapter: SQLiteRegistryAdapter, candidate: Service, expected_service_revision: int
) -> str:
    uow = adapter.open_uow()
    try:
        outcome = uow.services.save(candidate, expected_service_revision)
        if outcome == "saved":
            uow.commit()
        else:
            uow.rollback()
        return outcome
    finally:
        uow.close()


class _ProcessClock:
    def now(self) -> datetime:
        return datetime(2026, 7, 23, tzinfo=UTC)


class _ProcessIds:
    def __init__(self, service_id: str) -> None:
        self._service_id = UUID(service_id)

    def new_service_id(self) -> UUID:
        return self._service_id


def _seed_application_process(
    root: str,
    peer: Connection,
    service_id: str,
    domain_name: str,
    idempotency_key: str,
    busy_timeout_ms: int,
) -> None:
    """Exercise the application port, including its idempotency fingerprint boundary."""

    adapter = SQLiteRegistryAdapter(
        SQLiteLocationPolicy(Path(root)), busy_timeout_ms=busy_timeout_ms
    )
    application = ServiceApplication(adapter.open_uow, _ProcessClock(), _ProcessIds(service_id))
    try:
        _send(peer, "READY")
        _wait_for_release(peer)
        _send(peer, "BEGIN_ATTEMPT")
        metadata = application.seed(
            ApplicationContext("process-idempotency", "process-test"),
            SeedCommand(domain_name, "test", "Process idempotency fixture", idempotency_key),
        )
        _send(peer, "COMMIT_RETURNED")
        _send(peer, "RESULT", outcome="success", service_id=str(metadata.service_id))
    except Conflict:
        _send(peer, "RESULT", outcome="conflict")
    except Exception as error:
        _send(peer, "RESULT", outcome="error", error=type(error).__name__)
    finally:
        _send(peer, "EXIT")
        peer.close()


def _checkpoint_process(database: str, peer: Connection) -> None:
    """Use SQLite's checkpoint result tuple, which has no product-facing operation yet."""

    connection: sqlite3.Connection | None = None
    try:
        _send(peer, "READY")
        _wait_for_release(peer)
        _send(peer, "BEGIN_ATTEMPT")
        connection = sqlite3.connect(database, isolation_level=None, timeout=1.0)
        connection.execute("PRAGMA busy_timeout=1000")
        result = connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        if result is None or len(result) != 3:
            raise RuntimeError("invalid PASSIVE checkpoint result")
        _send(peer, "RESULT", outcome="checkpoint", values=tuple(result))
    except Exception as error:
        _send(peer, "RESULT", outcome="error", error=type(error).__name__)
    finally:
        if connection is not None:
            connection.close()
        _send(peer, "EXIT")
        peer.close()


def _adapter(root: Path, *, busy_timeout_ms: int = 5_000) -> SQLiteRegistryAdapter:
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root), busy_timeout_ms=busy_timeout_ms)
    assert adapter.migrate().status == "ready"
    return adapter


def _spawn(target: object, first: object, *remaining: object) -> tuple[mp.Process, Connection]:
    """Spawn a child with the structured control pipe in argument position two."""

    context = mp.get_context("spawn")
    parent, child = context.Pipe(duplex=True)
    process = context.Process(target=target, args=(first, child, *remaining))
    process.start()
    child.close()
    return process, parent


def _event(peer: Connection, state: str) -> dict[str, object]:
    assert peer.poll(_WATCHDOG_SECONDS), f"watchdog expired waiting for {state}"
    event = peer.recv()
    assert isinstance(event, dict)
    assert event.get("state") in _ALLOWED_STATES
    assert event["state"] == state, event
    return event


def _result(peer: Connection) -> dict[str, object]:
    """Consume the optional durable-commit marker before a child result."""

    assert peer.poll(_WATCHDOG_SECONDS), "watchdog expired waiting for result"
    event = peer.recv()
    assert isinstance(event, dict)
    assert event.get("state") in _ALLOWED_STATES
    if event["state"] == "COMMIT_RETURNED":
        return _event(peer, "RESULT")
    assert event["state"] == "RESULT", event
    return event


def _release(peer: Connection) -> None:
    peer.send({"state": "RELEASE"})
    _event(peer, "RELEASE")


def _release_all(*peers: Connection) -> None:
    """One parent-owned start barrier for contenders that must begin together."""

    for peer in peers:
        peer.send({"state": "RELEASE"})
    for peer in peers:
        _event(peer, "RELEASE")


def _exit(process: mp.Process, peer: Connection, *, expected: int = 0) -> None:
    _event(peer, "EXIT")
    process.join(_WATCHDOG_SECONDS)
    assert not process.is_alive(), "watchdog expired waiting for child exit"
    assert process.exitcode == expected
    peer.close()


def _terminate(process: mp.Process, peer: Connection) -> None:
    process.terminate()
    process.join(_WATCHDOG_SECONDS)
    assert not process.is_alive(), "watchdog expired terminating crash child"
    assert process.exitcode not in {None, 0}
    peer.close()


def _locked_registry(root: Path) -> tuple[mp.Process, Connection]:
    process, peer = _spawn(_locked_writer_process, str(root / "registry.sqlite"))
    _event(peer, "READY")
    _event(peer, "LOCK_HELD")
    return process, peer


def _service_ids(adapter: SQLiteRegistryAdapter, principal_id: str = "process-test") -> set[UUID]:
    uow = adapter.open_uow()
    try:
        return {service.service_id for service in uow.services.list(principal_id, 100)}
    finally:
        uow.close()


def _seed_service_store_predecessor(
    root: Path, service_id: UUID
) -> tuple[SQLiteServiceStoreCatalog, object, Path]:
    """Create an exact R1 DELETE predecessor without invoking current migration construction."""

    root.mkdir(mode=0o700)
    root.chmod(0o700)
    catalog = SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root), busy_timeout_ms=1_000)
    locator = catalog.allocate(service_id)
    stores = root / "service-stores"
    stores.mkdir(mode=0o700)
    stores.chmod(0o700)
    database = stores / f"{locator.namespace}.sqlite"
    with sqlite3.connect(database) as connection:
        for statement in SERVICE_STORE_R1.ddl:
            connection.execute(statement)
        connection.execute(
            f'INSERT INTO "{SERVICE_STORE_META}" VALUES (1,?,?,?,?,?)',
            (
                "aipcs.sqlite.service_store",
                "service_store",
                locator.namespace,
                SERVICE_STORE_R1.revision,
                0,
            ),
        )
        connection.execute(
            f'INSERT INTO "{SERVICE_STORE_MIGRATION}" VALUES (?,?,?,?,?)',
            (
                "service_store",
                SERVICE_STORE_R1.revision,
                SERVICE_STORE_R1.migration_id,
                SERVICE_STORE_R1.checksum,
                encode_time(datetime.now(UTC)),
            ),
        )
        connection.execute('CREATE TABLE "note" ("id" INTEGER PRIMARY KEY, "body" TEXT) STRICT')
        connection.execute('INSERT INTO "note" VALUES (1, \'predecessor payload\')')
    database.chmod(0o600)
    return catalog, locator, database


def _service_store_history(database: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(
            f'SELECT "revision","migration_id","checksum" FROM "{SERVICE_STORE_MIGRATION}" '
            'ORDER BY "revision"'
        ).fetchall()


def test_reader_completes_while_an_independent_writer_holds_wal_lock(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    _adapter(root)  # Prewarm WAL before any timing-sensitive process starts.
    holder, holder_peer = _locked_registry(root)
    reader, reader_peer = _spawn(_reader_process, str(root), 500)
    try:
        _event(reader_peer, "READY")
        _event(reader_peer, "BEGIN_ATTEMPT")
        result = _event(reader_peer, "RESULT")
        assert Counter([result["outcome"]]) == Counter(["ready"])
        _exit(reader, reader_peer)
    finally:
        _release(holder_peer)
        _exit(holder, holder_peer)


def test_writer_reports_busy_after_one_bounded_attempt(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    adapter = _adapter(root)
    holder, holder_peer = _locked_registry(root)
    service_id = uuid4()
    writer, writer_peer = _spawn(_writer_process, str(root), str(service_id), 150)
    try:
        _event(writer_peer, "READY")
        _event(writer_peer, "BEGIN_ATTEMPT")
        result = _event(writer_peer, "RESULT")
        assert Counter([result["outcome"]]) == Counter(["storage_busy"])
        assert 0.05 <= float(result["elapsed"]) < 2.0
        _exit(writer, writer_peer)
        assert service_id not in _service_ids(adapter)
    finally:
        _release(holder_peer)
        _exit(holder, holder_peer)


def test_writer_unblocks_when_holder_releases_before_timeout(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    adapter = _adapter(root)
    holder, holder_peer = _locked_registry(root)
    service_id = uuid4()
    writer, writer_peer = _spawn(_writer_process, str(root), str(service_id), 1_500)
    try:
        _event(writer_peer, "READY")
        _event(writer_peer, "BEGIN_ATTEMPT")
        _release(holder_peer)
        _exit(holder, holder_peer)
        assert _event(writer_peer, "LOCK_HELD")
        assert _event(writer_peer, "COMMIT_RETURNED")
        result = _event(writer_peer, "RESULT")
        assert Counter([result["outcome"]]) == Counter(["committed"])
        assert float(result["elapsed"]) < 2.0
        _exit(writer, writer_peer)
        assert service_id in _service_ids(adapter)
    finally:
        if holder.is_alive():
            _release(holder_peer)
            _exit(holder, holder_peer)


def test_crash_before_commit_releases_writer_slot_without_persisting_update(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    adapter = _adapter(root)
    service_id = uuid4()
    writer, writer_peer = _spawn(
        _writer_process,
        str(root),
        str(service_id),
        1_000,
        "before_commit",
    )
    _event(writer_peer, "READY")
    _event(writer_peer, "BEGIN_ATTEMPT")
    _event(writer_peer, "LOCK_HELD")
    _terminate(writer, writer_peer)
    assert service_id not in _service_ids(adapter)


def test_crash_after_returned_commit_is_visible_after_independent_restart(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    _adapter(root)
    service_id = uuid4()
    writer, writer_peer = _spawn(
        _writer_process,
        str(root),
        str(service_id),
        1_000,
        "after_commit",
    )
    _event(writer_peer, "READY")
    _event(writer_peer, "BEGIN_ATTEMPT")
    _event(writer_peer, "LOCK_HELD")
    _event(writer_peer, "COMMIT_RETURNED")
    _terminate(writer, writer_peer)
    restarted = SQLiteRegistryAdapter(SQLiteLocationPolicy(root), busy_timeout_ms=1_000)
    assert restarted.inspect_migration().status == "ready"
    assert service_id in _service_ids(restarted)


def test_service_store_writer_progresses_while_registry_writer_is_locked(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _adapter(root)
    catalog = SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root), busy_timeout_ms=1_000)
    service_id = uuid4()
    locator = catalog.allocate(service_id)
    assert catalog.migrate(locator).status == "ready"  # Prewarm independent WAL store.

    holder, holder_peer = _locked_registry(root)
    worker, worker_peer = _spawn(
        _service_store_migration_process,
        str(root),
        str(service_id),
        500,
    )
    try:
        _event(worker_peer, "READY")
        _event(worker_peer, "BEGIN_ATTEMPT")
        _event(worker_peer, "COMMIT_RETURNED")
        result = _event(worker_peer, "RESULT")
        assert Counter([result["outcome"]]) == Counter(["ready"])
        _exit(worker, worker_peer)
    finally:
        _release(holder_peer)
        _exit(holder, holder_peer)


def test_reader_snapshot_stays_old_while_a_later_reader_observes_the_commit(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    _adapter(root)
    held_reader, held_peer = _spawn(_snapshot_reader_process, str(root), 1_000)
    writer = None
    later_reader = None
    try:
        _event(held_peer, "READY")
        _event(held_peer, "BEGIN_ATTEMPT")
        held = _event(held_peer, "LOCK_HELD")
        assert held["count"] == 0

        writer, writer_peer = _spawn(_writer_process, str(root), str(uuid4()), 1_000)
        _event(writer_peer, "READY")
        _event(writer_peer, "BEGIN_ATTEMPT")
        _event(writer_peer, "LOCK_HELD")
        _event(writer_peer, "COMMIT_RETURNED")
        assert _event(writer_peer, "RESULT")["outcome"] == "committed"
        _exit(writer, writer_peer)
        writer = None

        later_reader, later_peer = _spawn(_service_count_reader_process, str(root), 1_000)
        _event(later_peer, "READY")
        _event(later_peer, "BEGIN_ATTEMPT")
        later = _event(later_peer, "RESULT")
        assert Counter([later["outcome"], later["count"]]) == Counter(["count", 1])
        _exit(later_reader, later_peer)
        later_reader = None

        _release(held_peer)
        snapshot = _event(held_peer, "RESULT")
        assert snapshot == {"state": "RESULT", "outcome": "snapshot", "counts": (0, 0)}
        _exit(held_reader, held_peer)
    finally:
        if writer is not None and writer.is_alive():
            writer.terminate()
            writer.join(_WATCHDOG_SECONDS)
        if later_reader is not None and later_reader.is_alive():
            later_reader.terminate()
            later_reader.join(_WATCHDOG_SECONDS)
        if held_reader.is_alive():
            _terminate(held_reader, held_peer)


@pytest.mark.parametrize("predecessor", [False, True], ids=["fresh", "predecessor"])
def test_two_processes_converge_on_one_exact_service_store_history(
    tmp_path: Path, predecessor: bool
) -> None:
    root = tmp_path / "root"
    service_id = uuid4()
    if predecessor:
        catalog, locator, database = _seed_service_store_predecessor(root, service_id)
    else:
        _adapter(root)
        catalog = SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root), busy_timeout_ms=1_000)
        locator = catalog.allocate(service_id)
        database = root / "service-stores" / f"{locator.namespace}.sqlite"

    first, first_peer = _spawn(
        _service_store_migration_process, str(root), str(service_id), 2_000, True
    )
    second, second_peer = _spawn(
        _service_store_migration_process, str(root), str(service_id), 2_000, True
    )
    try:
        _event(first_peer, "READY")
        _event(second_peer, "READY")
        _release_all(first_peer, second_peer)
        _event(first_peer, "BEGIN_ATTEMPT")
        _event(second_peer, "BEGIN_ATTEMPT")
        results = [_result(first_peer), _result(second_peer)]
        outcomes = [result["outcome"] for result in results]
        # A fresh-store contender can legitimately meet the other writer at
        # the required PASSIVE checkpoint.  That indicator is a bounded
        # StorageBusy result and the adapter deliberately has no retry loop.
        # Require one exact winner and reject every other failure category;
        # the final observation below proves the shared target converged.
        assert "ready" in outcomes, results
        assert all(
            result["outcome"] == "ready"
            or (
                result["outcome"] == "error"
                and result.get("error") == "StorageBusy"
            )
            for result in results
        ), results
        _exit(first, first_peer)
        _exit(second, second_peer)
    finally:
        for process, peer in ((first, first_peer), (second, second_peer)):
            if process.is_alive():
                process.terminate()
                process.join(_WATCHDOG_SECONDS)
                peer.close()

    assert catalog.inspect_migration(locator).status == "ready"
    assert _service_store_history(database) == [
        (SERVICE_STORE_R1.revision, SERVICE_STORE_R1.migration_id, SERVICE_STORE_R1.checksum),
        (SERVICE_STORE_R2.revision, SERVICE_STORE_R2.migration_id, SERVICE_STORE_R2.checksum),
        (SERVICE_STORE_R3.revision, SERVICE_STORE_R3.migration_id, SERVICE_STORE_R3.checksum),
    ]
    if predecessor:
        with sqlite3.connect(database) as connection:
            assert connection.execute('SELECT "body" FROM "note"').fetchone() == (
                "predecessor payload",
            )


def test_two_process_cas_writers_preserve_the_winner_and_report_busy_then_stale(
    tmp_path: Path,
) -> None:
    root = tmp_path / "registry"
    adapter = _adapter(root)
    service_id = uuid4()
    uow = adapter.open_uow()
    try:
        uow.services.add(_service(service_id))
        uow.commit()
    finally:
        uow.close()

    holder, holder_peer = _locked_registry(root)
    first, first_peer = _spawn(_cas_writer_process, str(root), str(service_id), "first", 150)
    second, second_peer = _spawn(
        _cas_writer_process, str(root), str(service_id), "second", 150
    )
    try:
        _event(first_peer, "READY")
        _event(second_peer, "READY")
        _release_all(first_peer, second_peer)
        _event(first_peer, "BEGIN_ATTEMPT")
        _event(second_peer, "BEGIN_ATTEMPT")
        assert Counter([_result(first_peer)["outcome"], _result(second_peer)["outcome"]]) == Counter(
            ["storage_busy", "storage_busy"]
        )

        _release(holder_peer)
        _exit(holder, holder_peer)
        _release(first_peer)
        assert _result(first_peer)["outcome"] == "saved"
        _exit(first, first_peer)
        _release(second_peer)
        assert _result(second_peer)["outcome"] == "stale_revision"
        _exit(second, second_peer)
    finally:
        for process, peer in ((holder, holder_peer), (first, first_peer), (second, second_peer)):
            if process.is_alive():
                process.terminate()
                process.join(_WATCHDOG_SECONDS)
                peer.close()

    uow = adapter.open_uow()
    try:
        persisted = uow.services.get("process-test", service_id)
    finally:
        uow.close()
    assert persisted is not None
    assert (persisted.service_revision, persisted.intent_description) == (2, "CAS update from first")


def test_spawned_application_calls_replay_one_idempotent_effect_and_conflict_on_new_fingerprint(
    tmp_path: Path,
) -> None:
    root = tmp_path / "registry"
    adapter = _adapter(root)
    key = "process-shared-key"
    first, first_peer = _spawn(
        _seed_application_process, str(root), str(uuid4()), "process_domain", key, 2_000
    )
    second, second_peer = _spawn(
        _seed_application_process, str(root), str(uuid4()), "process_domain", key, 2_000
    )
    try:
        _event(first_peer, "READY")
        _event(second_peer, "READY")
        _release_all(first_peer, second_peer)
        _event(first_peer, "BEGIN_ATTEMPT")
        _event(second_peer, "BEGIN_ATTEMPT")
        first_result = _result(first_peer)
        second_result = _result(second_peer)
        assert Counter([first_result["outcome"], second_result["outcome"]]) == Counter(
            ["success", "success"]
        ), (first_result, second_result)
        assert first_result["service_id"] == second_result["service_id"], (
            first_result,
            second_result,
        )
        _exit(first, first_peer)
        _exit(second, second_peer)
    finally:
        for process, peer in ((first, first_peer), (second, second_peer)):
            if process.is_alive():
                process.terminate()
                process.join(_WATCHDOG_SECONDS)
                peer.close()

    changed, changed_peer = _spawn(
        _seed_application_process, str(root), str(uuid4()), "process_changed", key, 1_000
    )
    try:
        _event(changed_peer, "READY")
        _release(changed_peer)
        _event(changed_peer, "BEGIN_ATTEMPT")
        assert Counter([_event(changed_peer, "RESULT")["outcome"]]) == Counter(["conflict"])
        _exit(changed, changed_peer)
    finally:
        if changed.is_alive():
            changed.terminate()
            changed.join(_WATCHDOG_SECONDS)
            changed_peer.close()

    assert len(_service_ids(adapter, "process-idempotency")) == 1
    with sqlite3.connect(root / "registry.sqlite") as connection:
        assert connection.execute('SELECT count(*) FROM "aipcs_registry_claim"').fetchone() == (1,)
        assert connection.execute('SELECT count(*) FROM "aipcs_registry_audit"').fetchone() == (1,)


def test_independent_service_databases_progress_while_a_peer_writer_is_held(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _adapter(root)
    catalog = SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root), busy_timeout_ms=1_000)
    held_locator = catalog.allocate(uuid4())
    assert catalog.migrate(held_locator).status == "ready"
    holder, holder_peer = _spawn(
        _locked_writer_process,
        str(root / "service-stores" / f"{held_locator.namespace}.sqlite"),
    )
    worker, worker_peer = _spawn(
        _service_store_migration_process, str(root), str(uuid4()), 500
    )
    try:
        _event(holder_peer, "READY")
        _event(holder_peer, "LOCK_HELD")
        _event(worker_peer, "READY")
        _event(worker_peer, "BEGIN_ATTEMPT")
        _event(worker_peer, "COMMIT_RETURNED")
        assert Counter([_event(worker_peer, "RESULT")["outcome"]]) == Counter(["ready"])
        _exit(worker, worker_peer)
    finally:
        _release(holder_peer)
        _exit(holder, holder_peer)


def test_passive_checkpoint_is_partial_under_reader_then_completes_after_release(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    _adapter(root)
    reader, reader_peer = _spawn(_snapshot_reader_process, str(root), 1_000)
    writer = None
    try:
        _event(reader_peer, "READY")
        _event(reader_peer, "BEGIN_ATTEMPT")
        _event(reader_peer, "LOCK_HELD")

        writer, writer_peer = _spawn(
            _writer_process,
            str(root),
            str(uuid4()),
            1_000,
            "after_commit",
        )
        _event(writer_peer, "READY")
        _event(writer_peer, "BEGIN_ATTEMPT")
        _event(writer_peer, "LOCK_HELD")
        _event(writer_peer, "COMMIT_RETURNED")

        partial, partial_peer = _spawn(_checkpoint_process, str(root / "registry.sqlite"))
        _event(partial_peer, "READY")
        _release(partial_peer)
        _event(partial_peer, "BEGIN_ATTEMPT")
        partial_result = _event(partial_peer, "RESULT")
        assert partial_result["outcome"] == "checkpoint"
        partial_values = tuple(partial_result["values"])
        assert partial_values[0] == 0
        assert partial_values[1] > partial_values[2] >= 0
        _exit(partial, partial_peer)

        # Preserve the committed WAL frames until after the partial checkpoint,
        # then model a process crash without a clean SQLite last-close.
        _terminate(writer, writer_peer)
        writer = None

        _release(reader_peer)
        assert _event(reader_peer, "RESULT")["outcome"] == "snapshot"
        _exit(reader, reader_peer)

        complete, complete_peer = _spawn(_checkpoint_process, str(root / "registry.sqlite"))
        _event(complete_peer, "READY")
        _release(complete_peer)
        _event(complete_peer, "BEGIN_ATTEMPT")
        complete_result = _event(complete_peer, "RESULT")
        assert complete_result["outcome"] == "checkpoint"
        complete_values = tuple(complete_result["values"])
        assert complete_values[0] == 0
        assert complete_values[1] == complete_values[2]
        _exit(complete, complete_peer)
    finally:
        if writer is not None and writer.is_alive():
            _terminate(writer, writer_peer)
        if reader.is_alive():
            _terminate(reader, reader_peer)
