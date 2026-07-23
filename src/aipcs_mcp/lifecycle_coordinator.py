"""Private, re-observing lifecycle coordinator; deliberately uncomposed."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress

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
from aipcs_mcp.application.ports import Clock, RegistryUnitOfWork, UowFactory
from aipcs_mcp.lifecycle import (
    DomainObservation,
    EvolveCommand,
    EvolveRecoveryObservation,
    FoundationObservation,
    LifecycleCommand,
    LifecycleIntent,
    LifecycleKind,
    LifecycleResultCategory,
    MaterialiseCommand,
    MaterialiseRecoveryObservation,
    RecoveryAction,
    plan_recovery,
)
from aipcs_mcp.relational import (
    RelationalSpecification,
    RelationalTransition,
    classify_transition,
    compile_manifest,
)
from aipcs_mcp.storage.contracts import (
    DomainSchemaState,
    DomainSchemaStore,
    MigrationState,
    ServiceStoreCatalog,
    ServiceStoreLocator,
    StorageAdapterInfo,
)
from aipcs_mcp.storage.errors import (
    StorageBusy,
    StorageContractError,
    StorageMigrationError,
    StorageUnavailable,
)


class LifecycleCoordinator:
    """Coordinate one admitted lifecycle request without exposing storage mechanics."""

    def __init__(
        self,
        uows: UowFactory,
        clock: Clock,
        catalog: ServiceStoreCatalog,
        domain: DomainSchemaStore,
    ) -> None:
        if (
            not callable(uows)
            or not callable(getattr(clock, "now", None))
            or not callable(getattr(catalog, "info", None))
            or not callable(getattr(catalog, "allocate", None))
            or not callable(getattr(catalog, "inspect_migration", None))
            or not callable(getattr(catalog, "migrate", None))
            or not callable(getattr(domain, "inspect", None))
            or not callable(getattr(domain, "materialise", None))
            or not callable(getattr(domain, "evolve", None))
        ):
            raise ValueError("Lifecycle coordinator dependencies are invalid.")
        self._uows = uows
        self._clock = clock
        self._catalog = catalog
        self._domain = domain

    def execute(self, command: LifecycleCommand) -> LifecycleExecutionResult:
        """Run one finite reconciliation attempt for a typed lifecycle command."""

        if type(command) not in {MaterialiseCommand, EvolveCommand}:
            return _result(LifecycleResultCategory.MALFORMED_INPUT)

        admitted = self._admit(command)
        if type(admitted) is LifecycleExecutionResult:
            return admitted
        intent, source = admitted
        if not _source_matches(intent, source):
            return self._finalize_recovery(intent)

        located = self._locator(intent, source)
        if type(located) is LifecycleExecutionResult:
            return located
        locator = located

        compiled = self._compile(intent, source)
        if type(compiled) is LifecycleExecutionResult:
            return compiled
        target, transition = compiled
        attempted: set[RecoveryAction] = set()

        while True:
            try:
                observed = self._observe(intent, locator, target, transition)
                if type(observed) is LifecycleExecutionResult:
                    return observed
                plan = plan_recovery(intent, observed)
            except Exception:
                return _result(LifecycleResultCategory.INTERNAL_FAILURE)
            if plan.action is RecoveryAction.DEFER_OBSERVATION:
                if plan.result_category is None:
                    return _result(LifecycleResultCategory.INTERNAL_FAILURE)
                return _result(plan.result_category)
            if plan.action is RecoveryAction.RECOVERY_REQUIRED:
                return self._finalize_recovery(intent)
            if plan.action is RecoveryAction.FINALIZE_REGISTRY:
                return self._finalize_completed(intent, locator)
            if plan.action is RecoveryAction.REPLAY_COMPLETE:
                return _result(LifecycleResultCategory.INTERNAL_FAILURE)
            if plan.action in attempted:
                if (
                    plan.action is RecoveryAction.PREPARE_FOUNDATION
                    and observed.foundation is FoundationObservation.DIRTY
                ):
                    # Migrate was already attempted and its return deliberately discarded.
                    # Persisting exact dirt after the fresh inspection needs operator recovery.
                    return self._finalize_recovery(intent)
                return _result(LifecycleResultCategory.OPERATION_UNCERTAIN)
            attempted.add(plan.action)
            action_result = self._apply(plan.action, locator, target, transition)
            if action_result is not None:
                return action_result

    def _admit(
        self, command: LifecycleCommand
    ) -> tuple[LifecycleIntent, Service] | LifecycleExecutionResult:
        try:
            uow = self._uows()
        except Exception as error:
            return _before_commit_error(error)
        try:
            outcome = uow.mutations.resolve_or_admit(command)
            if type(outcome) is PreparedLifecycleClaim:
                source = uow.services.get(command.principal_id, command.service_id)
                if type(source) is not Service:
                    source = None
                return self._commit_prepared(uow, outcome.intent, source)
            result = _registry_outcome(outcome)
            return self._close_nonmutating(uow, result)
        except Exception as error:
            _rollback_close(uow)
            return _before_commit_error(error)

    def _commit_prepared(
        self, uow: RegistryUnitOfWork, intent: LifecycleIntent, source: Service | None
    ) -> tuple[LifecycleIntent, Service] | LifecycleExecutionResult:
        try:
            uow.commit()
        except Exception:
            _close_only(uow)
            return _result(LifecycleResultCategory.OPERATION_UNCERTAIN)
        try:
            uow.close()
        except Exception:
            return _result(LifecycleResultCategory.OPERATION_UNCERTAIN)
        if source is None:
            return self._finalize_recovery(intent)
        return intent, source

    @staticmethod
    def _close_nonmutating(
        uow: RegistryUnitOfWork, result: LifecycleExecutionResult
    ) -> LifecycleExecutionResult:
        try:
            uow.rollback()
            uow.close()
        except Exception as error:
            return _before_commit_error(error)
        return result

    def _locator(
        self, intent: LifecycleIntent, source: Service
    ) -> ServiceStoreLocator | LifecycleExecutionResult:
        try:
            info = self._catalog.info()
            locator = self._catalog.allocate(intent.service_id)
        except StorageBusy:
            return _result(LifecycleResultCategory.STORAGE_BUSY)
        except StorageUnavailable:
            return _result(LifecycleResultCategory.STORAGE_UNAVAILABLE)
        except Exception:
            return _result(LifecycleResultCategory.INTERNAL_FAILURE)
        if not _valid_locator(info, locator, intent):
            return _result(LifecycleResultCategory.INTERNAL_FAILURE)
        if intent.kind is LifecycleKind.EVOLVE and (
            source.storage is None
            or source.storage.backend != locator.backend
            or source.storage.namespace != locator.namespace
        ):
            return _result(LifecycleResultCategory.INTERNAL_FAILURE)
        return locator

    @staticmethod
    def _compile(
        intent: LifecycleIntent, source: Service
    ) -> tuple[RelationalSpecification, RelationalTransition | None] | LifecycleExecutionResult:
        try:
            target = compile_manifest(intent.target_manifest)
            if intent.kind is LifecycleKind.MATERIALISE:
                return target, None
            if source.manifest is None:
                return _result(LifecycleResultCategory.INTERNAL_FAILURE)
            return target, classify_transition(source.manifest, intent.target_manifest)
        except Exception:
            # Admission already proved relational support; reaching this branch is an internal defect.
            return _result(LifecycleResultCategory.INTERNAL_FAILURE)

    def _observe(
        self,
        intent: LifecycleIntent,
        locator: ServiceStoreLocator,
        target: RelationalSpecification,
        transition: RelationalTransition | None,
    ) -> MaterialiseRecoveryObservation | EvolveRecoveryObservation | LifecycleExecutionResult:
        foundation = self._foundation(locator)
        if type(foundation) is LifecycleExecutionResult:
            return foundation
        if intent.kind is LifecycleKind.MATERIALISE:
            domain = (
                self._domain_state(locator, target)
                if foundation is FoundationObservation.READY
                else DomainObservation.NOT_OBSERVED
            )
            if type(domain) is LifecycleExecutionResult:
                return domain
            return MaterialiseRecoveryObservation(intent.phase, foundation, domain)

        if foundation is not FoundationObservation.READY:
            return EvolveRecoveryObservation(
                intent.phase,
                foundation,
                DomainObservation.NOT_OBSERVED,
                DomainObservation.NOT_OBSERVED,
            )
        target_state = self._domain_state(locator, target)
        if type(target_state) is LifecycleExecutionResult:
            return target_state
        source_state = DomainObservation.NOT_OBSERVED
        if target_state in {DomainObservation.UNMATERIALISED, DomainObservation.INCOMPATIBLE}:
            if transition is None:
                return _result(LifecycleResultCategory.INTERNAL_FAILURE)
            source_state = self._domain_state(locator, transition.current)
            if type(source_state) is LifecycleExecutionResult:
                return source_state
        return EvolveRecoveryObservation(intent.phase, foundation, target_state, source_state)

    def _foundation(self, locator: ServiceStoreLocator) -> FoundationObservation | LifecycleExecutionResult:
        try:
            state = self._catalog.inspect_migration(locator)
        except StorageBusy:
            return FoundationObservation.BUSY
        except StorageUnavailable:
            return FoundationObservation.UNAVAILABLE
        except StorageMigrationError:
            return FoundationObservation.UNCERTAIN
        except Exception:
            return _result(LifecycleResultCategory.INTERNAL_FAILURE)
        if type(state) is not MigrationState or state.component != "service_store":
            return _result(LifecycleResultCategory.INTERNAL_FAILURE)
        return {
            "uninitialised": FoundationObservation.UNINITIALISED,
            "ready": FoundationObservation.READY,
            "dirty": FoundationObservation.DIRTY,
            "incompatible": FoundationObservation.INCOMPATIBLE,
        }.get(state.status, _result(LifecycleResultCategory.INTERNAL_FAILURE))

    def _domain_state(
        self, locator: ServiceStoreLocator, specification: RelationalSpecification
    ) -> DomainObservation | LifecycleExecutionResult:
        try:
            state = self._domain.inspect(locator, specification)
        except StorageBusy:
            return DomainObservation.BUSY
        except StorageUnavailable:
            return DomainObservation.UNAVAILABLE
        except StorageMigrationError:
            return DomainObservation.UNCERTAIN
        except Exception:
            return _result(LifecycleResultCategory.INTERNAL_FAILURE)
        if type(state) is not DomainSchemaState:
            return _result(LifecycleResultCategory.INTERNAL_FAILURE)
        return {
            "unmaterialised": DomainObservation.UNMATERIALISED,
            "ready": DomainObservation.READY,
            "incompatible": DomainObservation.INCOMPATIBLE,
        }.get(state.status, _result(LifecycleResultCategory.INTERNAL_FAILURE))

    def _apply(
        self,
        action: RecoveryAction,
        locator: ServiceStoreLocator,
        target: RelationalSpecification,
        transition: RelationalTransition | None,
    ) -> LifecycleExecutionResult | None:
        try:
            if action is RecoveryAction.PREPARE_FOUNDATION:
                self._catalog.migrate(locator)
            elif action is RecoveryAction.APPLY_INITIAL_SCHEMA:
                self._domain.materialise(locator, target)
            elif action is RecoveryAction.APPLY_TRANSITION and transition is not None:
                self._domain.evolve(locator, transition)
            else:
                return _result(LifecycleResultCategory.INTERNAL_FAILURE)
        except StorageBusy:
            return _result(LifecycleResultCategory.STORAGE_BUSY)
        except StorageContractError:
            return _result(LifecycleResultCategory.INTERNAL_FAILURE)
        except Exception:
            return _result(LifecycleResultCategory.OPERATION_UNCERTAIN)
        return None

    def _finalize_completed(
        self, intent: LifecycleIntent, locator: ServiceStoreLocator
    ) -> LifecycleExecutionResult:
        try:
            at = self._clock.now()
            completion = (
                MaterialiseCompletion(intent, at, MaterialisationStorage(locator.backend, locator.namespace))
                if intent.kind is LifecycleKind.MATERIALISE
                else EvolveCompletion(intent, at)
            )
        except Exception:
            return _result(LifecycleResultCategory.INTERNAL_FAILURE)
        return self._terminal_write(lambda uow: uow.mutations.finalize_completed(completion))

    def _finalize_recovery(self, intent: LifecycleIntent) -> LifecycleExecutionResult:
        try:
            at = self._clock.now()
        except Exception:
            return _result(LifecycleResultCategory.INTERNAL_FAILURE)
        return self._terminal_write(
            lambda uow: uow.mutations.finalize_recovery_required(intent, at)
        )

    def _terminal_write(
        self,
        operation: Callable[[RegistryUnitOfWork], CompletedLifecycleClaim | RecoveryRequiredLifecycleClaim],
    ) -> LifecycleExecutionResult:
        try:
            uow = self._uows()
        except Exception as error:
            return _before_commit_error(error)
        try:
            terminal = operation(uow)
        except Exception as error:
            _rollback_close(uow)
            return _before_commit_error(error)
        if type(terminal) not in {CompletedLifecycleClaim, RecoveryRequiredLifecycleClaim}:
            _rollback_close(uow)
            return _result(LifecycleResultCategory.INTERNAL_FAILURE)
        try:
            uow.commit()
        except Exception:
            _close_only(uow)
            return _result(LifecycleResultCategory.OPERATION_UNCERTAIN)
        try:
            uow.close()
        except Exception:
            return _result(LifecycleResultCategory.OPERATION_UNCERTAIN)
        return _registry_outcome(terminal)


def _source_matches(intent: LifecycleIntent, source: Service) -> bool:
    if (
        source.principal_id != intent.principal_id
        or source.service_id != intent.service_id
        or source.operational_status != "active"
        or source.service_revision != intent.expected_service_revision
        or source.schema_version != intent.expected_schema_version
        or source.manifest is None
    ):
        return False
    if intent.kind is LifecycleKind.MATERIALISE:
        return (
            source.design_state == "seeded"
            and source.manifest == intent.target_manifest
            and source.schema_version == 1
            and source.materialised_at is None
            and source.storage is None
        )
    return (
        source.design_state == "materialised"
        and source.materialised_at is not None
        and source.storage is not None
    )


def _valid_locator(info: object, locator: object, intent: LifecycleIntent) -> bool:
    return (
        type(info) is StorageAdapterInfo
        and "service_store" in info.supported_components
        and type(locator) is ServiceStoreLocator
        and locator.backend == info.backend
        and locator.namespace == f"svc_{intent.service_id.hex}"
    )


def _registry_outcome(outcome: object) -> LifecycleExecutionResult:
    if type(outcome) is CompletedLifecycleClaim:
        return LifecycleExecutionResult("completed", outcome.service)
    if type(outcome) is ConflictClaim:
        return _result(LifecycleResultCategory.CHANGED_FINGERPRINT)
    if type(outcome) is StaleRevision:
        return _result(LifecycleResultCategory.STALE_REVISION)
    if type(outcome) is UnsupportedTransition:
        return _result(LifecycleResultCategory.UNSUPPORTED_TRANSITION)
    if type(outcome) is OperationInProgress:
        return _result(LifecycleResultCategory.OPERATION_IN_PROGRESS)
    if type(outcome) is RecoveryRequiredLifecycleClaim:
        return _result(LifecycleResultCategory.RECOVERY_REQUIRED)
    return _result(LifecycleResultCategory.INTERNAL_FAILURE)


def _result(category: LifecycleResultCategory) -> LifecycleExecutionResult:
    return LifecycleExecutionResult(category)


def _before_commit_error(error: Exception) -> LifecycleExecutionResult:
    if isinstance(error, StorageBusy):
        return _result(LifecycleResultCategory.STORAGE_BUSY)
    if isinstance(error, StorageUnavailable):
        return _result(LifecycleResultCategory.STORAGE_UNAVAILABLE)
    return _result(LifecycleResultCategory.INTERNAL_FAILURE)


def _rollback_close(uow: RegistryUnitOfWork) -> None:
    with suppress(Exception):
        uow.rollback()
    _close_only(uow)


def _close_only(uow: RegistryUnitOfWork) -> None:
    with suppress(Exception):
        uow.close()
