"""SYNTHETIC_FIXTURE. Managed PostgreSQL concurrency and uncertainty proof.

Every database is the project-owned disposable PostgreSQL 16 fixture. Tests
use independent real connections and bounded synchronization; no ambient
server, external DSN, privileged role, or persistent container is accepted.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, Lock
from typing import Any
from uuid import UUID

import pytest
from application.fakes import FixedClock, SequentialIds
from storage_contracts.parity_fixtures import evolved_parity_manifest, parity_manifest

from aipcs_mcp.application.errors import InternalFailure
from aipcs_mcp.application.models import ApplicationContext, SeedCommand, Service
from aipcs_mcp.application.services import ServiceApplication
from aipcs_mcp.records import (
    BranchMutationOutcome,
    CreateBranchCommand,
    CreateRecordCommand,
    DataFailure,
    FrozenJsonObject,
    GetRecordQuery,
    RecordMutationOutcome,
    UpdateBranchCommand,
    UpdateRecordCommand,
    compile_record_specification,
)
from aipcs_mcp.relational import classify_transition, compile_manifest
from aipcs_mcp.storage import DomainSchemaState, MigrationState
from aipcs_mcp.storage.errors import StorageBusy
from aipcs_mcp.storage.postgresql.connection import (
    PostgreSQLConnectionPolicy,
    PostgreSQLDsn,
    connect_postgresql,
)
from aipcs_mcp.storage.postgresql.data_core import qualified
from aipcs_mcp.storage.postgresql.data_store import PostgreSQLMaterialisedDataStore
from aipcs_mcp.storage.postgresql.domain_schema import PostgreSQLDomainSchemaStore
from aipcs_mcp.storage.postgresql.registry import PostgreSQLRegistryAdapter
from aipcs_mcp.storage.postgresql.registry_migrations import SCHEMA
from aipcs_mcp.storage.postgresql.service_store import PostgreSQLServiceStoreCatalog

from .container import PostgresTestTarget

_NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)
_PRINCIPAL = "concurrency-principal"
_CREATED_VIA = "concurrency-proof"
_JOIN_TIMEOUT = 10.0


def _parallel(*operations: Callable[[], Any]) -> list[Any]:
    """Release independent workers together and bound every wait."""

    barrier = Barrier(len(operations) + 1)

    def run(operation: Callable[[], Any]) -> Any:
        barrier.wait(timeout=_JOIN_TIMEOUT)
        return operation()

    workers = ThreadPoolExecutor(max_workers=len(operations))
    futures = [workers.submit(run, operation) for operation in operations]
    try:
        barrier.wait(timeout=_JOIN_TIMEOUT)
        return [future.result(timeout=_JOIN_TIMEOUT) for future in futures]
    finally:
        workers.shutdown(wait=True, cancel_futures=True)


def _registry(target: PostgresTestTarget, *, lock_timeout_ms: int = 1_000):
    return PostgreSQLRegistryAdapter(
        PostgreSQLDsn(target.dsn),
        PostgreSQLConnectionPolicy(SCHEMA, 5, lock_timeout_ms, 10_000),
    )


def _ready_data(
    target: PostgresTestTarget,
    service_id: UUID,
) -> tuple[
    PostgreSQLDsn,
    PostgreSQLServiceStoreCatalog,
    PostgreSQLDomainSchemaStore,
    object,
    object,
]:
    dsn = PostgreSQLDsn(target.dsn)
    catalog = PostgreSQLServiceStoreCatalog(dsn)
    locator = catalog.allocate(service_id)
    assert catalog.migrate(locator).status == "ready"
    domain = PostgreSQLDomainSchemaStore(dsn, service_store_catalog=catalog)
    manifest = parity_manifest()
    assert domain.materialise(locator, compile_manifest(manifest)) == DomainSchemaState("ready")
    return dsn, catalog, domain, locator, compile_record_specification(manifest)


def _store(
    dsn: PostgreSQLDsn,
    catalog: PostgreSQLServiceStoreCatalog,
    domain: PostgreSQLDomainSchemaStore,
    *,
    connection_factory: Callable[[PostgreSQLDsn, PostgreSQLConnectionPolicy], object] = (
        connect_postgresql
    ),
    lock_timeout_ms: int = 5_000,
) -> PostgreSQLMaterialisedDataStore:
    return PostgreSQLMaterialisedDataStore(
        dsn,
        lock_timeout_ms=lock_timeout_ms,
        statement_timeout_ms=10_000,
        connection_factory=connection_factory,
        service_store_catalog=catalog,
        domain_schema_store=domain,
        clock=lambda: _NOW,
    )


def _create(title: str, key: str) -> CreateRecordCommand:
    return CreateRecordCommand(
        "note",
        FrozenJsonObject.from_mapping({"title": title}),
        key,
    )


def _record_outcome(value: object) -> RecordMutationOutcome:
    assert isinstance(value, RecordMutationOutcome)
    return value


def _branch_outcome(value: object) -> BranchMutationOutcome:
    assert isinstance(value, BranchMutationOutcome)
    return value


def test_registry_first_migration_same_key_replay_and_service_cas_converge(
    postgres_test_target: PostgresTestTarget,
) -> None:
    first_registry = _registry(postgres_test_target)
    second_registry = _registry(postgres_test_target)
    expected = MigrationState("registry", 1, 1, "ready")
    assert _parallel(first_registry.migrate, second_registry.migrate) == [expected, expected]

    context = ApplicationContext(_PRINCIPAL, _CREATED_VIA)
    command = SeedCommand("concurrent_notes", "project", "Concurrent notes", "seed-same")

    def seed() -> object:
        application = ServiceApplication(
            first_registry.open_uow,
            FixedClock(_NOW),
            SequentialIds(UUID(int=1)),
        )
        return application.seed(context, command)

    seeded = _parallel(seed, seed)
    assert seeded[0].model_dump(mode="json", by_alias=True) == seeded[1].model_dump(
        mode="json", by_alias=True
    )
    assert seeded[0].service_id == UUID(int=1)
    check = first_registry.open_uow()
    try:
        assert check.count(_PRINCIPAL) == 1
        check.commit()
    finally:
        check.close()

    cas_service = Service(
        service_id=UUID(int=2),
        principal_id=_PRINCIPAL,
        domain_name="cas_notes",
        domain_class="project",
        intent_description="CAS notes",
        created_at=_NOW,
        updated_at=_NOW,
        last_activity_at=_NOW,
    )
    setup = first_registry.open_uow()
    try:
        setup.add(cas_service)
        setup.commit()
    finally:
        setup.close()

    left = first_registry.open_uow()
    right = first_registry.open_uow()
    try:
        left_value = left.get(_PRINCIPAL, UUID(int=2))
        right_value = right.get(_PRINCIPAL, UUID(int=2))
        assert left_value is not None and right_value == left_value
        moment = _NOW + timedelta(seconds=1)
        candidates = (
            replace(
                left_value,
                intent_description="left update",
                updated_at=moment,
                last_activity_at=moment,
                service_revision=2,
            ),
            replace(
                right_value,
                intent_description="right update",
                updated_at=moment,
                last_activity_at=moment,
                service_revision=2,
            ),
        )

        def save(uow: object, candidate: object) -> object:
            result = uow.save(candidate, 1)  # type: ignore[attr-defined]
            if result == "saved":
                uow.commit()  # type: ignore[attr-defined]
            else:
                uow.rollback()  # type: ignore[attr-defined]
            return result

        saved = _parallel(
            lambda: save(left, candidates[0]),
            lambda: save(right, candidates[1]),
        )
        assert sorted(saved) == ["saved", "stale_revision"]
    finally:
        left.close()
        right.close()

    observed = first_registry.open_uow()
    try:
        terminal = observed.get(_PRINCIPAL, UUID(int=2))
        assert terminal is not None
        assert terminal.service_revision == 2
        assert terminal.intent_description in {"left update", "right update"}
        observed.commit()
    finally:
        observed.close()


def test_first_service_migration_materialisation_and_evolution_converge(
    postgres_test_target: PostgresTestTarget,
) -> None:
    dsn = PostgreSQLDsn(postgres_test_target.dsn)
    locator = PostgreSQLServiceStoreCatalog(dsn).allocate(UUID(int=2))
    expected = MigrationState("service_store", 1, 1, "ready")
    assert _parallel(
        lambda: PostgreSQLServiceStoreCatalog(dsn).migrate(locator),
        lambda: PostgreSQLServiceStoreCatalog(dsn).migrate(locator),
    ) == [expected, expected]

    current_manifest = parity_manifest()
    current = compile_manifest(current_manifest)
    stores = (
        PostgreSQLDomainSchemaStore(dsn),
        PostgreSQLDomainSchemaStore(dsn),
    )
    assert _parallel(
        lambda: stores[0].materialise(locator, current),
        lambda: stores[1].materialise(locator, current),
    ) == [DomainSchemaState("ready"), DomainSchemaState("ready")]

    target_manifest = evolved_parity_manifest()
    transition = classify_transition(current_manifest, target_manifest)
    assert _parallel(
        lambda: stores[0].evolve(locator, transition),
        lambda: stores[1].evolve(locator, transition),
    ) == [DomainSchemaState("ready"), DomainSchemaState("ready")]
    assert stores[0].inspect(locator, compile_manifest(target_manifest)) == DomainSchemaState(
        "ready"
    )


def test_data_same_key_record_and_branch_cas_converge(
    postgres_test_target: PostgresTestTarget,
) -> None:
    dsn, catalog, domain, locator, specification = _ready_data(postgres_test_target, UUID(int=3))
    store = _store(dsn, catalog, domain)

    same = _create("same", "create-same")
    outcomes = [
        _record_outcome(value)
        for value in _parallel(
            lambda: store.create_record(locator, _PRINCIPAL, _CREATED_VIA, specification, same),
            lambda: store.create_record(locator, _PRINCIPAL, _CREATED_VIA, specification, same),
        )
    ]
    assert {value.replayed for value in outcomes} == {False, True}
    assert outcomes[0].record == outcomes[1].record

    baseline = _record_outcome(
        store.create_record(
            locator,
            _PRINCIPAL,
            _CREATED_VIA,
            specification,
            _create("record-cas", "record-cas-create"),
        )
    ).record
    record_results = _parallel(
        lambda: store.update_record(
            locator,
            _PRINCIPAL,
            _CREATED_VIA,
            specification,
            UpdateRecordCommand(
                "note",
                baseline.record_id,
                FrozenJsonObject.from_mapping({"rank": 1}),
                1,
                "record-cas-left",
            ),
        ),
        lambda: store.update_record(
            locator,
            _PRINCIPAL,
            _CREATED_VIA,
            specification,
            UpdateRecordCommand(
                "note",
                baseline.record_id,
                FrozenJsonObject.from_mapping({"rank": 2}),
                1,
                "record-cas-right",
            ),
        ),
    )
    assert sum(isinstance(value, RecordMutationOutcome) for value in record_results) == 1
    assert record_results.count(DataFailure("stale_record_version")) == 1
    current = store.get_record(
        locator,
        _PRINCIPAL,
        _CREATED_VIA,
        specification,
        GetRecordQuery("note", baseline.record_id),
    )
    assert current.record_version == 2  # type: ignore[union-attr]
    assert current.fields["rank"] in {1, 2}  # type: ignore[union-attr]

    branch_command = CreateBranchCommand(
        "branch-cas",
        "Branch CAS",
        "Concurrent branch update.",
        None,
        None,
        None,
        "branch-cas-create",
    )
    branch_replays = [
        _branch_outcome(value)
        for value in _parallel(
            lambda: store.create_branch(
                locator,
                _PRINCIPAL,
                _CREATED_VIA,
                specification,
                branch_command,
            ),
            lambda: store.create_branch(
                locator,
                _PRINCIPAL,
                _CREATED_VIA,
                specification,
                branch_command,
            ),
        )
    ]
    assert {value.replayed for value in branch_replays} == {False, True}
    assert branch_replays[0].branch == branch_replays[1].branch
    branch = branch_replays[0].branch
    branch_results = _parallel(
        lambda: store.update_branch(
            locator,
            _PRINCIPAL,
            _CREATED_VIA,
            specification,
            UpdateBranchCommand(
                branch.branch_id,
                FrozenJsonObject.from_mapping({"title": "Left"}),
                1,
                "branch-cas-left",
            ),
        ),
        lambda: store.update_branch(
            locator,
            _PRINCIPAL,
            _CREATED_VIA,
            specification,
            UpdateBranchCommand(
                branch.branch_id,
                FrozenJsonObject.from_mapping({"title": "Right"}),
                1,
                "branch-cas-right",
            ),
        ),
    )
    assert sum(isinstance(value, BranchMutationOutcome) for value in branch_results) == 1
    assert branch_results.count(DataFailure("stale_branch_revision")) == 1


class _GateConnection:
    def __init__(self, wrapped: object, acquired: Event, release: Event) -> None:
        self._wrapped = wrapped
        self._acquired = acquired
        self._release = release
        self._used = False

    def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> object:
        result = self._wrapped.execute(statement, parameters)  # type: ignore[attr-defined]
        if not self._used and "pg_advisory_xact_lock" in statement:
            self._used = True
            self._acquired.set()
            if not self._release.wait(timeout=_JOIN_TIMEOUT):
                raise RuntimeError("bounded gate timed out")
        return result

    def commit(self) -> None:
        self._wrapped.commit()  # type: ignore[attr-defined]

    def rollback(self) -> None:
        self._wrapped.rollback()  # type: ignore[attr-defined]

    def close(self) -> None:
        self._wrapped.close()  # type: ignore[attr-defined]


class _GateFactory:
    def __init__(self) -> None:
        self.acquired = Event()
        self.release = Event()

    def __call__(self, dsn: PostgreSQLDsn, policy: PostgreSQLConnectionPolicy) -> _GateConnection:
        return _GateConnection(connect_postgresql(dsn, policy), self.acquired, self.release)


def test_different_keys_and_namespaces_progress_while_one_data_lock_is_held(
    postgres_test_target: PostgresTestTarget,
) -> None:
    dsn, first_catalog, first_domain, first_locator, first_spec = _ready_data(
        postgres_test_target, UUID(int=4)
    )
    _, second_catalog, second_domain, second_locator, second_spec = _ready_data(
        postgres_test_target, UUID(int=5)
    )
    gate = _GateFactory()
    blocked = _store(
        dsn,
        first_catalog,
        first_domain,
        connection_factory=gate,
    )
    same_namespace = _store(dsn, first_catalog, first_domain, lock_timeout_ms=500)
    other_namespace = _store(dsn, second_catalog, second_domain, lock_timeout_ms=500)
    workers = ThreadPoolExecutor(max_workers=3)
    held: Future[object] | None = None
    try:
        held = workers.submit(
            blocked.create_record,
            first_locator,
            _PRINCIPAL,
            _CREATED_VIA,
            first_spec,
            _create("held", "held-key"),
        )
        assert gate.acquired.wait(timeout=_JOIN_TIMEOUT)
        different_key = workers.submit(
            same_namespace.create_record,
            first_locator,
            _PRINCIPAL,
            _CREATED_VIA,
            first_spec,
            _create("different-key", "free-key"),
        )
        different_namespace = workers.submit(
            other_namespace.create_record,
            second_locator,
            _PRINCIPAL,
            _CREATED_VIA,
            second_spec,
            _create("different-namespace", "held-key"),
        )
        assert isinstance(different_key.result(timeout=_JOIN_TIMEOUT), RecordMutationOutcome)
        assert isinstance(different_namespace.result(timeout=_JOIN_TIMEOUT), RecordMutationOutcome)
    finally:
        gate.release.set()
        if held is not None:
            assert isinstance(held.result(timeout=_JOIN_TIMEOUT), RecordMutationOutcome)
        workers.shutdown(wait=True, cancel_futures=True)


def test_advisory_and_row_lock_timeouts_are_bounded_and_recoverable(
    postgres_test_target: PostgresTestTarget,
) -> None:
    dsn = PostgreSQLDsn(postgres_test_target.dsn)
    held_locator = PostgreSQLServiceStoreCatalog(dsn).allocate(UUID(int=6))
    other_locator = PostgreSQLServiceStoreCatalog(dsn).allocate(UUID(int=7))
    holder = connect_postgresql(
        dsn,
        PostgreSQLConnectionPolicy(held_locator.namespace, 5, 1_000, 10_000),
    )
    try:
        holder.execute("BEGIN")
        holder.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s, 0))",
            (f"aipcs:service_store:{held_locator.namespace}",),
        )
        bounded = PostgreSQLServiceStoreCatalog(
            dsn,
            lock_timeout_ms=100,
            statement_timeout_ms=2_000,
        )
        try:
            bounded.migrate(held_locator)
        except StorageBusy:
            pass
        else:
            raise AssertionError("held advisory lock must map to StorageBusy")
        assert bounded.migrate(other_locator).status == "ready"
    finally:
        holder.rollback()
        holder.close()

    dsn, catalog, domain, locator, specification = _ready_data(postgres_test_target, UUID(int=8))
    regular = _store(dsn, catalog, domain)
    record = _record_outcome(
        regular.create_record(
            locator,
            _PRINCIPAL,
            _CREATED_VIA,
            specification,
            _create("row-lock", "row-lock-create"),
        )
    ).record
    row_holder = connect_postgresql(
        dsn,
        PostgreSQLConnectionPolicy(locator.namespace, 5, 1_000, 10_000),
    )
    command = UpdateRecordCommand(
        "note",
        record.record_id,
        FrozenJsonObject.from_mapping({"rank": 9}),
        1,
        "row-lock-update",
    )
    try:
        row_holder.execute("BEGIN")
        row_holder.execute(
            f"SELECT id FROM {qualified(locator.namespace, 'note')} WHERE id=%s FOR UPDATE",
            (record.record_id,),
        )
        bounded_store = _store(dsn, catalog, domain, lock_timeout_ms=100)
        assert bounded_store.update_record(
            locator,
            _PRINCIPAL,
            _CREATED_VIA,
            specification,
            command,
        ) == DataFailure("storage_busy")
    finally:
        row_holder.rollback()
        row_holder.close()
    assert isinstance(
        regular.update_record(
            locator,
            _PRINCIPAL,
            _CREATED_VIA,
            specification,
            command,
        ),
        RecordMutationOutcome,
    )


class _CommitLossConnection:
    def __init__(self, wrapped: object) -> None:
        self._wrapped = wrapped

    def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> object:
        return self._wrapped.execute(statement, parameters)  # type: ignore[attr-defined,no-any-return]

    def commit(self) -> None:
        self._wrapped.commit()  # type: ignore[attr-defined]
        raise RuntimeError("synthetic connection loss after durable commit")

    def rollback(self) -> None:
        self._wrapped.rollback()  # type: ignore[attr-defined]

    def close(self) -> None:
        self._wrapped.close()  # type: ignore[attr-defined]


class _CommitLossOnceFactory:
    def __init__(self) -> None:
        self._lock = Lock()
        self._used = False

    def __call__(self, dsn: PostgreSQLDsn, policy: PostgreSQLConnectionPolicy) -> object:
        connection = connect_postgresql(dsn, policy)
        with self._lock:
            if not self._used:
                self._used = True
                return _CommitLossConnection(connection)
        return connection


def test_post_commit_connection_loss_reobserves_replay_without_registry_poisoning(
    postgres_test_target: PostgresTestTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(postgres_test_target)
    assert registry.migrate() == MigrationState("registry", 1, 1, "ready")
    registry_loss = _CommitLossOnceFactory()
    from aipcs_mcp.storage.postgresql import registry as registry_module

    monkeypatch.setattr(registry_module, "connect_postgresql", registry_loss)
    context = ApplicationContext(_PRINCIPAL, _CREATED_VIA)
    seed = SeedCommand("commit_loss", "project", "Commit loss", "registry-commit-loss")
    with pytest.raises(InternalFailure):
        ServiceApplication(
            registry.open_uow,
            FixedClock(_NOW),
            SequentialIds(UUID(int=10)),
        ).seed(context, seed)
    registry_replay = ServiceApplication(
        registry.open_uow,
        FixedClock(_NOW),
        SequentialIds(UUID(int=11)),
    ).seed(context, seed)
    assert registry_replay.service_id == UUID(int=10)
    assert registry.inspect_migration() == MigrationState("registry", 1, 1, "ready")
    registry_check = registry.open_uow()
    try:
        assert registry_check.count(_PRINCIPAL) == 1
        registry_check.commit()
    finally:
        registry_check.close()

    dsn, catalog, domain, locator, specification = _ready_data(postgres_test_target, UUID(int=9))
    factory = _CommitLossOnceFactory()
    store = _store(
        dsn,
        catalog,
        domain,
        connection_factory=factory,
    )
    command = _create("durable", "commit-loss")

    observed = _record_outcome(
        store.create_record(
            locator,
            _PRINCIPAL,
            _CREATED_VIA,
            specification,
            command,
        )
    )
    assert observed.replayed is True
    replay = _record_outcome(
        store.create_record(
            locator,
            _PRINCIPAL,
            _CREATED_VIA,
            specification,
            command,
        )
    )
    assert replay.replayed is True
    assert replay.record == observed.record
