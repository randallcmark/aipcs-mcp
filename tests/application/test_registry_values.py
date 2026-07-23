"""Pure application-value conformance for the R2 registry port."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fixtures import valid_manifest

from aipcs_mcp.application.models import (
    CompletedLifecycleClaim,
    CompletedNonLifecycleClaim,
    ConflictClaim,
    EvolveCompletion,
    MaterialisationStorage,
    MaterialiseCompletion,
    NewClaim,
    OperationInProgress,
    PreparedLifecycleClaim,
    RecoveryRequiredLifecycleClaim,
    Service,
    StaleRevision,
    UnsupportedTransition,
    project,
)
from aipcs_mcp.lifecycle import (
    EvolveCommand,
    LifecyclePhase,
    LifecycleResultCategory,
    MaterialiseCommand,
    RecoveryState,
    prepare_intent,
)
from aipcs_mcp.manifest_v2 import ManifestV2

_AT = datetime(2026, 7, 23, tzinfo=UTC)
_LATER = datetime(2026, 7, 24, tzinfo=UTC)
_SERVICE_ID = UUID("5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23")


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


def _service(*, revision: int = 1, materialised: bool = False, manifest: ManifestV2 | None = None) -> Service:
    schema = manifest.schema_version if manifest is not None else None
    storage = MaterialisationStorage("sqlite", f"svc_{_SERVICE_ID.hex}") if materialised else None
    return Service(
        service_id=_SERVICE_ID,
        principal_id="principal-a",
        domain_name="notes",
        domain_class="project",
        intent_description="Durable notes",
        created_at=_AT,
        updated_at=_AT,
        last_activity_at=_AT,
        manifest=manifest,
        schema_version=schema,
        design_state="materialised" if materialised else "seeded",
        service_revision=revision,
        materialised_at=_AT if materialised else None,
        storage=storage,
    )


def _materialise_intent():
    command = MaterialiseCommand(
        principal_id="principal-a",
        created_via="contract-test",
        service_id=_SERVICE_ID,
        expected_service_revision=1,
        expected_schema_version=1,
        idempotency_key="materialise-1",
    )
    return prepare_intent(command, _manifest())


def _evolve_intent():
    command = EvolveCommand(
        principal_id="principal-a",
        created_via="contract-test",
        service_id=_SERVICE_ID,
        expected_service_revision=2,
        expected_schema_version=1,
        idempotency_key="evolve-1",
        target_manifest=_manifest(2),
    )
    return prepare_intent(command, command.target_manifest)


def test_service_r2_metadata_is_safe_paired_and_projected_with_current_aggregate() -> None:
    target = _manifest()
    service = _service(revision=2, materialised=True, manifest=target)

    assert service.service_revision == 2
    assert service.storage == MaterialisationStorage("sqlite", f"svc_{_SERVICE_ID.hex}")
    public = project(service, RecoveryState.CLEAR)
    assert public.materialised_at == _AT
    assert public.storage is not None
    assert public.storage.backend == "sqlite"
    assert public.storage.namespace == f"svc_{_SERVICE_ID.hex}"
    assert public.service_revision == 2
    assert public.recovery_state == RecoveryState.CLEAR

    assert replace(
        service, storage=MaterialisationStorage("postgresql", f"svc_{_SERVICE_ID.hex}")
    ).storage is not None
    with pytest.raises(ValueError):
        replace(service, storage=MaterialisationStorage("sqlite", f"svc_{UUID(int=2).hex}"))
    with pytest.raises(ValueError):
        replace(service, design_state="seeded")
    with pytest.raises(ValueError):
        replace(service, service_revision=0)


@pytest.mark.parametrize(
    "backend,namespace",
    [
        ("mysql", f"svc_{_SERVICE_ID.hex}"),
        ("sqlite", "sqlite://private"),
        ("sqlite", f"svc_{'0' * 32}"),
    ],
)
def test_materialisation_storage_rejects_unsafe_values(backend: object, namespace: object) -> None:
    with pytest.raises(ValueError):
        MaterialisationStorage(backend, namespace)  # type: ignore[arg-type]


def test_non_lifecycle_claims_are_closed_and_completion_is_detached() -> None:
    original = _service(manifest=_manifest())
    completion = CompletedNonLifecycleClaim("seed", original)
    assert isinstance(NewClaim(), NewClaim)
    assert isinstance(ConflictClaim(), ConflictClaim)
    assert original.manifest is not None
    original.manifest.entities[0].description = "changed after completion"
    assert completion.service.manifest is not None
    assert completion.service.manifest.entities[0].description != "changed after completion"
    with pytest.raises(ValueError):
        CompletedNonLifecycleClaim("materialise", _service())  # type: ignore[arg-type]


def test_lifecycle_claims_and_completion_values_are_phase_and_kind_exact() -> None:
    materialise = _materialise_intent()
    evolve = _evolve_intent()
    storage = MaterialisationStorage("sqlite", f"svc_{_SERVICE_ID.hex}")

    assert PreparedLifecycleClaim(materialise).intent == materialise
    assert MaterialiseCompletion(materialise, _AT, storage).prepared_intent == materialise
    assert EvolveCompletion(evolve, _AT).prepared_intent == evolve
    assert isinstance(StaleRevision(), StaleRevision)
    assert isinstance(UnsupportedTransition(), UnsupportedTransition)
    assert isinstance(OperationInProgress(), OperationInProgress)

    with pytest.raises(ValueError):
        MaterialiseCompletion(evolve, _AT, storage)
    with pytest.raises(ValueError):
        EvolveCompletion(materialise, _AT)
    with pytest.raises(ValueError):
        MaterialiseCompletion(materialise, datetime(2026, 7, 23), storage)
    with pytest.raises(ValueError):
        MaterialiseCompletion(
            materialise,
            _AT,
            MaterialisationStorage("sqlite", f"svc_{UUID(int=2).hex}"),
        )
    with pytest.raises(ValueError):
        PreparedLifecycleClaim(materialise.with_phase(LifecyclePhase.COMPLETED))
    with pytest.raises(ValueError):
        UnsupportedTransition(LifecycleResultCategory.STALE_REVISION)


def test_lifecycle_terminal_variants_require_exact_terminal_snapshot_and_category() -> None:
    materialise = _materialise_intent()
    completed = materialise.with_phase(LifecyclePhase.COMPLETED)
    terminal = _service(revision=2, materialised=True, manifest=materialise.target_manifest)
    result = CompletedLifecycleClaim(completed, terminal)
    assert result.intent is completed
    assert result.service == terminal

    recovery = materialise.with_phase(LifecyclePhase.RECOVERY_REQUIRED)
    assert RecoveryRequiredLifecycleClaim(recovery).category is LifecycleResultCategory.RECOVERY_REQUIRED
    with pytest.raises(ValueError):
        RecoveryRequiredLifecycleClaim(recovery, LifecycleResultCategory.STALE_REVISION)
    with pytest.raises(ValueError):
        CompletedLifecycleClaim(completed, replace(terminal, service_revision=3))
    with pytest.raises(ValueError):
        CompletedLifecycleClaim(completed, replace(terminal, design_state="seeded"))
    with pytest.raises(ValueError):
        CompletedLifecycleClaim(completed, replace(terminal, last_activity_at=_LATER))
    with pytest.raises(ValueError):
        CompletedLifecycleClaim(
            completed,
            replace(terminal, updated_at=_LATER, last_activity_at=_LATER),
        )

    evolve = _evolve_intent()
    evolved_terminal = replace(
        _service(revision=3, materialised=True, manifest=evolve.target_manifest),
        updated_at=_LATER,
        last_activity_at=_LATER,
    )
    assert CompletedLifecycleClaim(evolve.with_phase(LifecyclePhase.COMPLETED), evolved_terminal).service == (
        evolved_terminal
    )
    with pytest.raises(ValueError):
        CompletedLifecycleClaim(
            evolve.with_phase(LifecyclePhase.COMPLETED),
            replace(evolved_terminal, last_activity_at=datetime(2026, 7, 25, tzinfo=UTC)),
        )
