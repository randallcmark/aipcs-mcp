"""Private coordinator behavior; no MCP/runtime composition is exercised here."""

from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fixtures import valid_manifest

from aipcs_mcp.application.models import (
    CompletedLifecycleClaim,
    ConflictClaim,
    EvolveCompletion,
    LifecycleExecutionResult,
    MaterialisationStorage,
    MaterialiseCompletion,
    OperationInProgress,
    PreparedLifecycleClaim,
    RecoveryRequiredLifecycleClaim,
    Service,
    StaleRevision,
    UnsupportedTransition,
)
from aipcs_mcp.lifecycle import (
    EvolveCommand,
    LifecycleContractError,
    LifecycleIntent,
    LifecyclePhase,
    LifecycleResultCategory,
    MaterialiseCommand,
    prepare_intent,
)
from aipcs_mcp.lifecycle_coordinator import LifecycleCoordinator
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.storage.contracts import (
    DomainSchemaState,
    MigrationState,
    ServiceStoreLocator,
    StorageAdapterInfo,
)
from aipcs_mcp.storage.errors import (
    StorageBusy,
    StorageContractError,
    StorageMigrationError,
    StorageTransientJournal,
    StorageUnavailable,
)

_ID = UUID("5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23")
_AT = datetime(2026, 7, 23, tzinfo=UTC)


def _manifest(version: int = 1) -> ManifestV2:
    payload = deepcopy(valid_manifest())
    payload["schema_version"] = version
    if version == 2:
        payload["migration_history"] = [
            {"from_schema_version": 1, "to_schema_version": 2, "operations": ["add summary"]}
        ]
        payload["entities"][0]["attributes"].append({"name": "summary", "type": "string"})
    return ManifestV2.model_validate(payload)


def _service(*, materialised: bool = False) -> Service:
    storage = MaterialisationStorage("sqlite", f"svc_{_ID.hex}") if materialised else None
    return Service(
        service_id=_ID,
        principal_id="principal",
        domain_name="notes",
        domain_class="project",
        intent_description="durable notes",
        created_at=_AT,
        updated_at=_AT,
        last_activity_at=_AT,
        manifest=_manifest(),
        schema_version=1,
        design_state="materialised" if materialised else "seeded",
        service_revision=2,
        materialised_at=_AT if materialised else None,
        storage=storage,
    )


class _Clock:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def now(self) -> datetime:
        if self.error is not None:
            raise self.error
        return _AT


class _State:
    def __init__(self, service: Service) -> None:
        self.service = service
        self.intent: LifecycleIntent | None = None
        self.uows: list[_Uow] = []
        self.invalid_terminal = False
        self.admission_outcome: object | None = None
        self.source_override: Service | None = None
        self.source_missing = False
        self.terminal_override: object | None = None
        self.failures: dict[str, Exception] = {}
        self.uow_failures: dict[tuple[int, str], Exception] = {}
        self.uow_error: Exception | None = None

    def uow(self) -> _Uow:
        if self.uow_error is not None:
            raise self.uow_error
        number = len(self.uows) + 1
        error = self.uow_failures.get((number, "open"))
        if error is not None:
            raise error
        value = _Uow(self, number)
        self.uows.append(value)
        return value


class _Uow:
    def __init__(self, state: _State, number: int) -> None:
        self.state = state
        self.number = number
        self.service = state.service
        self.intent = state.intent
        self.closed = False
        self.close_count = 0
        self.committed = False
        self.calls: list[str] = []
        self.services = self
        self.mutations = self
        self.audits = self

    def get(self, principal_id: str, service_id: UUID) -> Service | None:
        self._called("get")
        if self.state.source_missing:
            return None
        if self.state.source_override is not None:
            return self.state.source_override
        if principal_id != self.service.principal_id or service_id != self.service.service_id:
            return None
        return self.service

    def resolve_or_admit(self, command: MaterialiseCommand | EvolveCommand):
        self._called("resolve")
        if self.state.admission_outcome is not None:
            return self.state.admission_outcome
        if self.intent is not None:
            if self.intent.phase is LifecyclePhase.COMPLETED:
                return CompletedLifecycleClaim(self.intent, self.service)
            if self.intent.phase is LifecyclePhase.RECOVERY_REQUIRED:
                return RecoveryRequiredLifecycleClaim(self.intent)
            return PreparedLifecycleClaim(self.intent)
        target = self.service.manifest if type(command) is MaterialiseCommand else command.target_manifest
        assert target is not None
        self.intent = prepare_intent(command, target)
        return PreparedLifecycleClaim(self.intent)

    def finalize_completed(self, completion: MaterialiseCompletion | EvolveCompletion):
        self._called("finalize_completed")
        assert self.intent == completion.prepared_intent
        if self.state.terminal_override is not None:
            return self.state.terminal_override
        if self.state.invalid_terminal:
            return object()
        if type(completion) is MaterialiseCompletion:
            self.service = replace(
                self.service,
                design_state="materialised",
                materialised_at=completion.at,
                storage=completion.storage,
                updated_at=completion.at,
                last_activity_at=completion.at,
                service_revision=self.service.service_revision + 1,
            )
        else:
            self.service = replace(
                self.service,
                manifest=completion.prepared_intent.target_manifest,
                schema_version=completion.prepared_intent.target_manifest.schema_version,
                updated_at=completion.at,
                last_activity_at=completion.at,
                service_revision=self.service.service_revision + 1,
            )
        self.intent = self.intent.with_phase(LifecyclePhase.COMPLETED)
        return CompletedLifecycleClaim(self.intent, self.service)

    def finalize_recovery_required(self, intent: LifecycleIntent, at: datetime):
        self._called("finalize_recovery")
        assert self.intent == intent
        if self.state.terminal_override is not None:
            return self.state.terminal_override
        self.intent = intent.with_phase(LifecyclePhase.RECOVERY_REQUIRED)
        return RecoveryRequiredLifecycleClaim(self.intent)

    def commit(self) -> None:
        self._called("commit")
        self.state.service = self.service
        self.state.intent = self.intent
        self.committed = True

    def rollback(self) -> None:
        self._called("rollback")

    def close(self) -> None:
        self.calls.append("close")
        error = self.state.uow_failures.get((self.number, "close")) or self.state.failures.get("close")
        self.closed = True
        self.close_count += 1
        if error is not None:
            raise error

    def _called(self, name: str) -> None:
        self.calls.append(name)
        error = self.state.uow_failures.get((self.number, name)) or self.state.failures.get(name)
        if error is not None:
            raise error


class _Catalog:
    def __init__(self, state: _State, observations: list[MigrationState | Exception]) -> None:
        self.state = state
        self.observations = observations
        self.calls: list[str] = []
        self.info_error: Exception | None = None
        self.allocate_error: Exception | None = None
        self.migrate_error: Exception | None = None
        self.migrate_result = MigrationState("service_store", 2, 2, "ready")
        self.migrate_results: list[MigrationState] = []
        self.locator: ServiceStoreLocator | None = None

    def info(self) -> StorageAdapterInfo:
        self._called("info")
        if self.info_error is not None:
            raise self.info_error
        return StorageAdapterInfo("sqlite", frozenset({"service_store"}))

    def allocate(self, service_id: UUID) -> ServiceStoreLocator:
        self._called("allocate")
        if self.allocate_error is not None:
            raise self.allocate_error
        if self.locator is not None:
            return self.locator
        return ServiceStoreLocator.for_service("sqlite", service_id)

    def inspect_migration(self, locator: ServiceStoreLocator) -> MigrationState:
        self._called("inspect_migration")
        value = self.observations.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def migrate(self, locator: ServiceStoreLocator) -> MigrationState:
        self._called("migrate")
        if self.migrate_error is not None:
            raise self.migrate_error
        if self.migrate_results:
            return self.migrate_results.pop(0)
        return self.migrate_result

    def _called(self, name: str) -> None:
        assert self.state.uows and all(uow.closed for uow in self.state.uows)
        self.calls.append(name)


class _Domain:
    def __init__(self, state: _State, observations: list[DomainSchemaState | Exception]) -> None:
        self.state = state
        self.observations = observations
        self.calls: list[str] = []
        self.materialise_error: Exception | None = None
        self.evolve_error: Exception | None = None

    def inspect(self, locator: ServiceStoreLocator, specification: object) -> DomainSchemaState:
        self._called("inspect")
        value = self.observations.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def materialise(self, locator: ServiceStoreLocator, specification: object) -> DomainSchemaState:
        self._called("materialise")
        if self.materialise_error is not None:
            raise self.materialise_error
        return DomainSchemaState("ready")

    def evolve(self, locator: ServiceStoreLocator, transition: object) -> DomainSchemaState:
        self._called("evolve")
        if self.evolve_error is not None:
            raise self.evolve_error
        return DomainSchemaState("ready")

    def _called(self, name: str) -> None:
        assert self.state.uows and all(uow.closed for uow in self.state.uows)
        self.calls.append(name)


def _coordinator(
    service: Service,
    foundations: list[MigrationState | Exception],
    domains: list[DomainSchemaState | Exception],
    *,
    clock: _Clock | None = None,
) -> tuple[LifecycleCoordinator, _State, _Catalog, _Domain]:
    state = _State(service)
    catalog = _Catalog(state, foundations)
    domain = _Domain(state, domains)
    return LifecycleCoordinator(state.uow, clock or _Clock(), catalog, domain), state, catalog, domain


def test_execution_result_is_strict_detached_and_exposes_only_retryability() -> None:
    completed = LifecycleExecutionResult("completed", _service())
    assert completed.retryable is False
    assert completed.service is not None
    object.__setattr__(completed.service.manifest, "schema_version", 99)  # type: ignore[union-attr]
    assert completed.service.schema_version == 1
    busy = LifecycleExecutionResult(LifecycleResultCategory.STORAGE_BUSY)
    assert busy.retryable is True and busy.service is None
    with pytest.raises(ValueError):
        LifecycleExecutionResult("completed")
    with pytest.raises(ValueError):
        LifecycleExecutionResult(LifecycleResultCategory.STALE_REVISION, _service())
    with pytest.raises(ValueError):
        LifecycleExecutionResult(type("HostileText", (str,), {})("completed"), _service())


def test_materialise_commits_closes_then_reobserves_each_physical_action() -> None:
    coordinator, state, catalog, domain = _coordinator(
        _service(),
        [
            MigrationState("service_store", 0, 2, "uninitialised"),
            MigrationState("service_store", 2, 2, "ready"),
            MigrationState("service_store", 2, 2, "ready"),
        ],
        [DomainSchemaState("unmaterialised"), DomainSchemaState("ready")],
    )
    result = coordinator.execute(
        MaterialiseCommand("principal", "test", _ID, 2, 1, "materialise-key")
    )
    assert result.category == "completed"
    assert result.service is not None and result.service.service_revision == 3
    assert catalog.calls == ["info", "allocate", "inspect_migration", "migrate", "inspect_migration", "inspect_migration"]
    assert domain.calls == ["inspect", "materialise", "inspect"]
    assert all(uow.closed and uow.close_count == 1 for uow in state.uows)
    assert state.uows[0].calls == ["resolve", "get", "commit", "close"]
    assert state.uows[1].calls == ["finalize_completed", "commit", "close"]


def test_materialise_adopts_prepared_wal_foundation_then_completes() -> None:
    coordinator, state, catalog, domain = _coordinator(
        _service(),
        [
            MigrationState("service_store", 1, 2, "dirty"),
            MigrationState("service_store", 2, 2, "ready"),
            MigrationState("service_store", 2, 2, "ready"),
        ],
        [DomainSchemaState("unmaterialised"), DomainSchemaState("ready")],
    )
    result = coordinator.execute(
        MaterialiseCommand("principal", "test", _ID, 2, 1, "prepared-wal-materialise")
    )
    assert result.category == "completed"
    assert catalog.calls.count("migrate") == 1
    assert domain.calls == ["inspect", "materialise", "inspect"]
    assert state.intent is not None and state.intent.phase is LifecyclePhase.COMPLETED


def test_materialise_exact_outdated_foundation_migrates_before_domain_observation() -> None:
    coordinator, state, catalog, domain = _coordinator(
        _service(),
        [
            MigrationState("service_store", 2, 3, "outdated"),
            MigrationState("service_store", 3, 3, "ready"),
            MigrationState("service_store", 3, 3, "ready"),
        ],
        [DomainSchemaState("unmaterialised"), DomainSchemaState("ready")],
    )
    catalog.migrate_result = MigrationState("service_store", 3, 3, "ready")

    result = coordinator.execute(
        MaterialiseCommand("principal", "test", _ID, 2, 1, "outdated-materialise")
    )

    assert result.category == "completed"
    assert catalog.calls == [
        "info",
        "allocate",
        "inspect_migration",
        "migrate",
        "inspect_migration",
        "inspect_migration",
    ]
    assert domain.calls == ["inspect", "materialise", "inspect"]
    assert state.intent is not None and state.intent.phase is LifecyclePhase.COMPLETED


def test_repeated_materialise_outdated_foundation_defers_without_terminal_poisoning() -> None:
    coordinator, state, catalog, domain = _coordinator(
        _service(),
        [
            MigrationState("service_store", 2, 3, "outdated"),
            MigrationState("service_store", 2, 3, "outdated"),
        ],
        [],
    )
    catalog.migrate_result = MigrationState("service_store", 2, 3, "outdated")

    result = coordinator.execute(
        MaterialiseCommand("principal", "test", _ID, 2, 1, "outdated-materialise")
    )

    assert result.category is LifecycleResultCategory.OPERATION_UNCERTAIN
    assert catalog.calls.count("migrate") == 1
    assert domain.calls == []
    assert state.intent is not None and state.intent.phase is LifecyclePhase.PREPARED
    assert all("finalize" not in call for uow in state.uows for call in uow.calls)


def test_evolve_adopts_prepared_wal_foundation_then_completes() -> None:
    coordinator, state, catalog, domain = _coordinator(
        _service(materialised=True),
        [
            MigrationState("service_store", 1, 2, "dirty"),
            MigrationState("service_store", 2, 2, "ready"),
        ],
        [DomainSchemaState("ready")],
    )
    result = coordinator.execute(
        EvolveCommand("principal", "test", _ID, 2, 1, "prepared-wal-evolve", _manifest(2))
    )
    assert result.category == "completed"
    assert catalog.calls.count("migrate") == 1
    assert domain.calls == ["inspect"]
    assert state.intent is not None and state.intent.phase is LifecyclePhase.COMPLETED


def test_evolve_adopts_exact_target_without_source_inspection() -> None:
    coordinator, state, catalog, domain = _coordinator(
        _service(materialised=True),
        [MigrationState("service_store", 2, 2, "ready")],
        [DomainSchemaState("ready")],
    )
    result = coordinator.execute(
        EvolveCommand("principal", "test", _ID, 2, 1, "evolve-key", _manifest(2))
    )
    assert result.category == "completed"
    assert result.service is not None and result.service.schema_version == 2
    assert catalog.calls == ["info", "allocate", "inspect_migration"]
    assert domain.calls == ["inspect"]
    assert all(uow.closed for uow in state.uows)


def test_inspection_migration_error_leaves_prepared_for_uncertain_retry() -> None:
    coordinator, state, catalog, domain = _coordinator(
        _service(), [StorageMigrationError()], []
    )
    result = coordinator.execute(
        MaterialiseCommand("principal", "test", _ID, 2, 1, "uncertain-key")
    )
    assert result.category is LifecycleResultCategory.OPERATION_UNCERTAIN
    assert state.intent is not None and state.intent.phase is LifecyclePhase.PREPARED
    assert domain.calls == []
    assert all(uow.closed for uow in state.uows)


def test_invalid_terminal_protocol_return_rolls_back_without_commit() -> None:
    coordinator, state, _, _ = _coordinator(
        _service(),
        [MigrationState("service_store", 2, 2, "ready")],
        [DomainSchemaState("ready")],
    )
    state.invalid_terminal = True
    result = coordinator.execute(
        MaterialiseCommand("principal", "test", _ID, 2, 1, "invalid-terminal")
    )
    assert result.category is LifecycleResultCategory.INTERNAL_FAILURE
    assert state.intent is not None and state.intent.phase is LifecyclePhase.PREPARED
    assert state.uows[-1].calls == ["finalize_completed", "rollback", "close"]


def test_application_and_transport_boundaries_do_not_import_storage_coordinator() -> None:
    root = Path(__file__).parents[2] / "src" / "aipcs_mcp"
    application = root / "application"
    for path in application.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert not any(module.startswith("aipcs_mcp.storage") for module in modules)
    for path in (root / "mcp_server.py", root / "cli.py"):
        assert "lifecycle_coordinator" not in path.read_text(encoding="utf-8")
    assert (
        "from .lifecycle_coordinator import LifecycleCoordinator"
        in (root / "runtime.py").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    ("outcome", "category"),
    [
        (ConflictClaim(), LifecycleResultCategory.CHANGED_FINGERPRINT),
        (StaleRevision(), LifecycleResultCategory.STALE_REVISION),
        (UnsupportedTransition(), LifecycleResultCategory.UNSUPPORTED_TRANSITION),
        (OperationInProgress(), LifecycleResultCategory.OPERATION_IN_PROGRESS),
    ],
)
def test_exact_nonprepared_registry_outcomes_never_touch_storage(
    outcome: object, category: LifecycleResultCategory
) -> None:
    coordinator, state, catalog, domain = _coordinator(_service(), [], [])
    state.admission_outcome = outcome
    result = coordinator.execute(MaterialiseCommand("principal", "test", _ID, 2, 1, "key"))
    assert result.category is category
    assert catalog.calls == [] and domain.calls == []
    assert state.uows[0].calls == ["resolve", "rollback", "close"]


def test_nonprepared_close_failure_is_bounded_without_repeating_close() -> None:
    coordinator, state, catalog, domain = _coordinator(_service(), [], [])
    state.admission_outcome = StaleRevision()
    state.failures["close"] = RuntimeError("close")

    result = coordinator.execute(MaterialiseCommand("principal", "test", _ID, 2, 1, "key"))

    assert result.category is LifecycleResultCategory.INTERNAL_FAILURE
    assert catalog.calls == [] and domain.calls == []
    assert state.uows[0].calls == ["resolve", "rollback", "close"]
    assert state.uows[0].close_count == 1


@pytest.mark.parametrize("phase", [LifecyclePhase.COMPLETED, LifecyclePhase.RECOVERY_REQUIRED])
def test_terminal_registry_claims_never_touch_storage(phase: LifecyclePhase) -> None:
    service = _service(materialised=True)
    command = EvolveCommand("principal", "test", _ID, 2, 1, "key", _manifest(2))
    intent = prepare_intent(command, command.target_manifest).with_phase(phase)
    if phase is LifecyclePhase.COMPLETED:
        terminal = replace(
            service,
            manifest=_manifest(2),
            schema_version=2,
            service_revision=3,
        )
        outcome: object = CompletedLifecycleClaim(intent, terminal)
    else:
        outcome = RecoveryRequiredLifecycleClaim(intent)
    coordinator, state, catalog, domain = _coordinator(service, [], [])
    state.admission_outcome = outcome
    result = coordinator.execute(command)
    expected: object = "completed" if phase is LifecyclePhase.COMPLETED else LifecycleResultCategory.RECOVERY_REQUIRED
    assert result.category == expected
    assert catalog.calls == [] and domain.calls == []


def test_malformed_execute_and_command_constructor_have_no_registry_side_effect() -> None:
    coordinator, state, _, _ = _coordinator(_service(), [], [])
    assert coordinator.execute(object()).category is LifecycleResultCategory.MALFORMED_INPUT  # type: ignore[arg-type]
    assert state.uows == []
    with pytest.raises(LifecycleContractError):
        MaterialiseCommand("principal", "test", _ID, 2, 2, "bad")


def test_missing_or_mismatched_prepared_source_becomes_terminal_recovery_before_storage() -> None:
    for missing, source in ((True, None), (False, replace(_service(), service_revision=3))):
        coordinator, state, catalog, domain = _coordinator(_service(), [], [])
        state.source_missing = missing
        state.source_override = source
        result = coordinator.execute(MaterialiseCommand("principal", "test", _ID, 2, 1, "key"))
        assert result.category is LifecycleResultCategory.RECOVERY_REQUIRED
        assert state.intent is not None and state.intent.phase is LifecyclePhase.RECOVERY_REQUIRED
        assert catalog.calls == [] and domain.calls == []


@pytest.mark.parametrize("wrong", ["info", "locator", "evolve_storage"])
def test_locator_backend_and_materialisation_identity_fail_closed(wrong: str) -> None:
    service = _service(materialised=wrong == "evolve_storage")
    command: MaterialiseCommand | EvolveCommand
    if wrong == "evolve_storage":
        service = replace(
            service,
            storage=MaterialisationStorage("postgresql", f"svc_{_ID.hex}"),
        )
        command = EvolveCommand("principal", "test", _ID, 2, 1, "key", _manifest(2))
    else:
        command = MaterialiseCommand("principal", "test", _ID, 2, 1, "key")
    coordinator, state, catalog, domain = _coordinator(service, [], [])
    if wrong == "info":
        catalog.info = lambda: StorageAdapterInfo("sqlite", frozenset({"registry"}))  # type: ignore[method-assign]
    if wrong == "locator":
        catalog.locator = ServiceStoreLocator.for_service("postgresql", _ID)
    result = coordinator.execute(command)
    assert result.category is LifecycleResultCategory.INTERNAL_FAILURE
    assert state.intent is not None and state.intent.phase is LifecyclePhase.PREPARED
    assert domain.calls == []


@pytest.mark.parametrize(
    ("method", "error", "category"),
    [
        ("info", StorageBusy(), LifecycleResultCategory.STORAGE_BUSY),
        ("info", StorageUnavailable(), LifecycleResultCategory.STORAGE_UNAVAILABLE),
        ("allocate", RuntimeError("locator"), LifecycleResultCategory.INTERNAL_FAILURE),
    ],
)
def test_locator_method_failures_leave_prepared_without_physical_io(
    method: str, error: Exception, category: LifecycleResultCategory
) -> None:
    coordinator, state, catalog, domain = _coordinator(_service(), [], [])
    if method == "info":
        catalog.info_error = error
    else:
        catalog.allocate_error = error
    result = coordinator.execute(MaterialiseCommand("principal", "test", _ID, 2, 1, "key"))
    assert result.category is category
    assert state.intent is not None and state.intent.phase is LifecyclePhase.PREPARED
    assert domain.calls == []


@pytest.mark.parametrize(
    ("state", "category"),
    [
        (MigrationState("service_store", 2, 2, "incompatible"), LifecycleResultCategory.RECOVERY_REQUIRED),
        (StorageBusy(), LifecycleResultCategory.STORAGE_BUSY),
        (StorageUnavailable(), LifecycleResultCategory.STORAGE_UNAVAILABLE),
        (StorageMigrationError(), LifecycleResultCategory.OPERATION_UNCERTAIN),
        (RuntimeError("inspection"), LifecycleResultCategory.INTERNAL_FAILURE),
    ],
)
def test_materialise_foundation_exact_and_uncertain_categories(
    state: MigrationState | Exception, category: LifecycleResultCategory
) -> None:
    coordinator, registry, catalog, domain = _coordinator(_service(), [state], [])
    result = coordinator.execute(MaterialiseCommand("principal", "test", _ID, 2, 1, "key"))
    assert result.category is category
    assert domain.calls == []
    if category is LifecycleResultCategory.RECOVERY_REQUIRED:
        assert registry.intent is not None and registry.intent.phase is LifecyclePhase.RECOVERY_REQUIRED
    else:
        assert registry.intent is not None and registry.intent.phase is LifecyclePhase.PREPARED
    if type(state) is StorageUnavailable:
        assert catalog.calls == ["info", "allocate", "inspect_migration"]


def test_transient_unavailable_foundation_reconciles_through_migration_boundary() -> None:
    coordinator, registry, catalog, domain = _coordinator(
        _service(),
        [
            StorageTransientJournal(),
            MigrationState("service_store", 3, 3, "ready"),
        ],
        [
            DomainSchemaState("unmaterialised"),
            DomainSchemaState("ready"),
        ],
    )

    result = coordinator.execute(
        MaterialiseCommand("principal", "test", _ID, 2, 1, "same-key")
    )

    assert result.category == "completed"
    assert catalog.calls == [
        "info",
        "allocate",
        "inspect_migration",
        "migrate",
        "inspect_migration",
    ]
    assert domain.calls == ["inspect", "materialise", "inspect"]
    assert registry.intent is not None
    assert registry.intent.phase is LifecyclePhase.COMPLETED


def test_transient_journal_then_unavailable_migration_is_post_action_uncertain() -> None:
    coordinator, registry, catalog, domain = _coordinator(
        _service(),
        [StorageTransientJournal(), StorageUnavailable()],
        [],
    )
    catalog.migrate_error = StorageUnavailable()

    result = coordinator.execute(
        MaterialiseCommand("principal", "test", _ID, 2, 1, "same-key")
    )

    assert result.category is LifecycleResultCategory.OPERATION_UNCERTAIN
    assert catalog.calls == [
        "info",
        "allocate",
        "inspect_migration",
        "migrate",
        "inspect_migration",
    ]
    assert domain.calls == []
    assert registry.intent is not None
    assert registry.intent.phase is LifecyclePhase.PREPARED


def test_post_action_reinspection_converges_an_exact_peer_completed_foundation() -> None:
    coordinator, registry, catalog, domain = _coordinator(
        _service(),
        [
            StorageTransientJournal(),
            MigrationState("service_store", 3, 3, "ready"),
            MigrationState("service_store", 3, 3, "ready"),
        ],
        [
            DomainSchemaState("unmaterialised"),
            DomainSchemaState("ready"),
        ],
    )
    catalog.migrate_error = StorageUnavailable()

    result = coordinator.execute(
        MaterialiseCommand("principal", "test", _ID, 2, 1, "same-key")
    )

    assert result.category == "completed"
    assert catalog.calls == [
        "info",
        "allocate",
        "inspect_migration",
        "migrate",
        "inspect_migration",
        "inspect_migration",
    ]
    assert domain.calls == ["inspect", "materialise", "inspect"]
    assert registry.intent is not None
    assert registry.intent.phase is LifecyclePhase.COMPLETED


def test_transient_journal_dirty_result_cannot_terminalise_recovery() -> None:
    coordinator, registry, catalog, domain = _coordinator(
        _service(),
        [
            StorageTransientJournal(),
            MigrationState("service_store", 2, 3, "dirty"),
        ],
        [],
    )
    catalog.migrate_result = MigrationState("service_store", 2, 3, "dirty")

    result = coordinator.execute(
        MaterialiseCommand("principal", "test", _ID, 2, 1, "same-key")
    )

    assert result.category is LifecycleResultCategory.OPERATION_UNCERTAIN
    assert catalog.calls == [
        "info",
        "allocate",
        "inspect_migration",
        "migrate",
        "inspect_migration",
    ]
    assert domain.calls == []
    assert registry.intent is not None
    assert registry.intent.phase is LifecyclePhase.PREPARED


def test_ordinary_prepared_r3_dirt_reconciles_without_terminal_registry_poisoning() -> None:
    coordinator, registry, catalog, domain = _coordinator(
        _service(),
        [
            MigrationState("service_store", 0, 3, "uninitialised"),
            MigrationState("service_store", 2, 3, "dirty"),
            MigrationState("service_store", 3, 3, "ready"),
            MigrationState("service_store", 3, 3, "ready"),
        ],
        [
            DomainSchemaState("unmaterialised"),
            DomainSchemaState("ready"),
        ],
    )
    catalog.migrate_results = [
        MigrationState("service_store", 2, 3, "dirty"),
        MigrationState("service_store", 3, 3, "ready"),
    ]

    result = coordinator.execute(
        MaterialiseCommand("principal", "test", _ID, 2, 1, "same-key")
    )

    assert result.category == "completed"
    assert catalog.calls == [
        "info",
        "allocate",
        "inspect_migration",
        "migrate",
        "inspect_migration",
        "migrate",
        "inspect_migration",
        "inspect_migration",
    ]
    assert domain.calls == ["inspect", "materialise", "inspect"]
    assert registry.intent is not None
    assert registry.intent.phase is LifecyclePhase.COMPLETED


def test_repeated_resumable_r3_dirt_defers_without_terminal_registry_poisoning() -> None:
    coordinator, registry, catalog, domain = _coordinator(
        _service(),
        [
            MigrationState("service_store", 0, 3, "uninitialised"),
            MigrationState("service_store", 2, 3, "dirty"),
            MigrationState("service_store", 2, 3, "dirty"),
        ],
        [],
    )
    catalog.migrate_results = [
        MigrationState("service_store", 2, 3, "dirty"),
        MigrationState("service_store", 2, 3, "dirty"),
    ]

    result = coordinator.execute(
        MaterialiseCommand("principal", "test", _ID, 2, 1, "same-key")
    )

    assert result.category is LifecycleResultCategory.OPERATION_UNCERTAIN
    assert catalog.calls.count("migrate") == 2
    assert domain.calls == []
    assert registry.intent is not None
    assert registry.intent.phase is LifecyclePhase.PREPARED


def test_domain_transient_journal_reconciles_foundation_then_reinspects() -> None:
    coordinator, registry, catalog, domain = _coordinator(
        _service(),
        [
            MigrationState("service_store", 3, 3, "ready"),
            MigrationState("service_store", 3, 3, "ready"),
        ],
        [
            StorageTransientJournal(),
            DomainSchemaState("ready"),
        ],
    )

    result = coordinator.execute(
        MaterialiseCommand("principal", "test", _ID, 2, 1, "same-key")
    )

    assert result.category == "completed"
    assert catalog.calls == [
        "info",
        "allocate",
        "inspect_migration",
        "inspect_migration",
    ]
    assert domain.calls == ["inspect", "inspect"]
    assert registry.intent is not None
    assert registry.intent.phase is LifecyclePhase.COMPLETED


@pytest.mark.parametrize("materialised", [False, True])
def test_repeated_dirty_foundation_defers_without_terminal_registry_poisoning(
    materialised: bool,
) -> None:
    coordinator, state, catalog, domain = _coordinator(
        _service(materialised=materialised),
        [
            MigrationState("service_store", 3, 3, "dirty"),
            MigrationState("service_store", 3, 3, "dirty"),
            MigrationState("service_store", 3, 3, "dirty"),
        ],
        [],
    )
    command: MaterialiseCommand | EvolveCommand
    if materialised:
        command = EvolveCommand("principal", "test", _ID, 2, 1, "persistent-dirty", _manifest(2))
    else:
        command = MaterialiseCommand("principal", "test", _ID, 2, 1, "persistent-dirty")
    catalog.migrate_result = MigrationState("service_store", 3, 3, "dirty")
    result = coordinator.execute(command)
    assert result.category is LifecycleResultCategory.OPERATION_UNCERTAIN
    assert catalog.calls.count("migrate") == 2
    assert domain.calls == []
    assert state.intent is not None and state.intent.phase is LifecyclePhase.PREPARED


@pytest.mark.parametrize(
    ("state", "category"),
    [
        (DomainSchemaState("incompatible"), LifecycleResultCategory.RECOVERY_REQUIRED),
        (StorageBusy(), LifecycleResultCategory.STORAGE_BUSY),
        (StorageUnavailable(), LifecycleResultCategory.STORAGE_UNAVAILABLE),
        (StorageMigrationError(), LifecycleResultCategory.OPERATION_UNCERTAIN),
        (RuntimeError("inspection"), LifecycleResultCategory.INTERNAL_FAILURE),
    ],
)
def test_materialise_domain_exact_and_uncertain_categories(
    state: DomainSchemaState | Exception, category: LifecycleResultCategory
) -> None:
    coordinator, registry, _, domain = _coordinator(
        _service(), [MigrationState("service_store", 2, 2, "ready")], [state]
    )
    result = coordinator.execute(MaterialiseCommand("principal", "test", _ID, 2, 1, "key"))
    assert result.category is category
    assert domain.calls == ["inspect"]
    assert registry.intent is not None


def test_evolve_source_inspection_and_transition_are_target_first() -> None:
    coordinator, state, catalog, domain = _coordinator(
        _service(materialised=True),
        [MigrationState("service_store", 2, 2, "ready"), MigrationState("service_store", 2, 2, "ready")],
        [DomainSchemaState("incompatible"), DomainSchemaState("ready"), DomainSchemaState("ready")],
    )
    result = coordinator.execute(
        EvolveCommand("principal", "test", _ID, 2, 1, "key", _manifest(2))
    )
    assert result.category == "completed"
    assert domain.calls == ["inspect", "inspect", "evolve", "inspect"]
    assert catalog.calls.count("inspect_migration") == 2
    assert all(uow.closed for uow in state.uows)


@pytest.mark.parametrize(
    ("status", "revision", "target_revision"),
    (("uninitialised", 0, 2), ("incompatible", 2, 2)),
)
def test_evolve_nonready_foundation_is_terminal_recovery_without_domain_io(
    status: str, revision: int, target_revision: int
) -> None:
    coordinator, state, _, domain = _coordinator(
        _service(materialised=True),
        [MigrationState("service_store", revision, target_revision, status)],
        [],
    )
    result = coordinator.execute(EvolveCommand("principal", "test", _ID, 2, 1, "key", _manifest(2)))
    assert result.category is LifecycleResultCategory.RECOVERY_REQUIRED
    assert state.intent is not None and state.intent.phase is LifecyclePhase.RECOVERY_REQUIRED
    assert domain.calls == []


def test_evolve_exact_outdated_foundation_migrates_before_schema_observation() -> None:
    coordinator, state, catalog, domain = _coordinator(
        _service(materialised=True),
        [
            MigrationState("service_store", 2, 3, "outdated"),
            MigrationState("service_store", 3, 3, "ready"),
        ],
        [DomainSchemaState("ready")],
    )
    result = coordinator.execute(EvolveCommand("principal", "test", _ID, 2, 1, "key", _manifest(2)))
    assert result.category == "completed"
    assert catalog.calls == ["info", "allocate", "inspect_migration", "migrate", "inspect_migration"]
    assert domain.calls == ["inspect"]
    assert state.intent is not None and state.intent.phase is LifecyclePhase.COMPLETED


@pytest.mark.parametrize(
    ("target", "source", "category"),
    [
        (DomainSchemaState("unmaterialised"), DomainSchemaState("unmaterialised"), LifecycleResultCategory.RECOVERY_REQUIRED),
        (DomainSchemaState("incompatible"), DomainSchemaState("incompatible"), LifecycleResultCategory.RECOVERY_REQUIRED),
        (StorageMigrationError(), None, LifecycleResultCategory.OPERATION_UNCERTAIN),
    ],
)
def test_evolve_exact_and_transient_target_paths(
    target: DomainSchemaState | Exception,
    source: DomainSchemaState | None,
    category: LifecycleResultCategory,
) -> None:
    observations: list[DomainSchemaState | Exception] = [target]
    if source is not None:
        observations.append(source)
    coordinator, registry, _, domain = _coordinator(
        _service(materialised=True), [MigrationState("service_store", 2, 2, "ready")], observations
    )
    result = coordinator.execute(EvolveCommand("principal", "test", _ID, 2, 1, "key", _manifest(2)))
    assert result.category is category
    assert registry.intent is not None
    if isinstance(target, Exception):
        assert domain.calls == ["inspect"]


def test_once_per_action_returns_uncertain_without_repeating_migration() -> None:
    coordinator, state, catalog, domain = _coordinator(
        _service(),
        [
            MigrationState("service_store", 0, 2, "uninitialised"),
            MigrationState("service_store", 0, 2, "uninitialised"),
        ],
        [],
    )
    result = coordinator.execute(MaterialiseCommand("principal", "test", _ID, 2, 1, "key"))
    assert result.category is LifecycleResultCategory.OPERATION_UNCERTAIN
    assert catalog.calls.count("migrate") == 1 and domain.calls == []
    assert state.intent is not None and state.intent.phase is LifecyclePhase.PREPARED


def test_successful_foundation_action_then_unavailable_reinspection_is_uncertain() -> None:
    coordinator, state, catalog, domain = _coordinator(
        _service(),
        [
            MigrationState("service_store", 0, 2, "uninitialised"),
            StorageUnavailable(),
        ],
        [],
    )

    result = coordinator.execute(
        MaterialiseCommand("principal", "test", _ID, 2, 1, "key")
    )

    assert result.category is LifecycleResultCategory.OPERATION_UNCERTAIN
    assert catalog.calls == [
        "info",
        "allocate",
        "inspect_migration",
        "migrate",
        "inspect_migration",
    ]
    assert domain.calls == []
    assert state.intent is not None and state.intent.phase is LifecyclePhase.PREPARED


def test_successful_domain_action_then_unavailable_reinspection_is_uncertain() -> None:
    coordinator, state, catalog, domain = _coordinator(
        _service(),
        [
            MigrationState("service_store", 2, 2, "ready"),
            MigrationState("service_store", 2, 2, "ready"),
        ],
        [DomainSchemaState("unmaterialised"), StorageUnavailable()],
    )

    result = coordinator.execute(
        MaterialiseCommand("principal", "test", _ID, 2, 1, "key")
    )

    assert result.category is LifecycleResultCategory.OPERATION_UNCERTAIN
    assert catalog.calls == [
        "info",
        "allocate",
        "inspect_migration",
        "inspect_migration",
    ]
    assert domain.calls == ["inspect", "materialise", "inspect"]
    assert state.intent is not None and state.intent.phase is LifecyclePhase.PREPARED


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (StorageBusy(), LifecycleResultCategory.STORAGE_BUSY),
        (StorageUnavailable(), LifecycleResultCategory.OPERATION_UNCERTAIN),
        (StorageMigrationError(), LifecycleResultCategory.OPERATION_UNCERTAIN),
        (StorageContractError(), LifecycleResultCategory.INTERNAL_FAILURE),
        (RuntimeError("after action"), LifecycleResultCategory.OPERATION_UNCERTAIN),
    ],
)
def test_post_action_error_mapping(error: Exception, category: LifecycleResultCategory) -> None:
    coordinator, registry, catalog, _ = _coordinator(
        _service(), [MigrationState("service_store", 0, 2, "uninitialised")], []
    )
    catalog.migrate_error = error
    result = coordinator.execute(MaterialiseCommand("principal", "test", _ID, 2, 1, "key"))
    assert result.category is category
    assert registry.intent is not None and registry.intent.phase is LifecyclePhase.PREPARED


@pytest.mark.parametrize(
    ("kind", "error", "category"),
    [
        ("materialise", StorageBusy(), LifecycleResultCategory.STORAGE_BUSY),
        ("materialise", StorageContractError(), LifecycleResultCategory.INTERNAL_FAILURE),
        ("materialise", RuntimeError("after ddl"), LifecycleResultCategory.OPERATION_UNCERTAIN),
        ("evolve", StorageBusy(), LifecycleResultCategory.STORAGE_BUSY),
        ("evolve", StorageContractError(), LifecycleResultCategory.INTERNAL_FAILURE),
        ("evolve", RuntimeError("after ddl"), LifecycleResultCategory.OPERATION_UNCERTAIN),
    ],
)
def test_domain_post_action_error_mapping(
    kind: str, error: Exception, category: LifecycleResultCategory
) -> None:
    if kind == "materialise":
        coordinator, registry, _, domain = _coordinator(
            _service(),
            [MigrationState("service_store", 2, 2, "ready")],
            [DomainSchemaState("unmaterialised")],
        )
        domain.materialise_error = error
        command: MaterialiseCommand | EvolveCommand = MaterialiseCommand(
            "principal", "test", _ID, 2, 1, "key"
        )
    else:
        coordinator, registry, _, domain = _coordinator(
            _service(materialised=True),
            [MigrationState("service_store", 2, 2, "ready")],
            [DomainSchemaState("incompatible"), DomainSchemaState("ready")],
        )
        domain.evolve_error = error
        command = EvolveCommand("principal", "test", _ID, 2, 1, "key", _manifest(2))
    result = coordinator.execute(command)
    assert result.category is category
    assert registry.intent is not None and registry.intent.phase is LifecyclePhase.PREPARED


@pytest.mark.parametrize(
    ("stage", "error", "category"),
    [
        ("resolve", RuntimeError("resolve"), LifecycleResultCategory.INTERNAL_FAILURE),
        ("get", RuntimeError("get"), LifecycleResultCategory.INTERNAL_FAILURE),
        ("commit", RuntimeError("commit"), LifecycleResultCategory.OPERATION_UNCERTAIN),
        ("close", RuntimeError("close"), LifecycleResultCategory.OPERATION_UNCERTAIN),
    ],
)
def test_admission_uow_failures_are_bounded(stage: str, error: Exception, category: LifecycleResultCategory) -> None:
    coordinator, state, catalog, domain = _coordinator(_service(), [], [])
    state.failures[stage] = error
    result = coordinator.execute(MaterialiseCommand("principal", "test", _ID, 2, 1, "key"))
    assert result.category is category
    assert catalog.calls == [] and domain.calls == []
    assert all(uow.closed for uow in state.uows)


def test_pre_admission_storage_unavailable_remains_storage_unavailable() -> None:
    coordinator, state, catalog, domain = _coordinator(_service(), [], [])
    state.uow_error = StorageUnavailable()

    result = coordinator.execute(
        MaterialiseCommand("principal", "test", _ID, 2, 1, "key")
    )

    assert result.category is LifecycleResultCategory.STORAGE_UNAVAILABLE
    assert catalog.calls == [] and domain.calls == []


@pytest.mark.parametrize(
    ("stage", "category"),
    [
        ("open", LifecycleResultCategory.INTERNAL_FAILURE),
        ("finalize_completed", LifecycleResultCategory.INTERNAL_FAILURE),
        ("commit", LifecycleResultCategory.OPERATION_UNCERTAIN),
        ("close", LifecycleResultCategory.OPERATION_UNCERTAIN),
    ],
)
def test_terminal_completion_uow_failures_are_bounded(stage: str, category: LifecycleResultCategory) -> None:
    coordinator, state, _, _ = _coordinator(
        _service(), [MigrationState("service_store", 2, 2, "ready")], [DomainSchemaState("ready")]
    )
    state.uow_failures[(2, stage)] = RuntimeError(stage)
    result = coordinator.execute(MaterialiseCommand("principal", "test", _ID, 2, 1, "key"))
    assert result.category is category
    assert state.intent is not None
    if stage != "close":
        assert state.intent.phase is LifecyclePhase.PREPARED
    if stage != "open":
        assert state.uows[-1].closed


def test_terminal_completion_storage_unavailable_is_post_action_uncertain() -> None:
    coordinator, state, _, _ = _coordinator(
        _service(),
        [MigrationState("service_store", 3, 3, "ready")],
        [DomainSchemaState("ready")],
    )
    state.uow_failures[(2, "open")] = StorageUnavailable()

    result = coordinator.execute(
        MaterialiseCommand("principal", "test", _ID, 2, 1, "key")
    )

    assert result.category is LifecycleResultCategory.OPERATION_UNCERTAIN
    assert state.intent is not None and state.intent.phase is LifecyclePhase.PREPARED


def test_terminal_recovery_uow_failure_after_incompatible_foundation_is_internal() -> None:
    coordinator, state, catalog, domain = _coordinator(
        _service(),
        [
            MigrationState("service_store", 2, 3, "incompatible"),
        ],
        [],
    )
    state.uow_failures[(2, "finalize_recovery")] = RuntimeError("recovery")
    result = coordinator.execute(MaterialiseCommand("principal", "test", _ID, 2, 1, "key"))
    assert result.category is LifecycleResultCategory.INTERNAL_FAILURE
    assert state.intent is not None and state.intent.phase is LifecyclePhase.PREPARED
    assert catalog.calls.count("migrate") == 0
    assert domain.calls == []


@pytest.mark.parametrize("terminal", ["completed", "recovery_required"])
def test_terminal_races_return_exact_registry_terminal_result(terminal: str) -> None:
    coordinator, state, _, _ = _coordinator(
        _service(), [MigrationState("service_store", 2, 2, "ready")], [DomainSchemaState("ready")]
    )
    intent = prepare_intent(MaterialiseCommand("principal", "test", _ID, 2, 1, "key"), _manifest())
    if terminal == "completed":
        completed = replace(
            _service(),
            design_state="materialised",
            materialised_at=_AT,
            storage=MaterialisationStorage("sqlite", f"svc_{_ID.hex}"),
            service_revision=3,
        )
        state.terminal_override = CompletedLifecycleClaim(intent.with_phase(LifecyclePhase.COMPLETED), completed)
    else:
        state.terminal_override = RecoveryRequiredLifecycleClaim(
            intent.with_phase(LifecyclePhase.RECOVERY_REQUIRED)
        )
    result = coordinator.execute(MaterialiseCommand("principal", "test", _ID, 2, 1, "key"))
    expected: object = "completed" if terminal == "completed" else LifecycleResultCategory.RECOVERY_REQUIRED
    assert result.category == expected


def test_terminal_clock_and_open_failures_leave_prepared_without_storage_repair() -> None:
    coordinator, state, _, _ = _coordinator(
        _service(), [MigrationState("service_store", 2, 2, "ready")], [DomainSchemaState("ready")],
        clock=_Clock(RuntimeError("clock")),
    )
    result = coordinator.execute(MaterialiseCommand("principal", "test", _ID, 2, 1, "key"))
    assert result.category is LifecycleResultCategory.INTERNAL_FAILURE
    assert state.intent is not None and state.intent.phase is LifecyclePhase.PREPARED

    coordinator, state, _, _ = _coordinator(
        _service(), [MigrationState("service_store", 2, 2, "ready")], [DomainSchemaState("ready")]
    )
    state.uow_failures[(2, "open")] = RuntimeError("terminal open")
    result = coordinator.execute(MaterialiseCommand("principal", "test", _ID, 2, 1, "key"))
    assert result.category is LifecycleResultCategory.INTERNAL_FAILURE
    assert state.intent is not None and state.intent.phase is LifecyclePhase.PREPARED
