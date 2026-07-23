"""Pure lifecycle recovery vocabulary; deliberately independent of storage and transport."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from aipcs_mcp.manifest_v2 import ManifestV2

MAX_SERVICE_REVISION = (1 << 63) - 1
# This reserves the future per-record counter's independent meaning.  It shares
# the signed-64-bit representation with service revision but is never a service
# lifecycle compare-and-swap value.
MAX_RECORD_VERSION = (1 << 63) - 1
MAX_SCHEMA_VERSION = 65
MAX_PRINCIPAL_LENGTH = 128
MAX_CREATED_VIA_LENGTH = 64
MAX_IDEMPOTENCY_KEY_LENGTH = 128


class LifecycleContractError(ValueError):
    """A malformed pure lifecycle-contract value."""


class LifecycleKind(StrEnum):
    MATERIALISE = "materialise"
    EVOLVE = "evolve"


class LifecyclePhase(StrEnum):
    PREPARED = "prepared"
    COMPLETED = "completed"
    RECOVERY_REQUIRED = "recovery_required"


class RecoveryState(StrEnum):
    """Closed, service-scoped public aggregate derived only from registry evidence."""

    CLEAR = "clear"
    PENDING = "pending"
    RECOVERY_REQUIRED = "recovery_required"


class FoundationObservation(StrEnum):
    UNINITIALISED = "uninitialised"
    OUTDATED = "outdated"
    READY = "ready"
    DIRTY = "dirty"
    INCOMPATIBLE = "incompatible"
    NOT_OBSERVED = "not_observed"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"
    UNCERTAIN = "uncertain"


class DomainObservation(StrEnum):
    UNMATERIALISED = "unmaterialised"
    READY = "ready"
    INCOMPATIBLE = "incompatible"
    NOT_OBSERVED = "not_observed"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"
    UNCERTAIN = "uncertain"


class RecoveryAction(StrEnum):
    PREPARE_FOUNDATION = "prepare_foundation"
    APPLY_INITIAL_SCHEMA = "apply_initial_schema"
    APPLY_TRANSITION = "apply_transition"
    FINALIZE_REGISTRY = "finalize_registry"
    REPLAY_COMPLETE = "replay_complete"
    DEFER_OBSERVATION = "defer_observation"
    RECOVERY_REQUIRED = "recovery_required"


class DeferredResult(StrEnum):
    STORAGE_BUSY = "storage_busy"
    OPERATION_UNCERTAIN = "operation_uncertain"
    STORAGE_UNAVAILABLE = "storage_unavailable"


class LifecycleResultCategory(StrEnum):
    """Closed, transport-neutral lifecycle failure/result categories."""

    MALFORMED_INPUT = "malformed_input"
    UNSUPPORTED_TRANSITION = "unsupported_transition"
    STALE_REVISION = "stale_revision"
    CHANGED_FINGERPRINT = "changed_fingerprint"
    OPERATION_IN_PROGRESS = "operation_in_progress"
    RECOVERY_REQUIRED = "recovery_required"
    STORAGE_BUSY = "storage_busy"
    OPERATION_UNCERTAIN = "operation_uncertain"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    INTERNAL_FAILURE = "internal_failure"


class LifecycleClaimStatus(StrEnum):
    """Outcome of resolving the global principal/idempotency-key namespace."""

    NEW = "new"
    CONFLICT = "conflict"
    REPLAY_COMPLETE = "replay_complete"
    RESUME_PREPARED = "resume_prepared"
    RECOVERY_REQUIRED = "recovery_required"


class LifecycleAdmissionStatus(StrEnum):
    """Outcome of admitting a new key after service-state preconditions pass."""

    PREPARED = "prepared"
    OPERATION_IN_PROGRESS = "operation_in_progress"
    RECOVERY_REQUIRED = "recovery_required"


_FOUNDATION_NON_STATE = frozenset(
    {
        FoundationObservation.BUSY,
        FoundationObservation.UNAVAILABLE,
        FoundationObservation.UNCERTAIN,
    }
)
_FOUNDATION_EXACT_NON_READY = frozenset(
    {
        FoundationObservation.UNINITIALISED,
        FoundationObservation.OUTDATED,
        FoundationObservation.DIRTY,
        FoundationObservation.INCOMPATIBLE,
    }
)
_DOMAIN_NON_STATE = frozenset(
    {
        DomainObservation.BUSY,
        DomainObservation.UNAVAILABLE,
        DomainObservation.UNCERTAIN,
    }
)


@dataclass(frozen=True, slots=True)
class MaterialiseCommand:
    """Future public materialise request after transport validation."""

    principal_id: str
    created_via: str
    service_id: UUID
    expected_service_revision: int
    expected_schema_version: int
    idempotency_key: str
    kind: LifecycleKind = field(default=LifecycleKind.MATERIALISE, init=False)

    def __post_init__(self) -> None:
        _validate_common_command(self)
        if self.expected_schema_version != 1:
            raise LifecycleContractError("Materialise requires expected schema version 1.")


@dataclass(frozen=True, slots=True, init=False)
class EvolveCommand:
    """Future public evolve request with an independently revalidated target manifest."""

    principal_id: str
    created_via: str
    service_id: UUID
    expected_service_revision: int
    expected_schema_version: int
    idempotency_key: str
    _target_manifest_json: str = field(repr=False)
    kind: LifecycleKind = field(default=LifecycleKind.EVOLVE, init=False)

    def __init__(
        self,
        principal_id: str,
        created_via: str,
        service_id: UUID,
        expected_service_revision: int,
        expected_schema_version: int,
        idempotency_key: str,
        target_manifest: ManifestV2,
    ) -> None:
        _validate_common_fields(
            principal_id,
            created_via,
            service_id,
            expected_service_revision,
            expected_schema_version,
            idempotency_key,
        )
        if expected_schema_version >= MAX_SCHEMA_VERSION:
            raise LifecycleContractError("Evolve cannot exceed the supported schema-version bound.")
        snapshot = _manifest_snapshot(target_manifest)
        if snapshot.schema_version != expected_schema_version + 1:
            raise LifecycleContractError("Evolve target must advance schema version by exactly one.")
        object.__setattr__(self, "principal_id", principal_id)
        object.__setattr__(self, "created_via", created_via)
        object.__setattr__(self, "service_id", service_id)
        object.__setattr__(self, "expected_service_revision", expected_service_revision)
        object.__setattr__(self, "expected_schema_version", expected_schema_version)
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(self, "_target_manifest_json", _manifest_json(snapshot))
        object.__setattr__(self, "kind", LifecycleKind.EVOLVE)

    @property
    def target_manifest(self) -> ManifestV2:
        """Return a fresh detached target copy backed by immutable canonical JSON."""

        return _manifest_from_json(self._target_manifest_json)


type LifecycleCommand = MaterialiseCommand | EvolveCommand


@dataclass(frozen=True, slots=True, init=False)
class LifecycleIntent:
    """Immutable admitted-operation evidence; never a current-schema authority."""

    kind: LifecycleKind
    phase: LifecyclePhase
    principal_id: str
    created_via: str
    service_id: UUID
    expected_service_revision: int
    expected_schema_version: int
    idempotency_key: str
    fingerprint: str
    _target_manifest_json: str = field(repr=False)

    def __init__(
        self,
        kind: LifecycleKind,
        phase: LifecyclePhase,
        principal_id: str,
        created_via: str,
        service_id: UUID,
        expected_service_revision: int,
        expected_schema_version: int,
        idempotency_key: str,
        fingerprint: str,
        target_manifest: ManifestV2,
    ) -> None:
        _validate_kind(kind)
        _validate_phase(phase)
        _validate_common_fields(
            principal_id,
            created_via,
            service_id,
            expected_service_revision,
            expected_schema_version,
            idempotency_key,
        )
        _validate_fingerprint(fingerprint)
        snapshot = _manifest_snapshot(target_manifest)
        if kind is LifecycleKind.MATERIALISE:
            if expected_schema_version != 1 or snapshot.schema_version != 1:
                raise LifecycleContractError("Materialise intent requires an initial target manifest.")
        elif snapshot.schema_version != expected_schema_version + 1:
            raise LifecycleContractError("Evolve intent target must advance schema version by exactly one.")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "principal_id", principal_id)
        object.__setattr__(self, "created_via", created_via)
        object.__setattr__(self, "service_id", service_id)
        object.__setattr__(self, "expected_service_revision", expected_service_revision)
        object.__setattr__(self, "expected_schema_version", expected_schema_version)
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "_target_manifest_json", _manifest_json(snapshot))
        if fingerprint != lifecycle_fingerprint(_command_for_intent(self)):
            raise LifecycleContractError("Lifecycle fingerprint does not match the admitted request.")

    @property
    def target_manifest(self) -> ManifestV2:
        """Return a fresh detached target copy backed by immutable canonical JSON."""

        return _manifest_from_json(self._target_manifest_json)

    def with_phase(self, phase: LifecyclePhase) -> LifecycleIntent:
        """Finalize prepared evidence once without mutating it in place."""

        _validate_phase(phase)
        if self.phase is not LifecyclePhase.PREPARED or phase not in {
            LifecyclePhase.COMPLETED,
            LifecyclePhase.RECOVERY_REQUIRED,
        }:
            raise LifecycleContractError("Lifecycle intent phase transition is not monotonic.")

        return LifecycleIntent(
            kind=self.kind,
            phase=phase,
            principal_id=self.principal_id,
            created_via=self.created_via,
            service_id=self.service_id,
            expected_service_revision=self.expected_service_revision,
            expected_schema_version=self.expected_schema_version,
            idempotency_key=self.idempotency_key,
            fingerprint=self.fingerprint,
            target_manifest=self.target_manifest,
        )


@dataclass(frozen=True, slots=True)
class LifecycleClaim:
    """A typed same-key claim result before service-state admission."""

    status: LifecycleClaimStatus
    intent: LifecycleIntent | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not LifecycleClaimStatus:
            raise LifecycleContractError("Lifecycle claim status is invalid.")
        if self.status in {LifecycleClaimStatus.NEW, LifecycleClaimStatus.CONFLICT}:
            if self.intent is not None:
                raise LifecycleContractError("New or conflicting claim cannot carry an intent.")
            return
        if type(self.intent) is not LifecycleIntent:
            raise LifecycleContractError("Terminal or prepared claim requires an intent.")
        expected_phase = {
            LifecycleClaimStatus.REPLAY_COMPLETE: LifecyclePhase.COMPLETED,
            LifecycleClaimStatus.RESUME_PREPARED: LifecyclePhase.PREPARED,
            LifecycleClaimStatus.RECOVERY_REQUIRED: LifecyclePhase.RECOVERY_REQUIRED,
        }[self.status]
        if self.intent.phase is not expected_phase:
            raise LifecycleContractError("Lifecycle claim status does not match intent phase.")


@dataclass(frozen=True, slots=True)
class LifecycleAdmission:
    """A typed per-service admission outcome after exact revision checks."""

    status: LifecycleAdmissionStatus
    intent: LifecycleIntent

    def __post_init__(self) -> None:
        if type(self.status) is not LifecycleAdmissionStatus or type(self.intent) is not LifecycleIntent:
            raise LifecycleContractError("Lifecycle admission is invalid.")
        expected_phase = {
            LifecycleAdmissionStatus.PREPARED: LifecyclePhase.PREPARED,
            LifecycleAdmissionStatus.OPERATION_IN_PROGRESS: LifecyclePhase.PREPARED,
            LifecycleAdmissionStatus.RECOVERY_REQUIRED: LifecyclePhase.RECOVERY_REQUIRED,
        }[self.status]
        if self.intent.phase is not expected_phase:
            raise LifecycleContractError("Lifecycle admission status does not match intent phase.")


def prepare_intent(
    command: LifecycleCommand,
    target_manifest: ManifestV2,
) -> LifecycleIntent:
    """Bind a validated request to the registry-authoritative target snapshot."""

    if type(command) not in {MaterialiseCommand, EvolveCommand}:
        raise LifecycleContractError("Lifecycle command type is invalid.")
    target = _manifest_snapshot(target_manifest)
    if type(command) is MaterialiseCommand:
        if target.schema_version != command.expected_schema_version:
            raise LifecycleContractError("Materialise target must match the expected schema version.")
    elif target != command.target_manifest:
        raise LifecycleContractError("Evolve intent target must match the admitted request target.")
    return LifecycleIntent(
        kind=command.kind,
        phase=LifecyclePhase.PREPARED,
        principal_id=command.principal_id,
        created_via=command.created_via,
        service_id=command.service_id,
        expected_service_revision=command.expected_service_revision,
        expected_schema_version=command.expected_schema_version,
        idempotency_key=command.idempotency_key,
        fingerprint=lifecycle_fingerprint(command),
        target_manifest=target,
    )


def canonical_lifecycle_payload(command: LifecycleCommand) -> dict[str, object]:
    """Return the exact logical payload covered by the durable request fingerprint."""

    if type(command) not in {MaterialiseCommand, EvolveCommand}:
        raise LifecycleContractError("Lifecycle command type is invalid.")
    payload: dict[str, object] = {
        "kind": command.kind.value,
        "principal": command.principal_id,
        "created_via": command.created_via,
        "service_id": str(command.service_id),
        "expected_service_revision": command.expected_service_revision,
        "expected_schema_version": command.expected_schema_version,
    }
    if type(command) is EvolveCommand:
        payload["target_manifest"] = command.target_manifest.model_dump(
            mode="json", by_alias=True, warnings="error"
        )
    return payload


def canonical_lifecycle_json(command: LifecycleCommand) -> str:
    """Return the frozen compact ASCII JSON form used before SHA-256 hashing."""

    return json.dumps(
        canonical_lifecycle_payload(command), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def lifecycle_fingerprint(command: LifecycleCommand) -> str:
    """Compute the canonical lowercase SHA-256 request fingerprint."""

    return hashlib.sha256(canonical_lifecycle_json(command).encode("utf-8")).hexdigest()


def _command_for_intent(intent: LifecycleIntent) -> LifecycleCommand:
    fields = {
        "principal_id": intent.principal_id,
        "created_via": intent.created_via,
        "service_id": intent.service_id,
        "expected_service_revision": intent.expected_service_revision,
        "expected_schema_version": intent.expected_schema_version,
        "idempotency_key": intent.idempotency_key,
    }
    if intent.kind is LifecycleKind.MATERIALISE:
        return MaterialiseCommand(**fields)
    return EvolveCommand(target_manifest=intent.target_manifest, **fields)


@dataclass(frozen=True, slots=True)
class MaterialiseRecoveryObservation:
    """One fully ordered materialise observation without adapter details."""

    phase: LifecyclePhase
    foundation: FoundationObservation
    target: DomainObservation

    def __post_init__(self) -> None:
        _validate_phase(self.phase)
        _validate_foundation(self.foundation)
        _validate_domain(self.target)
        if self.phase is not LifecyclePhase.PREPARED:
            if (
                self.foundation is not FoundationObservation.NOT_OBSERVED
                or self.target is not DomainObservation.NOT_OBSERVED
            ):
                raise LifecycleContractError("Terminal materialise observations must not inspect storage.")
            return
        if self.foundation is FoundationObservation.READY:
            if self.target is DomainObservation.NOT_OBSERVED:
                raise LifecycleContractError("Ready materialise foundation requires a target observation.")
            return
        if self.foundation in _FOUNDATION_EXACT_NON_READY | _FOUNDATION_NON_STATE:
            if self.target is not DomainObservation.NOT_OBSERVED:
                raise LifecycleContractError("Non-ready materialise foundation cannot include target state.")
            return
        raise LifecycleContractError("Prepared materialise foundation observation is invalid.")


@dataclass(frozen=True, slots=True)
class EvolveRecoveryObservation:
    """One fully ordered evolve observation, always inspecting target before source."""

    phase: LifecyclePhase
    foundation: FoundationObservation
    target: DomainObservation
    source: DomainObservation

    def __post_init__(self) -> None:
        _validate_phase(self.phase)
        _validate_foundation(self.foundation)
        _validate_domain(self.target)
        _validate_domain(self.source)
        if self.phase is not LifecyclePhase.PREPARED:
            if (
                self.foundation is not FoundationObservation.NOT_OBSERVED
                or self.target is not DomainObservation.NOT_OBSERVED
                or self.source is not DomainObservation.NOT_OBSERVED
            ):
                raise LifecycleContractError("Terminal evolve observations must not inspect storage.")
            return
        if self.foundation in _FOUNDATION_EXACT_NON_READY | _FOUNDATION_NON_STATE:
            if (
                self.target is not DomainObservation.NOT_OBSERVED
                or self.source is not DomainObservation.NOT_OBSERVED
            ):
                raise LifecycleContractError("Non-ready evolve foundation cannot include schema state.")
            return
        if self.foundation is not FoundationObservation.READY:
            raise LifecycleContractError("Prepared evolve foundation observation is invalid.")
        _validate_ready_evolve_observation(self.target, self.source)


type LifecycleObservation = MaterialiseRecoveryObservation | EvolveRecoveryObservation


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """One deterministic action from a validated observation."""

    action: RecoveryAction
    deferred_result: DeferredResult | None = None

    def __post_init__(self) -> None:
        if type(self.action) is not RecoveryAction:
            raise LifecycleContractError("Recovery action is invalid.")
        if self.action is RecoveryAction.DEFER_OBSERVATION:
            if type(self.deferred_result) is not DeferredResult:
                raise LifecycleContractError("Deferred recovery action requires a typed result.")
        elif self.deferred_result is not None:
            raise LifecycleContractError("Only deferred recovery actions carry a deferred result.")

    @property
    def retryable(self) -> bool:
        category = self.result_category
        return category is not None and lifecycle_result_retryable(category)

    @property
    def result_category(self) -> LifecycleResultCategory | None:
        if self.action is RecoveryAction.RECOVERY_REQUIRED:
            return LifecycleResultCategory.RECOVERY_REQUIRED
        return {
            DeferredResult.STORAGE_BUSY: LifecycleResultCategory.STORAGE_BUSY,
            DeferredResult.OPERATION_UNCERTAIN: LifecycleResultCategory.OPERATION_UNCERTAIN,
            DeferredResult.STORAGE_UNAVAILABLE: LifecycleResultCategory.STORAGE_UNAVAILABLE,
        }.get(self.deferred_result)


def lifecycle_result_retryable(category: LifecycleResultCategory) -> bool:
    """Return the frozen retryability meaning for one exact result category."""

    if type(category) is not LifecycleResultCategory:
        raise LifecycleContractError("Lifecycle result category is invalid.")
    return category in {
        LifecycleResultCategory.STORAGE_BUSY,
        LifecycleResultCategory.OPERATION_IN_PROGRESS,
        LifecycleResultCategory.OPERATION_UNCERTAIN,
    }


def plan_recovery(intent: LifecycleIntent, observation: LifecycleObservation) -> RecoveryPlan:
    """Classify one exact lifecycle observation without any I/O or repair behavior."""

    if type(intent) is not LifecycleIntent:
        raise LifecycleContractError("Lifecycle intent type is invalid.")
    if intent.kind is LifecycleKind.MATERIALISE:
        if type(observation) is not MaterialiseRecoveryObservation:
            raise LifecycleContractError("Materialise intent requires a materialise observation.")
    elif type(observation) is not EvolveRecoveryObservation:
        raise LifecycleContractError("Evolve intent requires an evolve observation.")
    if observation.phase is not intent.phase:
        raise LifecycleContractError("Intent and observation phases must match exactly.")
    if intent.phase is LifecyclePhase.COMPLETED:
        return RecoveryPlan(RecoveryAction.REPLAY_COMPLETE)
    if intent.phase is LifecyclePhase.RECOVERY_REQUIRED:
        return RecoveryPlan(RecoveryAction.RECOVERY_REQUIRED)
    deferred = _deferred_foundation(observation.foundation)
    if deferred is not None:
        return deferred
    if type(observation) is MaterialiseRecoveryObservation:
        return _plan_materialise(observation)
    return _plan_evolve(observation)


def _plan_materialise(observation: MaterialiseRecoveryObservation) -> RecoveryPlan:
    if observation.foundation in {
        FoundationObservation.UNINITIALISED,
        FoundationObservation.OUTDATED,
        FoundationObservation.DIRTY,
    }:
        return RecoveryPlan(RecoveryAction.PREPARE_FOUNDATION)
    if observation.foundation is FoundationObservation.INCOMPATIBLE:
        return RecoveryPlan(RecoveryAction.RECOVERY_REQUIRED)
    deferred = _deferred_domain(observation.target)
    if deferred is not None:
        return deferred
    if observation.target is DomainObservation.UNMATERIALISED:
        return RecoveryPlan(RecoveryAction.APPLY_INITIAL_SCHEMA)
    if observation.target is DomainObservation.READY:
        return RecoveryPlan(RecoveryAction.FINALIZE_REGISTRY)
    return RecoveryPlan(RecoveryAction.RECOVERY_REQUIRED)


def _plan_evolve(observation: EvolveRecoveryObservation) -> RecoveryPlan:
    if observation.foundation in {FoundationObservation.OUTDATED, FoundationObservation.DIRTY}:
        return RecoveryPlan(RecoveryAction.PREPARE_FOUNDATION)
    if observation.foundation is not FoundationObservation.READY:
        return RecoveryPlan(RecoveryAction.RECOVERY_REQUIRED)
    deferred = _deferred_domain(observation.target)
    if deferred is not None:
        return deferred
    if observation.target is DomainObservation.READY:
        return RecoveryPlan(RecoveryAction.FINALIZE_REGISTRY)
    if observation.target is DomainObservation.UNMATERIALISED:
        deferred = _deferred_domain(observation.source)
        if deferred is not None:
            return deferred
        return RecoveryPlan(RecoveryAction.RECOVERY_REQUIRED)
    if observation.target is DomainObservation.INCOMPATIBLE:
        deferred = _deferred_domain(observation.source)
        if deferred is not None:
            return deferred
        if observation.source is DomainObservation.READY:
            return RecoveryPlan(RecoveryAction.APPLY_TRANSITION)
    return RecoveryPlan(RecoveryAction.RECOVERY_REQUIRED)


def _deferred_foundation(value: FoundationObservation) -> RecoveryPlan | None:
    if value is FoundationObservation.BUSY:
        return RecoveryPlan(RecoveryAction.DEFER_OBSERVATION, DeferredResult.STORAGE_BUSY)
    if value is FoundationObservation.UNCERTAIN:
        return RecoveryPlan(RecoveryAction.DEFER_OBSERVATION, DeferredResult.OPERATION_UNCERTAIN)
    if value is FoundationObservation.UNAVAILABLE:
        return RecoveryPlan(RecoveryAction.DEFER_OBSERVATION, DeferredResult.STORAGE_UNAVAILABLE)
    return None


def _deferred_domain(value: DomainObservation) -> RecoveryPlan | None:
    if value is DomainObservation.BUSY:
        return RecoveryPlan(RecoveryAction.DEFER_OBSERVATION, DeferredResult.STORAGE_BUSY)
    if value is DomainObservation.UNCERTAIN:
        return RecoveryPlan(RecoveryAction.DEFER_OBSERVATION, DeferredResult.OPERATION_UNCERTAIN)
    if value is DomainObservation.UNAVAILABLE:
        return RecoveryPlan(RecoveryAction.DEFER_OBSERVATION, DeferredResult.STORAGE_UNAVAILABLE)
    return None


def _validate_ready_evolve_observation(target: DomainObservation, source: DomainObservation) -> None:
    if target is DomainObservation.READY:
        if source is not DomainObservation.NOT_OBSERVED:
            raise LifecycleContractError("Ready target must not observe evolve source.")
        return
    if target in _DOMAIN_NON_STATE:
        if source is not DomainObservation.NOT_OBSERVED:
            raise LifecycleContractError("Transient target must not observe evolve source.")
        return
    if target is DomainObservation.UNMATERIALISED:
        if source not in {DomainObservation.UNMATERIALISED, *_DOMAIN_NON_STATE}:
            raise LifecycleContractError("Unmaterialised target has an invalid evolve source state.")
        return
    if target is DomainObservation.INCOMPATIBLE:
        if source not in {DomainObservation.READY, DomainObservation.INCOMPATIBLE, *_DOMAIN_NON_STATE}:
            raise LifecycleContractError("Incompatible target has an invalid evolve source state.")
        return
    raise LifecycleContractError("Ready evolve target observation is invalid.")


def _validate_common_command(command: MaterialiseCommand | EvolveCommand) -> None:
    _validate_common_fields(
        command.principal_id,
        command.created_via,
        command.service_id,
        command.expected_service_revision,
        command.expected_schema_version,
        command.idempotency_key,
    )


def _validate_common_fields(
    principal_id: str,
    created_via: str,
    service_id: UUID,
    expected_service_revision: int,
    expected_schema_version: int,
    idempotency_key: str,
) -> None:
    if not _valid_text(principal_id, MAX_PRINCIPAL_LENGTH):
        raise LifecycleContractError("Principal id is invalid.")
    if not _valid_text(created_via, MAX_CREATED_VIA_LENGTH):
        raise LifecycleContractError("Created-via value is invalid.")
    if type(service_id) is not UUID or service_id.int == 0:
        raise LifecycleContractError("Service id is invalid.")
    if type(expected_service_revision) is not int or not 1 <= expected_service_revision <= MAX_SERVICE_REVISION:
        raise LifecycleContractError("Expected service revision is invalid.")
    if type(expected_schema_version) is not int or not 1 <= expected_schema_version <= MAX_SCHEMA_VERSION:
        raise LifecycleContractError("Expected schema version is invalid.")
    if not _valid_text(idempotency_key, MAX_IDEMPOTENCY_KEY_LENGTH):
        raise LifecycleContractError("Idempotency key is invalid.")


def _manifest_snapshot(manifest: ManifestV2) -> ManifestV2:
    if type(manifest) is not ManifestV2:
        raise LifecycleContractError("Target manifest is invalid.")
    try:
        payload = manifest.model_dump(mode="json", by_alias=True, warnings="error")
        return ManifestV2.model_validate(payload)
    except Exception as error:
        raise LifecycleContractError("Target manifest is invalid.") from error


def _manifest_json(manifest: ManifestV2) -> str:
    return json.dumps(
        manifest.model_dump(mode="json", by_alias=True, warnings="error"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _manifest_from_json(value: str) -> ManifestV2:
    try:
        return ManifestV2.model_validate_json(value)
    except Exception as error:  # pragma: no cover - validated before storage in this pure value.
        raise LifecycleContractError("Stored target manifest snapshot is invalid.") from error


def _validate_fingerprint(value: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise LifecycleContractError("Lifecycle fingerprint is invalid.")


def _validate_kind(value: LifecycleKind) -> None:
    if type(value) is not LifecycleKind:
        raise LifecycleContractError("Lifecycle kind is invalid.")


def _validate_phase(value: LifecyclePhase) -> None:
    if type(value) is not LifecyclePhase:
        raise LifecycleContractError("Lifecycle phase is invalid.")


def _validate_foundation(value: FoundationObservation) -> None:
    if type(value) is not FoundationObservation:
        raise LifecycleContractError("Foundation observation is invalid.")


def _validate_domain(value: DomainObservation) -> None:
    if type(value) is not DomainObservation:
        raise LifecycleContractError("Domain observation is invalid.")


def _valid_text(value: str, maximum: int) -> bool:
    return type(value) is str and bool(value) and value == value.strip() and len(value) <= maximum
