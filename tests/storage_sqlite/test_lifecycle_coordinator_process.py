"""Spawn-process lifecycle proof over the private SQLite coordinator boundary."""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from multiprocessing.connection import Connection
from pathlib import Path
from uuid import UUID

from fixtures import valid_manifest

from aipcs_mcp.application.models import Service
from aipcs_mcp.lifecycle import LifecyclePhase, MaterialiseCommand
from aipcs_mcp.lifecycle_coordinator import LifecycleCoordinator
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.relational import compile_manifest
from aipcs_mcp.storage import DomainSchemaState, MigrationState, ServiceStoreLocator
from aipcs_mcp.storage.sqlite import (
    SQLiteLocationPolicy,
    SQLiteRegistryAdapter,
    SQLiteServiceStoreCatalog,
)
from aipcs_mcp.storage.sqlite.domain_schema import SQLiteDomainSchemaStore

_WATCHDOG_SECONDS = 8.0
_PRINCIPAL = "coordinator-process"
_AT = datetime(2026, 7, 23, tzinfo=UTC)
_STATES = frozenset(
    {"READY", "BEGIN_ATTEMPT", "PREPARED", "RESULT", "RELEASE", "LOCK_HELD", "EXIT", "UNEXPECTED_CATALOG"}
)


@dataclass
class _Clock:
    def now(self) -> datetime:
        return _AT


class _GateCatalog:
    """Stop after registry admission and before the first catalog operation."""

    def __init__(self, wrapped: SQLiteServiceStoreCatalog, peer: Connection) -> None:
        self._wrapped = wrapped
        self._peer = peer
        self._stopped = False

    def info(self):
        if not self._stopped:
            self._stopped = True
            _send(self._peer, "PREPARED")
            _wait_release(self._peer)
        return self._wrapped.info()

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)


class _FailIfCatalogTouched:
    """Turns any loser-side service-store access into an observable test failure."""

    def __init__(self, wrapped: SQLiteServiceStoreCatalog, peer: Connection) -> None:
        self._wrapped = wrapped
        self._peer = peer

    def _unexpected(self) -> None:
        _send(self._peer, "UNEXPECTED_CATALOG")
        raise RuntimeError("loser reached service-store work")

    def info(self):
        self._unexpected()

    def allocate(self, service_id: UUID):
        self._unexpected()

    def inspect_migration(self, locator: ServiceStoreLocator):
        self._unexpected()

    def migrate(self, locator: ServiceStoreLocator):
        self._unexpected()


def _send(peer: Connection, state: str, **values: object) -> None:
    assert state in _STATES
    peer.send({"state": state, **values})


def _wait_release(peer: Connection) -> None:
    message = peer.recv()
    if not isinstance(message, dict) or message.get("state") != "RELEASE":
        raise RuntimeError("unexpected process control message")
    _send(peer, "RELEASE")


def _category(value: object) -> str:
    category = value.category  # type: ignore[attr-defined]
    return getattr(category, "value", category)


def _execute_materialise_process(
    root: str,
    peer: Connection,
    service_id: str,
    key: str,
    busy_timeout_ms: int,
    mode: str,
) -> None:
    """Spawn a fresh coordinator; no connection or anchor crosses this boundary."""

    location = SQLiteLocationPolicy(Path(root))
    adapter = SQLiteRegistryAdapter(location, busy_timeout_ms=busy_timeout_ms)
    catalog = SQLiteServiceStoreCatalog(location, busy_timeout_ms=busy_timeout_ms)
    domain = SQLiteDomainSchemaStore(location, busy_timeout_ms=busy_timeout_ms)
    try:
        injected_catalog: object = catalog
        if mode == "gate":
            injected_catalog = _GateCatalog(catalog, peer)
        elif mode == "forbid_catalog":
            injected_catalog = _FailIfCatalogTouched(catalog, peer)
        coordinator = LifecycleCoordinator(adapter.open_uow, _Clock(), injected_catalog, domain)  # type: ignore[arg-type]
        command = MaterialiseCommand(
            principal_id=_PRINCIPAL,
            created_via="coordinator-process-test",
            service_id=UUID(service_id),
            expected_service_revision=1,
            expected_schema_version=1,
            idempotency_key=key,
        )
        _send(peer, "READY")
        _wait_release(peer)
        _send(peer, "BEGIN_ATTEMPT")
        result = coordinator.execute(command)
        _send(peer, "RESULT", category=_category(result))
    except Exception as error:
        _send(peer, "RESULT", category="error", error=type(error).__name__)
    finally:
        _send(peer, "EXIT")
        peer.close()


def _locked_writer_process(database: str, peer: Connection) -> None:
    connection: sqlite3.Connection | None = None
    try:
        _send(peer, "READY")
        connection = sqlite3.connect(database, isolation_level=None, timeout=1.0)
        connection.execute("PRAGMA busy_timeout=1000")
        connection.execute("BEGIN IMMEDIATE")
        _send(peer, "LOCK_HELD")
        _wait_release(peer)
        connection.rollback()
    finally:
        if connection is not None:
            connection.close()
        _send(peer, "EXIT")
        peer.close()


def _spawn(target: object, first: object, *remaining: object) -> tuple[mp.Process, Connection]:
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
    assert event.get("state") == state, event
    return event


def _release(peer: Connection) -> None:
    peer.send({"state": "RELEASE"})
    _event(peer, "RELEASE")


def _release_all(*peers: Connection) -> None:
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
    if process.is_alive():
        process.terminate()
        process.join(_WATCHDOG_SECONDS)
    peer.close()


def _manifest() -> ManifestV2:
    return ManifestV2.model_validate(valid_manifest())


def _service(service_id: UUID) -> Service:
    manifest = _manifest()
    return Service(
        service_id=service_id,
        principal_id=_PRINCIPAL,
        domain_name=f"process_{service_id.hex[-10:]}",
        domain_class="project",
        intent_description="Spawned coordinator proof",
        created_at=_AT,
        updated_at=_AT,
        last_activity_at=_AT,
        manifest=manifest,
        schema_version=1,
    )


def _adapter(root: Path, *, busy_timeout_ms: int = 1_000) -> SQLiteRegistryAdapter:
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root), busy_timeout_ms=busy_timeout_ms)
    assert adapter.migrate() == MigrationState("registry", 3, 3, "ready")
    return adapter


def _add(adapter: SQLiteRegistryAdapter, service: Service) -> None:
    uow = adapter.open_uow()
    try:
        uow.services.add(service)
        uow.commit()
    finally:
        uow.close()


def _service_state(adapter: SQLiteRegistryAdapter, service_id: UUID) -> Service:
    uow = adapter.open_uow()
    try:
        service = uow.services.get(_PRINCIPAL, service_id)
        assert service is not None
        return service
    finally:
        uow.close()


def _store_path(root: Path, service_id: UUID) -> Path:
    return root / "service-stores" / f"svc_{service_id.hex}.sqlite"


def _phase(root: Path, key: str) -> str | None:
    with sqlite3.connect(root / "registry.sqlite") as connection:
        row = connection.execute(
            'SELECT "phase" FROM "aipcs_registry_mutation" WHERE "principal_id"=? '
            'AND "idempotency_key"=?',
            (_PRINCIPAL, key),
        ).fetchone()
    return None if row is None else row[0]


def _terminal_audits(root: Path, service_id: UUID) -> list[tuple[str, str]]:
    with sqlite3.connect(root / "registry.sqlite") as connection:
        return connection.execute(
            'SELECT "action","outcome" FROM "aipcs_registry_audit" WHERE "principal_id"=? '
            'AND "service_id"=? ORDER BY "audit_id"',
            (_PRINCIPAL, str(service_id)),
        ).fetchall()


def _run_parent_completion(root: Path, service_id: UUID, key: str) -> str:
    location = SQLiteLocationPolicy(root)
    adapter = SQLiteRegistryAdapter(location, busy_timeout_ms=1_000)
    coordinator = LifecycleCoordinator(
        adapter.open_uow,
        _Clock(),
        SQLiteServiceStoreCatalog(location, busy_timeout_ms=1_000),
        SQLiteDomainSchemaStore(location, busy_timeout_ms=1_000),
    )
    result = coordinator.execute(
        MaterialiseCommand(
            principal_id=_PRINCIPAL,
            created_via="coordinator-process-test",
            service_id=service_id,
            expected_service_revision=1,
            expected_schema_version=1,
            idempotency_key=key,
        )
    )
    return _category(result)


def test_same_key_materialise_workers_converge_on_one_effect(tmp_path: Path) -> None:
    root = tmp_path / "root"
    service_id = UUID("00000000-0000-0000-0000-000000000111")
    _add(_adapter(root), _service(service_id))
    first, first_peer = _spawn(
        _execute_materialise_process, str(root), str(service_id), "same-key", 1_000, "gate"
    )
    try:
        _event(first_peer, "READY")
        _release(first_peer)
        _event(first_peer, "BEGIN_ATTEMPT")
        _event(first_peer, "PREPARED")

        second, second_peer = _spawn(
            _execute_materialise_process, str(root), str(service_id), "same-key", 1_000, "normal"
        )
        try:
            _event(second_peer, "READY")
            _release(second_peer)
            _event(second_peer, "BEGIN_ATTEMPT")
            assert _event(second_peer, "RESULT")["category"] == "completed"
            _exit(second, second_peer)
        finally:
            _terminate(second, second_peer)

        _release(first_peer)
        assert _event(first_peer, "RESULT")["category"] == "completed"
        _exit(first, first_peer)
    finally:
        _terminate(first, first_peer)

    adapter = _adapter(root)
    service = _service_state(adapter, service_id)
    assert (service.service_revision, service.design_state) == (2, "materialised")
    assert _phase(root, "same-key") == LifecyclePhase.COMPLETED.value
    assert _terminal_audits(root, service_id) == [("materialise", "completed")]


def test_simultaneous_same_key_workers_reconcile_one_exact_target(tmp_path: Path) -> None:
    """Regression: both same-key workers cross the catalog boundary together."""

    root = tmp_path / "root"
    service_id = UUID("00000000-0000-0000-0000-000000000117")
    _add(_adapter(root), _service(service_id))
    first, first_peer = _spawn(
        _execute_materialise_process, str(root), str(service_id), "same-key-race", 1_000, "gate"
    )
    second, second_peer = _spawn(
        _execute_materialise_process, str(root), str(service_id), "same-key-race", 1_000, "gate"
    )
    try:
        _event(first_peer, "READY")
        _event(second_peer, "READY")
        _release_all(first_peer, second_peer)
        _event(first_peer, "BEGIN_ATTEMPT")
        _event(second_peer, "BEGIN_ATTEMPT")
        _event(first_peer, "PREPARED")
        _event(second_peer, "PREPARED")
        _release_all(first_peer, second_peer)
        outcomes = [_event(first_peer, "RESULT")["category"], _event(second_peer, "RESULT")["category"]]
        _exit(first, first_peer)
        _exit(second, second_peer)
    finally:
        _terminate(first, first_peer)
        _terminate(second, second_peer)

    adapter = _adapter(root)
    catalog = SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root), busy_timeout_ms=1_000)
    locator = catalog.allocate(service_id)
    foundation = catalog.inspect_migration(locator).status
    try:
        domain = SQLiteDomainSchemaStore(SQLiteLocationPolicy(root)).inspect(
            locator, compile_manifest(_manifest())
        ).status
    except Exception as error:
        domain = type(error).__name__
    retry = _run_parent_completion(root, service_id, "same-key-race")
    evidence = (
        outcomes,
        retry,
        _phase(root, "same-key-race"),
        foundation,
        domain,
        _service_state(adapter, service_id).design_state,
        _terminal_audits(root, service_id),
    )
    assert set(outcomes) <= {"completed", "operation_uncertain", "storage_busy"}, evidence
    assert retry == "completed", evidence
    assert _phase(root, "same-key-race") == LifecyclePhase.COMPLETED.value, evidence
    assert foundation == "ready", evidence
    assert domain == "ready", evidence
    assert _terminal_audits(root, service_id) == [("materialise", "completed")], evidence


def test_different_key_loser_is_blocked_before_any_service_store_work(tmp_path: Path) -> None:
    root = tmp_path / "root"
    service_id = UUID("00000000-0000-0000-0000-000000000112")
    _add(_adapter(root), _service(service_id))
    winner, winner_peer = _spawn(
        _execute_materialise_process, str(root), str(service_id), "winner", 1_000, "gate"
    )
    try:
        _event(winner_peer, "READY")
        _release(winner_peer)
        _event(winner_peer, "BEGIN_ATTEMPT")
        _event(winner_peer, "PREPARED")

        loser, loser_peer = _spawn(
            _execute_materialise_process,
            str(root),
            str(service_id),
            "loser",
            1_000,
            "forbid_catalog",
        )
        try:
            _event(loser_peer, "READY")
            _release(loser_peer)
            _event(loser_peer, "BEGIN_ATTEMPT")
            assert _event(loser_peer, "RESULT")["category"] == "operation_in_progress"
            _exit(loser, loser_peer)
        finally:
            _terminate(loser, loser_peer)

        assert not _store_path(root, service_id).exists()
        _release(winner_peer)
        assert _event(winner_peer, "RESULT")["category"] == "completed"
        _exit(winner, winner_peer)
    finally:
        _terminate(winner, winner_peer)

    assert _phase(root, "winner") == LifecyclePhase.COMPLETED.value
    assert _phase(root, "loser") is None
    assert _terminal_audits(root, service_id) == [("materialise", "completed")]


def test_registry_admission_lock_returns_busy_without_service_store_creation(tmp_path: Path) -> None:
    root = tmp_path / "root"
    service_id = UUID("00000000-0000-0000-0000-000000000113")
    _add(_adapter(root), _service(service_id))
    holder, holder_peer = _spawn(_locked_writer_process, str(root / "registry.sqlite"))
    worker, worker_peer = _spawn(
        _execute_materialise_process, str(root), str(service_id), "registry-busy", 120, "normal"
    )
    try:
        _event(holder_peer, "READY")
        _event(holder_peer, "LOCK_HELD")
        _event(worker_peer, "READY")
        _release(worker_peer)
        _event(worker_peer, "BEGIN_ATTEMPT")
        assert _event(worker_peer, "RESULT")["category"] == "storage_busy"
        _exit(worker, worker_peer)
        assert not _store_path(root, service_id).exists()
        assert _phase(root, "registry-busy") is None
    finally:
        _release(holder_peer)
        _exit(holder, holder_peer)


def test_service_store_lock_preserves_prepared_intent_then_retry_completes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    service_id = UUID("00000000-0000-0000-0000-000000000114")
    adapter = _adapter(root)
    _add(adapter, _service(service_id))
    catalog = SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root), busy_timeout_ms=1_000)
    locator = catalog.allocate(service_id)
    assert catalog.migrate(locator) == MigrationState("service_store", 2, 2, "ready")
    holder, holder_peer = _spawn(_locked_writer_process, str(_store_path(root, service_id)))
    worker, worker_peer = _spawn(
        _execute_materialise_process, str(root), str(service_id), "store-busy", 120, "normal"
    )
    try:
        _event(holder_peer, "READY")
        _event(holder_peer, "LOCK_HELD")
        _event(worker_peer, "READY")
        _release(worker_peer)
        _event(worker_peer, "BEGIN_ATTEMPT")
        assert _event(worker_peer, "RESULT")["category"] == "storage_busy"
        _exit(worker, worker_peer)
        assert _phase(root, "store-busy") == LifecyclePhase.PREPARED.value
        assert _service_state(adapter, service_id).design_state == "seeded"
    finally:
        _release(holder_peer)
        _exit(holder, holder_peer)

    assert _run_parent_completion(root, service_id, "store-busy") == "completed"
    assert _phase(root, "store-busy") == LifecyclePhase.COMPLETED.value
    assert _terminal_audits(root, service_id) == [("materialise", "completed")]


def test_independent_service_store_progresses_while_peer_store_writer_is_held(tmp_path: Path) -> None:
    root = tmp_path / "root"
    held_id = UUID("00000000-0000-0000-0000-000000000115")
    worker_id = UUID("00000000-0000-0000-0000-000000000116")
    adapter = _adapter(root)
    _add(adapter, _service(held_id))
    _add(adapter, _service(worker_id))
    catalog = SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root), busy_timeout_ms=1_000)
    held_locator = catalog.allocate(held_id)
    assert catalog.migrate(held_locator) == MigrationState("service_store", 2, 2, "ready")
    holder, holder_peer = _spawn(_locked_writer_process, str(_store_path(root, held_id)))
    worker, worker_peer = _spawn(
        _execute_materialise_process, str(root), str(worker_id), "independent", 500, "normal"
    )
    try:
        _event(holder_peer, "READY")
        _event(holder_peer, "LOCK_HELD")
        _event(worker_peer, "READY")
        _release(worker_peer)
        _event(worker_peer, "BEGIN_ATTEMPT")
        assert _event(worker_peer, "RESULT")["category"] == "completed"
        _exit(worker, worker_peer)
    finally:
        _release(holder_peer)
        _exit(holder, holder_peer)

    assert _service_state(adapter, worker_id).design_state == "materialised"
    worker_locator = catalog.allocate(worker_id)
    assert SQLiteDomainSchemaStore(SQLiteLocationPolicy(root)).inspect(
        worker_locator, compile_manifest(_manifest())
    ) == DomainSchemaState("ready")
