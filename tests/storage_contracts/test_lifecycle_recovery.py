"""SYNTHETIC_FIXTURE. Pure lifecycle admission/recovery conformance proof."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields
from typing import Literal, get_type_hints
from uuid import UUID

from fixtures import valid_manifest

from aipcs_mcp.lifecycle import (
    MAX_SCHEMA_VERSION,
    MAX_SERVICE_REVISION,
    EvolveCommand,
    EvolveRecoveryObservation,
    LifecycleAdmission,
    LifecycleAdmissionStatus,
    LifecycleClaim,
    LifecycleClaimStatus,
    LifecycleIntent,
    LifecycleKind,
    LifecyclePhase,
    LifecycleResultCategory,
    MaterialiseCommand,
    MaterialiseRecoveryObservation,
    RecoveryPlan,
    lifecycle_fingerprint,
    lifecycle_result_retryable,
    prepare_intent,
)
from aipcs_mcp.manifest_v2 import ManifestV2

_SERVICE_ID = UUID(int=1)


def _with_phase(intent: LifecycleIntent, phase: LifecyclePhase) -> LifecycleIntent:
    """Build a distinct immutable lifecycle value using its public contract."""

    return LifecycleIntent(
        kind=intent.kind,
        phase=phase,
        principal_id=intent.principal_id,
        created_via=intent.created_via,
        service_id=intent.service_id,
        expected_service_revision=intent.expected_service_revision,
        expected_schema_version=intent.expected_schema_version,
        idempotency_key=intent.idempotency_key,
        fingerprint=intent.fingerprint,
        target_manifest=intent.target_manifest,
    )


@dataclass(frozen=True)
class _Finalisation:
    """One test-only finalization result with no storage semantics."""

    status: Literal["completed", "replay", "recovery_required", "conflict"]
    intent: LifecycleIntent | None = None


class _LifecycleAdmissionFake:
    """Deterministic in-memory model of the frozen admission-order contract."""

    def __init__(self) -> None:
        self._intents: dict[tuple[str, str], LifecycleIntent] = {}
        self._service_revisions: dict[UUID, int] = {}
        self._schema_versions: dict[UUID, int] = {}
        self.finalization_wins = 0

    def seed_service(self, service_id: UUID, *, service_revision: int, schema_version: int) -> None:
        self._service_revisions[service_id] = service_revision
        self._schema_versions[service_id] = schema_version

    def _claim(self, command: MaterialiseCommand | EvolveCommand) -> LifecycleClaim:
        key = (command.principal_id, command.idempotency_key)
        fingerprint = lifecycle_fingerprint(command)
        existing = self._intents.get(key)
        if existing is None:
            return LifecycleClaim(LifecycleClaimStatus.NEW)
        if existing.fingerprint != fingerprint:
            return LifecycleClaim(LifecycleClaimStatus.CONFLICT)
        return LifecycleClaim(
            {
                LifecyclePhase.COMPLETED: LifecycleClaimStatus.REPLAY_COMPLETE,
                LifecyclePhase.PREPARED: LifecycleClaimStatus.RESUME_PREPARED,
                LifecyclePhase.RECOVERY_REQUIRED: LifecycleClaimStatus.RECOVERY_REQUIRED,
            }[existing.phase],
            existing,
        )

    def _admit_new(
        self, command: MaterialiseCommand | EvolveCommand, target: ManifestV2
    ) -> LifecycleAdmission | LifecycleResultCategory:
        if (
            self._service_revisions.get(command.service_id) != command.expected_service_revision
            or self._schema_versions.get(command.service_id) != command.expected_schema_version
        ):
            return LifecycleResultCategory.STALE_REVISION
        blocker = next(
            (
                intent
                for intent in self._intents.values()
                if intent.service_id == command.service_id
                and intent.phase in {LifecyclePhase.PREPARED, LifecyclePhase.RECOVERY_REQUIRED}
            ),
            None,
        )
        if blocker is not None:
            return LifecycleAdmission(
                (
                    LifecycleAdmissionStatus.OPERATION_IN_PROGRESS
                    if blocker.phase is LifecyclePhase.PREPARED
                    else LifecycleAdmissionStatus.RECOVERY_REQUIRED
                ),
                blocker,
            )

        intent = prepare_intent(command, target)
        self._intents[(command.principal_id, command.idempotency_key)] = intent
        return LifecycleAdmission(LifecycleAdmissionStatus.PREPARED, intent)

    def operate(
        self, command: MaterialiseCommand | EvolveCommand, target: ManifestV2
    ) -> LifecycleClaim | LifecycleAdmission | LifecycleResultCategory:
        """Resolve a key claim before any fresh-key revision or blocker read."""

        claim = self._claim(command)
        if claim.status is not LifecycleClaimStatus.NEW:
            return claim
        return self._admit_new(command, target)

    def finalise(self, intent: LifecycleIntent) -> _Finalisation:
        key = (intent.principal_id, intent.idempotency_key)
        current = self._intents.get(key)
        if current is None or current.fingerprint != intent.fingerprint:
            return _Finalisation("conflict")
        if current.phase is LifecyclePhase.COMPLETED:
            return _Finalisation("replay", current)
        if current.phase is LifecyclePhase.RECOVERY_REQUIRED:
            return _Finalisation("recovery_required", current)
        if (
            self._service_revisions.get(current.service_id)
            != current.expected_service_revision
            or self._schema_versions.get(current.service_id) != current.expected_schema_version
        ):
            recovered = _with_phase(current, LifecyclePhase.RECOVERY_REQUIRED)
            self._intents[key] = recovered
            return _Finalisation("recovery_required", recovered)

        completed = _with_phase(current, LifecyclePhase.COMPLETED)
        self._intents[key] = completed
        self._service_revisions[current.service_id] = current.expected_service_revision + 1
        self._schema_versions[current.service_id] = completed.target_manifest.schema_version
        self.finalization_wins += 1
        return _Finalisation("completed", completed)

    def force_recovery_required(self, intent: LifecycleIntent) -> LifecycleIntent:
        recovered = _with_phase(intent, LifecyclePhase.RECOVERY_REQUIRED)
        self._intents[(intent.principal_id, intent.idempotency_key)] = recovered
        return recovered

    def service_state(self, service_id: UUID) -> tuple[int, int]:
        return self._service_revisions[service_id], self._schema_versions[service_id]


def _v1() -> ManifestV2:
    return ManifestV2.model_validate(valid_manifest())


def _v2() -> ManifestV2:
    payload = deepcopy(valid_manifest())
    payload["schema_version"] = 2
    payload["migration_history"] = [
        {
            "from_schema_version": 1,
            "to_schema_version": 2,
            "operations": ["add optional project summary"],
        }
    ]
    payload["entities"][0]["attributes"].append({"name": "summary", "type": "string"})
    return ManifestV2.model_validate(payload)


def _materialise(
    *,
    service_id: UUID = _SERVICE_ID,
    key: str = "materialise-key",
    created_via: str = "contract-test",
    expected_service_revision: int = 1,
) -> MaterialiseCommand:
    return MaterialiseCommand(
        principal_id="contract-principal",
        created_via=created_via,
        service_id=service_id,
        expected_service_revision=expected_service_revision,
        expected_schema_version=1,
        idempotency_key=key,
    )


def _evolve(
    *,
    service_id: UUID = _SERVICE_ID,
    key: str = "evolve-key",
    expected_service_revision: int = 2,
) -> EvolveCommand:
    return EvolveCommand(
        principal_id="contract-principal",
        created_via="contract-test",
        service_id=service_id,
        expected_service_revision=expected_service_revision,
        expected_schema_version=1,
        idempotency_key=key,
        target_manifest=_v2(),
    )


def _ready_fake(service_id: UUID = _SERVICE_ID) -> _LifecycleAdmissionFake:
    fake = _LifecycleAdmissionFake()
    fake.seed_service(service_id, service_revision=1, schema_version=1)
    return fake


def test_completed_exact_replays_after_revision_increment_and_changed_key_conflicts() -> None:
    fake = _ready_fake()
    command = _materialise()
    prepared = fake.operate(command, _v1())
    assert isinstance(prepared, LifecycleAdmission)
    assert prepared.status is LifecycleAdmissionStatus.PREPARED
    assert prepared.intent is not None
    completed = fake.finalise(prepared.intent)
    assert completed.status == "completed"
    assert fake.service_state(command.service_id) == (2, 1)

    replay = fake.operate(command, _v1())
    assert isinstance(replay, LifecycleClaim)
    assert replay.status is LifecycleClaimStatus.REPLAY_COMPLETE
    assert replay.intent == completed.intent
    assert fake.service_state(command.service_id) == (2, 1)
    assert fake.finalization_wins == 1

    changed = fake.operate(_materialise(created_via="different-client"), _v1())
    assert isinstance(changed, LifecycleClaim)
    assert changed.status is LifecycleClaimStatus.CONFLICT
    assert fake.service_state(command.service_id) == (2, 1)
    retained = fake.operate(command, _v1())
    assert isinstance(retained, LifecycleClaim)
    assert retained.status is LifecycleClaimStatus.REPLAY_COMPLETE
    assert retained.intent == completed.intent


def test_prepared_same_key_resumes_and_different_key_is_excluded_per_service() -> None:
    fake = _ready_fake()
    command = _materialise()
    prepared = fake.operate(command, _v1())
    assert isinstance(prepared, LifecycleAdmission)
    assert prepared.status is LifecycleAdmissionStatus.PREPARED
    assert prepared.intent is not None

    resumed = fake.operate(command, _v1())
    assert isinstance(resumed, LifecycleClaim)
    assert resumed.status is LifecycleClaimStatus.RESUME_PREPARED
    assert resumed.intent == prepared.intent

    blocked_command = _materialise(key="other-key")
    blocked = fake.operate(blocked_command, _v1())
    assert isinstance(blocked, LifecycleAdmission)
    assert blocked.status is LifecycleAdmissionStatus.OPERATION_IN_PROGRESS
    assert blocked.intent == prepared.intent

    unrelated = _materialise(service_id=UUID(int=2), key="other-service")
    fake.seed_service(unrelated.service_id, service_revision=1, schema_version=1)
    unrelated_admission = fake.operate(unrelated, _v1())
    assert isinstance(unrelated_admission, LifecycleAdmission)
    assert unrelated_admission.status is LifecycleAdmissionStatus.PREPARED


def test_different_key_after_completion_uses_current_expected_versions() -> None:
    fake = _ready_fake()
    prepared = fake.operate(_materialise(), _v1())
    assert isinstance(prepared, LifecycleAdmission)
    assert prepared.intent is not None
    assert fake.finalise(prepared.intent).status == "completed"

    stale_command = _evolve(expected_service_revision=1)
    stale = fake.operate(stale_command, _v2())
    assert stale is LifecycleResultCategory.STALE_REVISION
    assert lifecycle_result_retryable(stale) is False
    accepted = fake.operate(_evolve(), _v2())
    assert isinstance(accepted, LifecycleAdmission)
    assert accepted.status is LifecycleAdmissionStatus.PREPARED
    assert accepted.intent is not None
    assert accepted.intent.kind is LifecycleKind.EVOLVE


def test_recovery_required_is_terminal_for_same_and_different_keys() -> None:
    fake = _ready_fake()
    command = _materialise()
    prepared = fake.operate(command, _v1())
    assert isinstance(prepared, LifecycleAdmission)
    assert prepared.intent is not None
    terminal = fake.force_recovery_required(prepared.intent)

    same_key = fake.operate(command, _v1())
    assert isinstance(same_key, LifecycleClaim)
    assert same_key.status is LifecycleClaimStatus.RECOVERY_REQUIRED
    assert same_key.intent == terminal
    changed_same_key = fake.operate(_materialise(created_via="different-client"), _v1())
    assert isinstance(changed_same_key, LifecycleClaim)
    assert changed_same_key.status is LifecycleClaimStatus.CONFLICT
    retained = fake.operate(command, _v1())
    assert isinstance(retained, LifecycleClaim)
    assert retained.status is LifecycleClaimStatus.RECOVERY_REQUIRED
    assert retained.intent == terminal
    different_command = _materialise(key="other-key")
    different_key = fake.operate(different_command, _v1())
    assert isinstance(different_key, LifecycleAdmission)
    assert different_key.status is LifecycleAdmissionStatus.RECOVERY_REQUIRED
    assert different_key.intent == terminal


def test_same_key_finalisation_has_one_cas_winner_and_terminal_reread() -> None:
    fake = _ready_fake()
    prepared = fake.operate(_materialise(), _v1())
    assert isinstance(prepared, LifecycleAdmission)
    assert prepared.intent is not None

    winner = fake.finalise(prepared.intent)
    reread = fake.finalise(prepared.intent)
    assert winner.status == "completed"
    assert reread.status == "replay"
    assert reread.intent == winner.intent
    assert fake.finalization_wins == 1
    assert fake.service_state(UUID(int=1)) == (2, 1)
    replay = fake.operate(_materialise(), _v1())
    assert isinstance(replay, LifecycleClaim)
    assert replay.status is LifecycleClaimStatus.REPLAY_COMPLETE


def test_stale_finalisation_marks_recovery_without_overwriting_current_service() -> None:
    fake = _ready_fake()
    command = _materialise()
    prepared = fake.operate(command, _v1())
    assert isinstance(prepared, LifecycleAdmission)
    assert prepared.intent is not None

    fake.seed_service(command.service_id, service_revision=2, schema_version=1)
    stale_finalisation = fake.finalise(prepared.intent)
    assert stale_finalisation.status == "recovery_required"
    assert stale_finalisation.intent is not None
    assert stale_finalisation.intent.phase is LifecyclePhase.RECOVERY_REQUIRED
    assert fake.service_state(command.service_id) == (2, 1)
    assert fake.finalization_wins == 0

    reread = fake.operate(command, _v1())
    assert isinstance(reread, LifecycleClaim)
    assert reread.status is LifecycleClaimStatus.RECOVERY_REQUIRED
    assert reread.intent == stale_finalisation.intent


def test_version_domains_and_lifecycle_values_do_not_leak_backend_or_persistence_types() -> None:
    manifest = _v1()
    command = _materialise()
    intent = prepare_intent(command, manifest)
    assert manifest.manifest_version == 2
    assert manifest.schema_version == command.expected_schema_version == 1
    assert MAX_SCHEMA_VERSION == 65
    assert MAX_SERVICE_REVISION > MAX_SCHEMA_VERSION
    assert intent.expected_service_revision == 1
    assert intent.target_manifest is not manifest

    forbidden = {"backend", "locator", "path", "dsn", "sql", "connection", "database"}
    for value_type in (
        MaterialiseCommand,
        EvolveCommand,
        LifecycleIntent,
        MaterialiseRecoveryObservation,
        EvolveRecoveryObservation,
        RecoveryPlan,
    ):
        assert forbidden.isdisjoint(field.name for field in fields(value_type))
        annotations = " ".join(map(str, get_type_hints(value_type).values())).lower()
        assert all(marker not in annotations for marker in forbidden)
