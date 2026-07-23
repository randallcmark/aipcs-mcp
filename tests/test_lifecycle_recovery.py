"""Pure V1-08A lifecycle fingerprint and recovery-contract conformance tests."""

from __future__ import annotations

import ast
import sqlite3
from itertools import product
from pathlib import Path
from uuid import UUID

import pytest
from fixtures import valid_manifest

from aipcs_mcp.lifecycle import (
    MAX_RECORD_VERSION,
    MAX_SERVICE_REVISION,
    DeferredResult,
    DomainObservation,
    EvolveCommand,
    EvolveRecoveryObservation,
    FoundationObservation,
    LifecycleAdmission,
    LifecycleAdmissionStatus,
    LifecycleClaim,
    LifecycleClaimStatus,
    LifecycleContractError,
    LifecyclePhase,
    LifecycleResultCategory,
    MaterialiseCommand,
    MaterialiseRecoveryObservation,
    RecoveryAction,
    canonical_lifecycle_json,
    lifecycle_fingerprint,
    lifecycle_result_retryable,
    plan_recovery,
    prepare_intent,
)
from aipcs_mcp.manifest_v2 import SERVER_MANAGED_FIELDS, ManifestV2

SERVICE_ID = UUID("5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23")


def _manifest(version: int = 1) -> ManifestV2:
    payload = valid_manifest()
    payload["schema_version"] = version
    payload["migration_history"] = [
        {
            "from_schema_version": prior,
            "to_schema_version": prior + 1,
            "operations": [f"advance schema to version {prior + 1}"],
        }
        for prior in range(1, version)
    ]
    return ManifestV2.model_validate(payload)


def _materialise() -> MaterialiseCommand:
    return MaterialiseCommand(
        principal_id="principal-a",
        created_via="mcp",
        service_id=SERVICE_ID,
        expected_service_revision=7,
        expected_schema_version=1,
        idempotency_key="materialise-1",
    )


def _evolve() -> EvolveCommand:
    return EvolveCommand(
        principal_id="principal-a",
        created_via="mcp",
        service_id=SERVICE_ID,
        expected_service_revision=8,
        expected_schema_version=1,
        idempotency_key="evolve-1",
        target_manifest=_manifest(2),
    )


def _expected_deferred(
    value: FoundationObservation | DomainObservation,
) -> tuple[RecoveryAction, DeferredResult | None, LifecycleResultCategory | None, bool] | None:
    mapping = {
        FoundationObservation.BUSY: (DeferredResult.STORAGE_BUSY, LifecycleResultCategory.STORAGE_BUSY),
        FoundationObservation.UNCERTAIN: (
            DeferredResult.OPERATION_UNCERTAIN,
            LifecycleResultCategory.OPERATION_UNCERTAIN,
        ),
        FoundationObservation.UNAVAILABLE: (
            DeferredResult.STORAGE_UNAVAILABLE,
            LifecycleResultCategory.STORAGE_UNAVAILABLE,
        ),
        DomainObservation.BUSY: (DeferredResult.STORAGE_BUSY, LifecycleResultCategory.STORAGE_BUSY),
        DomainObservation.UNCERTAIN: (
            DeferredResult.OPERATION_UNCERTAIN,
            LifecycleResultCategory.OPERATION_UNCERTAIN,
        ),
        DomainObservation.UNAVAILABLE: (
            DeferredResult.STORAGE_UNAVAILABLE,
            LifecycleResultCategory.STORAGE_UNAVAILABLE,
        ),
    }
    result = mapping.get(value)
    if result is None:
        return None
    deferred, category = result
    return RecoveryAction.DEFER_OBSERVATION, deferred, category, category is not LifecycleResultCategory.STORAGE_UNAVAILABLE


def _recovery_required_expected() -> tuple[
    RecoveryAction, DeferredResult | None, LifecycleResultCategory | None, bool
]:
    return RecoveryAction.RECOVERY_REQUIRED, None, LifecycleResultCategory.RECOVERY_REQUIRED, False


def _expected_materialise_action(
    phase: LifecyclePhase, foundation: FoundationObservation, target: DomainObservation
) -> tuple[RecoveryAction, DeferredResult | None, LifecycleResultCategory | None, bool]:
    if phase is LifecyclePhase.COMPLETED:
        return RecoveryAction.REPLAY_COMPLETE, None, None, False
    if phase is LifecyclePhase.RECOVERY_REQUIRED:
        return _recovery_required_expected()
    deferred = _expected_deferred(foundation)
    if deferred is not None:
        return deferred
    if foundation in {FoundationObservation.UNINITIALISED, FoundationObservation.DIRTY}:
        return RecoveryAction.PREPARE_FOUNDATION, None, None, False
    if foundation is FoundationObservation.INCOMPATIBLE:
        return _recovery_required_expected()
    deferred = _expected_deferred(target)
    if deferred is not None:
        return deferred
    if target is DomainObservation.UNMATERIALISED:
        return RecoveryAction.APPLY_INITIAL_SCHEMA, None, None, False
    if target is DomainObservation.READY:
        return RecoveryAction.FINALIZE_REGISTRY, None, None, False
    return _recovery_required_expected()


def _expected_evolve_action(
    phase: LifecyclePhase,
    foundation: FoundationObservation,
    target: DomainObservation,
    source: DomainObservation,
) -> tuple[RecoveryAction, DeferredResult | None, LifecycleResultCategory | None, bool]:
    if phase is LifecyclePhase.COMPLETED:
        return RecoveryAction.REPLAY_COMPLETE, None, None, False
    if phase is LifecyclePhase.RECOVERY_REQUIRED:
        return _recovery_required_expected()
    deferred = _expected_deferred(foundation)
    if deferred is not None:
        return deferred
    if foundation is FoundationObservation.DIRTY:
        return RecoveryAction.PREPARE_FOUNDATION, None, None, False
    if foundation is not FoundationObservation.READY:
        return _recovery_required_expected()
    deferred = _expected_deferred(target)
    if deferred is not None:
        return deferred
    if target is DomainObservation.READY:
        return RecoveryAction.FINALIZE_REGISTRY, None, None, False
    deferred = _expected_deferred(source)
    if deferred is not None:
        return deferred
    if target is DomainObservation.INCOMPATIBLE and source is DomainObservation.READY:
        return RecoveryAction.APPLY_TRANSITION, None, None, False
    return _recovery_required_expected()


def _intent_in_phase(command: MaterialiseCommand | EvolveCommand, phase: LifecyclePhase):
    intent = prepare_intent(command, _manifest(command.expected_schema_version + (1 if isinstance(command, EvolveCommand) else 0)))
    return intent if phase is LifecyclePhase.PREPARED else intent.with_phase(phase)


def test_lifecycle_module_is_structurally_pure_and_never_imports_storage() -> None:
    import aipcs_mcp.lifecycle as lifecycle

    tree = ast.parse(Path(lifecycle.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "aipcs_mcp.storage" not in imported
    assert not any(name.startswith("sqlite3") for name in imported)
    assert not any(name.startswith("pathlib") for name in imported)


def test_pure_contract_uses_no_storage_from_an_external_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def blocked_connect(*args: object, **kwargs: object) -> object:
        raise AssertionError("pure lifecycle contract must not open SQLite")

    monkeypatch.setattr(sqlite3, "connect", blocked_connect)
    monkeypatch.chdir(tmp_path)
    intent = prepare_intent(_materialise(), _manifest())
    result = plan_recovery(
        intent,
        MaterialiseRecoveryObservation(
            LifecyclePhase.PREPARED,
            FoundationObservation.READY,
            DomainObservation.READY,
        ),
    )
    assert Path.cwd() == tmp_path
    assert result.action is RecoveryAction.FINALIZE_REGISTRY


def test_fingerprint_has_exact_compact_ascii_shape_and_known_digest() -> None:
    command = _materialise()
    assert canonical_lifecycle_json(command) == (
        '{"created_via":"mcp","expected_schema_version":1,'
        '"expected_service_revision":7,"kind":"materialise",'
        '"principal":"principal-a","service_id":"5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23"}'
    )
    assert lifecycle_fingerprint(command) == "49f31d4f8ae992243644fc2aa7bcb8eadabeb03c74040dae4d1b7b96bd679839"


def test_evolve_fingerprint_has_exact_complete_target_json_and_known_digest() -> None:
    command = _evolve()
    assert canonical_lifecycle_json(command) == (
        '{"created_via":"mcp","expected_schema_version":1,"expected_service_revision":8,'
        '"kind":"evolve","principal":"principal-a",'
        '"service_id":"5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23","target_manifest":{'
        '"discovery_facets":[{"entity":"project","field":"owner_id"}],"entities":[{'
        '"attributes":[{"allowed_values":null,"description":null,"name":"id",'
        '"primary_key":true,"required":true,"retrieval_mode":null,"type":"uuid"},{'
        '"allowed_values":null,"description":null,"name":"owner_id","primary_key":false,'
        '"required":true,"retrieval_mode":null,"type":"string"},{"allowed_values":null,'
        '"description":null,"name":"created_at","primary_key":false,"required":true,'
        '"retrieval_mode":null,"type":"datetime"},{"allowed_values":null,"description":null,'
        '"name":"updated_at","primary_key":false,"required":true,"retrieval_mode":null,'
        '"type":"datetime"},{"allowed_values":null,"description":null,"name":"created_via",'
        '"primary_key":false,"required":true,"retrieval_mode":null,"type":"string"},{'
        '"allowed_values":null,"description":null,"name":"record_version","primary_key":false,'
        '"required":true,"retrieval_mode":null,"type":"integer"},{"allowed_values":null,'
        '"description":null,"name":"title","primary_key":false,"required":true,'
        '"retrieval_mode":null,"type":"string"}],"description":null,"name":"project"}],'
        '"indices":[{"entity":"project","fields":["owner_id"],"name":"project_owner_idx",'
        '"unique":false}],"manifest_version":2,"migration_history":[{"from_schema_version":1,'
        '"operations":["advance schema to version 2"],"to_schema_version":2}],'
        '"query_patterns":["Find projects by owner."],"relationships":[],'
        '"retrieval_guidance":"Use exact owner filters before listing.","schema_version":2}}'
    )
    assert lifecycle_fingerprint(command) == "b22745fa5b9825dba2cca535c649931cda60da46f160d468814269d7ad9bad56"


def test_evolve_fingerprint_deeply_detaches_canonical_target_manifest() -> None:
    target = _manifest(2)
    command = EvolveCommand(
        principal_id="principal-a",
        created_via="mcp",
        service_id=SERVICE_ID,
        expected_service_revision=8,
        expected_schema_version=1,
        idempotency_key="evolve-1",
        target_manifest=target,
    )
    original = lifecycle_fingerprint(command)
    object.__setattr__(target, "schema_version", 1)
    assert command.target_manifest.schema_version == 2
    assert lifecycle_fingerprint(command) == original
    exposed_snapshot = command.target_manifest
    object.__setattr__(exposed_snapshot, "schema_version", 1)
    assert command.target_manifest.schema_version == 2
    intent = prepare_intent(command, _manifest(2))
    exposed_intent_snapshot = intent.target_manifest
    object.__setattr__(exposed_intent_snapshot, "schema_version", 1)
    assert intent.target_manifest.schema_version == 2
    assert intent.fingerprint == lifecycle_fingerprint(command)
    assert "target_manifest" in canonical_lifecycle_json(command)
    assert lifecycle_fingerprint(command) != lifecycle_fingerprint(_materialise())


def test_lifecycle_kind_is_fingerprint_separated_and_intents_verify_it_exactly() -> None:
    evolve = _evolve()
    materialise = MaterialiseCommand(
        principal_id=evolve.principal_id,
        created_via=evolve.created_via,
        service_id=evolve.service_id,
        expected_service_revision=evolve.expected_service_revision,
        expected_schema_version=evolve.expected_schema_version,
        idempotency_key=evolve.idempotency_key,
    )
    assert lifecycle_fingerprint(materialise) != lifecycle_fingerprint(evolve)
    intent = prepare_intent(evolve, _manifest(2))
    with pytest.raises(LifecycleContractError):
        type(intent)(
            kind=intent.kind,
            phase=intent.phase,
            principal_id=intent.principal_id,
            created_via=intent.created_via,
            service_id=intent.service_id,
            expected_service_revision=intent.expected_service_revision,
            expected_schema_version=intent.expected_schema_version,
            idempotency_key=intent.idempotency_key,
            fingerprint="0" * 64,
            target_manifest=intent.target_manifest,
        )


def test_revision_bounds_manifest_adjacency_and_exact_uuid_are_enforced() -> None:
    with pytest.raises(LifecycleContractError):
        MaterialiseCommand(
            principal_id="principal-a",
            created_via="mcp",
            service_id=SERVICE_ID,
            expected_service_revision=MAX_SERVICE_REVISION + 1,
            expected_schema_version=1,
            idempotency_key="k",
        )
    with pytest.raises(LifecycleContractError):
        MaterialiseCommand(
            principal_id="principal-a",
            created_via="mcp",
            service_id=UUID(int=0),
            expected_service_revision=1,
            expected_schema_version=1,
            idempotency_key="k",
        )
    with pytest.raises(LifecycleContractError):
        EvolveCommand(
            principal_id="principal-a",
            created_via="mcp",
            service_id=SERVICE_ID,
            expected_service_revision=1,
            expected_schema_version=1,
            idempotency_key="k",
            target_manifest=_manifest(1),
        )
    with pytest.raises(LifecycleContractError):
        MaterialiseCommand(
            principal_id="principal-a",
            created_via="mcp",
            service_id=SERVICE_ID,
            expected_service_revision=True,  # type: ignore[arg-type]
            expected_schema_version=1,
            idempotency_key="k",
        )
    forged_manifest = _manifest(2)
    object.__setattr__(forged_manifest, "manifest_version", 1)
    with pytest.raises(LifecycleContractError):
        EvolveCommand(
            principal_id="principal-a",
            created_via="mcp",
            service_id=SERVICE_ID,
            expected_service_revision=1,
            expected_schema_version=1,
            idempotency_key="k",
            target_manifest=forged_manifest,
        )


def test_manifest_schema_service_record_and_adapter_revisions_remain_distinct() -> None:
    manifest = _manifest()
    command = _materialise()
    intent = prepare_intent(command, manifest)
    assert manifest.manifest_version == 2
    assert manifest.schema_version == 1
    assert command.expected_schema_version == manifest.schema_version
    assert command.expected_service_revision == 7
    assert MAX_RECORD_VERSION == MAX_SERVICE_REVISION
    assert SERVER_MANAGED_FIELDS["record_version"] == {
        "type": "integer",
        "required": True,
        "primary_key": False,
    }
    assert "record_version" not in intent.__dataclass_fields__
    assert "adapter_migration_revision" not in intent.__dataclass_fields__
    assert "adapter_revision" not in intent.__dataclass_fields__
    assert "migration_revision" not in intent.__dataclass_fields__


def test_claim_and_admission_vocabulary_is_phase_exact() -> None:
    prepared = prepare_intent(_materialise(), _manifest())
    completed = prepared.with_phase(LifecyclePhase.COMPLETED)
    recovery_required = prepared.with_phase(LifecyclePhase.RECOVERY_REQUIRED)
    assert LifecycleClaim(LifecycleClaimStatus.NEW).intent is None
    assert LifecycleClaim(LifecycleClaimStatus.REPLAY_COMPLETE, completed).intent is completed
    assert LifecycleClaim(LifecycleClaimStatus.RESUME_PREPARED, prepared).intent is prepared
    assert LifecycleClaim(LifecycleClaimStatus.RECOVERY_REQUIRED, recovery_required).intent is recovery_required
    assert LifecycleAdmission(LifecycleAdmissionStatus.PREPARED, prepared).intent is prepared
    assert (
        LifecycleAdmission(LifecycleAdmissionStatus.OPERATION_IN_PROGRESS, prepared).intent is prepared
    )
    assert (
        LifecycleAdmission(LifecycleAdmissionStatus.RECOVERY_REQUIRED, recovery_required).intent
        is recovery_required
    )
    with pytest.raises(LifecycleContractError):
        LifecycleClaim(LifecycleClaimStatus.REPLAY_COMPLETE, prepared)
    with pytest.raises(LifecycleContractError):
        LifecycleAdmission(LifecycleAdmissionStatus.OPERATION_IN_PROGRESS, recovery_required)


@pytest.mark.parametrize(
    ("category", "retryable"),
    [
        (LifecycleResultCategory.MALFORMED_INPUT, False),
        (LifecycleResultCategory.UNSUPPORTED_TRANSITION, False),
        (LifecycleResultCategory.STALE_REVISION, False),
        (LifecycleResultCategory.CHANGED_FINGERPRINT, False),
        (LifecycleResultCategory.OPERATION_IN_PROGRESS, True),
        (LifecycleResultCategory.RECOVERY_REQUIRED, False),
        (LifecycleResultCategory.STORAGE_BUSY, True),
        (LifecycleResultCategory.OPERATION_UNCERTAIN, True),
        (LifecycleResultCategory.STORAGE_UNAVAILABLE, False),
        (LifecycleResultCategory.INTERNAL_FAILURE, False),
    ],
)
def test_lifecycle_result_categories_have_frozen_retryability(
    category: LifecycleResultCategory, retryable: bool
) -> None:
    assert lifecycle_result_retryable(category) is retryable
    assert category.value in {
        "malformed_input",
        "unsupported_transition",
        "stale_revision",
        "changed_fingerprint",
        "operation_in_progress",
        "recovery_required",
        "storage_busy",
        "operation_uncertain",
        "storage_unavailable",
        "internal_failure",
    }
    with pytest.raises(LifecycleContractError):
        lifecycle_result_retryable("storage_busy")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("observation", "action", "deferred"),
    [
        (
            MaterialiseRecoveryObservation(
                LifecyclePhase.PREPARED,
                FoundationObservation.UNINITIALISED,
                DomainObservation.NOT_OBSERVED,
            ),
            RecoveryAction.PREPARE_FOUNDATION,
            None,
        ),
        (
            MaterialiseRecoveryObservation(
                LifecyclePhase.PREPARED,
                FoundationObservation.READY,
                DomainObservation.UNMATERIALISED,
            ),
            RecoveryAction.APPLY_INITIAL_SCHEMA,
            None,
        ),
        (
            MaterialiseRecoveryObservation(
                LifecyclePhase.PREPARED,
                FoundationObservation.READY,
                DomainObservation.READY,
            ),
            RecoveryAction.FINALIZE_REGISTRY,
            None,
        ),
        (
            MaterialiseRecoveryObservation(
                LifecyclePhase.PREPARED,
                FoundationObservation.DIRTY,
                DomainObservation.NOT_OBSERVED,
            ),
            RecoveryAction.PREPARE_FOUNDATION,
            None,
        ),
        (
            MaterialiseRecoveryObservation(
                LifecyclePhase.PREPARED,
                FoundationObservation.BUSY,
                DomainObservation.NOT_OBSERVED,
            ),
            RecoveryAction.DEFER_OBSERVATION,
            DeferredResult.STORAGE_BUSY,
        ),
    ],
)
def test_materialise_truth_table(
    observation: MaterialiseRecoveryObservation,
    action: RecoveryAction,
    deferred: DeferredResult | None,
) -> None:
    intent = prepare_intent(_materialise(), _manifest())
    result = plan_recovery(intent, observation)
    assert (result.action, result.deferred_result) == (action, deferred)
    assert result.retryable is (deferred is DeferredResult.STORAGE_BUSY)


@pytest.mark.parametrize(
    ("observation", "action", "deferred"),
    [
        (
            EvolveRecoveryObservation(
                LifecyclePhase.PREPARED,
                FoundationObservation.READY,
                DomainObservation.READY,
                DomainObservation.NOT_OBSERVED,
            ),
            RecoveryAction.FINALIZE_REGISTRY,
            None,
        ),
        (
            EvolveRecoveryObservation(
                LifecyclePhase.PREPARED,
                FoundationObservation.READY,
                DomainObservation.INCOMPATIBLE,
                DomainObservation.READY,
            ),
            RecoveryAction.APPLY_TRANSITION,
            None,
        ),
        (
            EvolveRecoveryObservation(
                LifecyclePhase.PREPARED,
                FoundationObservation.READY,
                DomainObservation.UNMATERIALISED,
                DomainObservation.UNMATERIALISED,
            ),
            RecoveryAction.RECOVERY_REQUIRED,
            None,
        ),
        (
            EvolveRecoveryObservation(
                LifecyclePhase.PREPARED,
                FoundationObservation.UNINITIALISED,
                DomainObservation.NOT_OBSERVED,
                DomainObservation.NOT_OBSERVED,
            ),
            RecoveryAction.RECOVERY_REQUIRED,
            None,
        ),
        (
            EvolveRecoveryObservation(
                LifecyclePhase.PREPARED,
                FoundationObservation.DIRTY,
                DomainObservation.NOT_OBSERVED,
                DomainObservation.NOT_OBSERVED,
            ),
            RecoveryAction.PREPARE_FOUNDATION,
            None,
        ),
        (
            EvolveRecoveryObservation(
                LifecyclePhase.PREPARED,
                FoundationObservation.READY,
                DomainObservation.UNCERTAIN,
                DomainObservation.NOT_OBSERVED,
            ),
            RecoveryAction.DEFER_OBSERVATION,
            DeferredResult.OPERATION_UNCERTAIN,
        ),
    ],
)
def test_evolve_truth_table_and_target_adoption(
    observation: EvolveRecoveryObservation,
    action: RecoveryAction,
    deferred: DeferredResult | None,
) -> None:
    intent = prepare_intent(_evolve(), _manifest(2))
    result = plan_recovery(intent, observation)
    assert (result.action, result.deferred_result) == (action, deferred)


def test_terminal_phases_do_not_reinspect_and_replay_or_report() -> None:
    initial = prepare_intent(_materialise(), _manifest())
    completed = initial.with_phase(LifecyclePhase.COMPLETED)
    recovery_required = initial.with_phase(LifecyclePhase.RECOVERY_REQUIRED)
    terminal = MaterialiseRecoveryObservation(
        LifecyclePhase.COMPLETED,
        FoundationObservation.NOT_OBSERVED,
        DomainObservation.NOT_OBSERVED,
    )
    assert plan_recovery(completed, terminal).action is RecoveryAction.REPLAY_COMPLETE
    terminal_recovery = MaterialiseRecoveryObservation(
        LifecyclePhase.RECOVERY_REQUIRED,
        FoundationObservation.NOT_OBSERVED,
        DomainObservation.NOT_OBSERVED,
    )
    assert plan_recovery(recovery_required, terminal_recovery).action is RecoveryAction.RECOVERY_REQUIRED


def test_intent_phase_advancement_is_monotonic_and_terminal_once() -> None:
    prepared = prepare_intent(_materialise(), _manifest())
    completed = prepared.with_phase(LifecyclePhase.COMPLETED)
    assert completed.phase is LifecyclePhase.COMPLETED
    assert prepared.with_phase(LifecyclePhase.RECOVERY_REQUIRED).phase is LifecyclePhase.RECOVERY_REQUIRED
    for source, target in (
        (prepared, LifecyclePhase.PREPARED),
        (completed, LifecyclePhase.PREPARED),
        (completed, LifecyclePhase.RECOVERY_REQUIRED),
        (completed, LifecyclePhase.COMPLETED),
    ):
        with pytest.raises(LifecycleContractError):
            source.with_phase(target)


def test_observation_order_and_impossible_combinations_fail_closed() -> None:
    with pytest.raises(LifecycleContractError):
        MaterialiseRecoveryObservation(
            LifecyclePhase.PREPARED,
            FoundationObservation.UNINITIALISED,
            DomainObservation.READY,
        )
    with pytest.raises(LifecycleContractError):
        EvolveRecoveryObservation(
            LifecyclePhase.PREPARED,
            FoundationObservation.READY,
            DomainObservation.UNMATERIALISED,
            DomainObservation.READY,
        )
    with pytest.raises(LifecycleContractError):
        EvolveRecoveryObservation(
            LifecyclePhase.PREPARED,
            FoundationObservation.READY,
            DomainObservation.INCOMPATIBLE,
            DomainObservation.UNMATERIALISED,
        )
    with pytest.raises(LifecycleContractError):
        EvolveRecoveryObservation(
            LifecyclePhase.COMPLETED,
            FoundationObservation.READY,
            DomainObservation.NOT_OBSERVED,
            DomainObservation.NOT_OBSERVED,
        )


def test_materialise_observation_matrix_is_closed_and_exhaustive() -> None:
    for phase, foundation, target in product(
        LifecyclePhase, FoundationObservation, DomainObservation
    ):
        valid = (
            phase is not LifecyclePhase.PREPARED
            and foundation is FoundationObservation.NOT_OBSERVED
            and target is DomainObservation.NOT_OBSERVED
        ) or (
            phase is LifecyclePhase.PREPARED
            and foundation is FoundationObservation.READY
            and target is not DomainObservation.NOT_OBSERVED
        ) or (
            phase is LifecyclePhase.PREPARED
            and foundation
            in {
                FoundationObservation.UNINITIALISED,
                FoundationObservation.DIRTY,
                FoundationObservation.INCOMPATIBLE,
                FoundationObservation.BUSY,
                FoundationObservation.UNAVAILABLE,
                FoundationObservation.UNCERTAIN,
            }
            and target is DomainObservation.NOT_OBSERVED
        )
        if valid:
            observation = MaterialiseRecoveryObservation(phase, foundation, target)
            result = plan_recovery(_intent_in_phase(_materialise(), phase), observation)
            assert (
                result.action,
                result.deferred_result,
                result.result_category,
                result.retryable,
            ) == _expected_materialise_action(phase, foundation, target)
        else:
            with pytest.raises(LifecycleContractError):
                MaterialiseRecoveryObservation(phase, foundation, target)


def test_evolve_observation_matrix_is_closed_and_exhaustive() -> None:
    for phase, foundation, target, source in product(
        LifecyclePhase, FoundationObservation, DomainObservation, DomainObservation
    ):
        terminal = (
            phase is not LifecyclePhase.PREPARED
            and foundation is FoundationObservation.NOT_OBSERVED
            and target is DomainObservation.NOT_OBSERVED
            and source is DomainObservation.NOT_OBSERVED
        )
        non_ready_foundation = (
            phase is LifecyclePhase.PREPARED
            and foundation is not FoundationObservation.READY
            and foundation is not FoundationObservation.NOT_OBSERVED
            and target is DomainObservation.NOT_OBSERVED
            and source is DomainObservation.NOT_OBSERVED
        )
        ready_target = (
            phase is LifecyclePhase.PREPARED
            and foundation is FoundationObservation.READY
            and target in {DomainObservation.READY, *set(DomainObservation) - {DomainObservation.NOT_OBSERVED}}
        )
        ready_target = ready_target and (
            (target in {DomainObservation.READY, DomainObservation.BUSY, DomainObservation.UNAVAILABLE, DomainObservation.UNCERTAIN}
             and source is DomainObservation.NOT_OBSERVED)
            or (
                target is DomainObservation.UNMATERIALISED
                and source
                in {
                    DomainObservation.UNMATERIALISED,
                    DomainObservation.BUSY,
                    DomainObservation.UNAVAILABLE,
                    DomainObservation.UNCERTAIN,
                }
            )
            or (
                target is DomainObservation.INCOMPATIBLE
                and source
                in {
                    DomainObservation.READY,
                    DomainObservation.INCOMPATIBLE,
                    DomainObservation.BUSY,
                    DomainObservation.UNAVAILABLE,
                    DomainObservation.UNCERTAIN,
                }
            )
        )
        if terminal or non_ready_foundation or ready_target:
            observation = EvolveRecoveryObservation(phase, foundation, target, source)
            result = plan_recovery(_intent_in_phase(_evolve(), phase), observation)
            assert (
                result.action,
                result.deferred_result,
                result.result_category,
                result.retryable,
            ) == _expected_evolve_action(phase, foundation, target, source)
        else:
            with pytest.raises(LifecycleContractError):
                EvolveRecoveryObservation(phase, foundation, target, source)


def test_intent_and_observation_must_match_kind_and_phase() -> None:
    intent = prepare_intent(_materialise(), _manifest())
    with pytest.raises(LifecycleContractError):
        plan_recovery(
            intent,
            EvolveRecoveryObservation(
                LifecyclePhase.PREPARED,
                FoundationObservation.READY,
                DomainObservation.READY,
                DomainObservation.NOT_OBSERVED,
            ),
        )
    with pytest.raises(LifecycleContractError):
        plan_recovery(
            intent,
            MaterialiseRecoveryObservation(
                LifecyclePhase.COMPLETED,
                FoundationObservation.NOT_OBSERVED,
                DomainObservation.NOT_OBSERVED,
            ),
        )
