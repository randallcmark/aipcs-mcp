"""Focused application conformance for the shared registry-claim authority."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fixtures import valid_manifest

from aipcs_mcp.application.models import MaterialisationStorage, Service
from aipcs_mcp.application.operational_lifecycle import ArchiveCommand, SuspendCommand
from aipcs_mcp.application.registry_authority import (
    CompletedRegistryClaim,
    ExportCommand,
    IdentityCollisionKind,
    ImportCommand,
    ImportIdentity,
    LocalRegistryEvent,
    PreparedRegistryClaim,
    PurgeAuthority,
    PurgeAuthorityKind,
    PurgeCommand,
    ReceiptKind,
    ReceiptVerification,
    RecoveryRequiredRegistryClaim,
    RegistryClaimConflict,
    RegistryIdentityCollision,
    RegistryOperationInProgress,
    RegistryUnsupportedTransition,
    StorageBackend,
    TombstonedRegistryClaim,
    TransferReceipt,
)
from aipcs_mcp.manifest_v2 import ManifestV2
from application.fakes import FakeRegistry

_AT = datetime(2026, 7, 23, tzinfo=UTC)
_SERVICE_ID = UUID("5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23")
_IMPORT_ID = UUID("6b5d0b6a-976c-4e32-b4d2-cc0a64e3ee24")
_RECEIPT_ID = UUID("7b5d0b6a-976c-4e32-b4d2-cc0a64e3ee25")
_ROOT = "a" * 64


def _service(
    service_id: UUID = _SERVICE_ID,
    *,
    status: str = "active",
    revision: int = 7,
    materialised: bool = False,
) -> Service:
    manifest = ManifestV2.model_validate(valid_manifest()) if materialised else None
    return Service(
        service_id=service_id,
        principal_id="principal-a",
        domain_name=f"notes_{service_id.hex[:8]}",
        domain_class="project",
        intent_description="durable notes",
        created_at=_AT,
        updated_at=_AT,
        last_activity_at=_AT,
        manifest=manifest,
        schema_version=1 if materialised else None,
        design_state="materialised" if materialised else "seeded",
        operational_status=status,  # type: ignore[arg-type]
        service_revision=revision,
        materialised_at=_AT if materialised else None,
        storage=(MaterialisationStorage("sqlite", f"svc_{service_id.hex}") if materialised else None),
    )


def _write(registry: FakeRegistry, operation):
    uow = registry.uow()
    try:
        result = operation(uow)
        uow.commit()
        return result
    finally:
        uow.close()


def _suspend(*, key: str = "suspend-1", revision: int = 7) -> SuspendCommand:
    return SuspendCommand("principal-a", "test", _SERVICE_ID, revision, key)


def test_same_global_key_replays_exact_evidence_and_changed_requests_conflict() -> None:
    registry = FakeRegistry()
    _write(registry, lambda uow: uow.add(_service()))
    first = _write(registry, lambda uow: uow.authority.resolve_or_prepare(_suspend()))
    replay = _write(registry, lambda uow: uow.authority.resolve_or_prepare(_suspend()))
    changed = _write(
        registry, lambda uow: uow.authority.resolve_or_prepare(_suspend(revision=8))
    )
    different_kind = _write(
        registry,
        lambda uow: uow.authority.resolve_or_prepare(
            ArchiveCommand("principal-a", "test", _SERVICE_ID, 7, "suspend-1")
        ),
    )

    assert isinstance(first, PreparedRegistryClaim)
    assert replay == first
    assert isinstance(changed, RegistryClaimConflict)
    assert isinstance(different_kind, RegistryClaimConflict)


def test_existing_mutation_namespace_remains_globally_reserved() -> None:
    registry = FakeRegistry()
    service = _service()
    _write(registry, lambda uow: uow.add(service))
    _write(
        registry,
        lambda uow: uow.complete_non_lifecycle("seed", "principal-a", "shared", "b" * 64, service),
    )

    result = _write(
        registry,
        lambda uow: uow.authority.resolve_or_prepare(_suspend(key="shared")),
    )

    assert isinstance(result, RegistryClaimConflict)


def test_operational_completion_is_atomic_and_uses_frozen_target_policy() -> None:
    registry = FakeRegistry()
    _write(registry, lambda uow: uow.add(_service()))
    prepared = _write(registry, lambda uow: uow.authority.resolve_or_prepare(_suspend()))
    assert isinstance(prepared, PreparedRegistryClaim)

    completed = _write(
        registry,
        lambda uow: uow.authority.finalize_operational(prepared.intent, _AT + timedelta(seconds=1)),
    )
    current = _write(registry, lambda uow: uow.get("principal-a", _SERVICE_ID))

    assert isinstance(completed, CompletedRegistryClaim)
    assert current is not None
    assert current.operational_status == "suspended"
    assert current.service_revision == 8
    assert registry.local_events[-1].action == "suspend"
    assert "principal-a" not in repr(completed)
    assert "principal-a" not in repr(registry.local_events[-1])


def test_prepared_export_and_operational_operations_share_the_service_blocker() -> None:
    registry = FakeRegistry()
    _write(registry, lambda uow: uow.add(_service()))
    export = _write(
        registry,
        lambda uow: uow.authority.resolve_or_prepare(
            ExportCommand("principal-a", "test", _SERVICE_ID, 7, "export-1")
        ),
    )
    blocked = _write(registry, lambda uow: uow.authority.resolve_or_prepare(_suspend()))

    assert isinstance(export, PreparedRegistryClaim)
    assert isinstance(blocked, RegistryOperationInProgress)
    assert blocked.intent == export.intent


@pytest.mark.parametrize(
    ("receipt_principal", "receipt_service_id", "verification"),
    [
        ("principal-b", _SERVICE_ID, ReceiptVerification.VERIFIED),
        ("principal-a", _IMPORT_ID, ReceiptVerification.VERIFIED),
        ("principal-a", _SERVICE_ID, ReceiptVerification.UNVERIFIED),
    ],
)
def test_export_completion_rejects_mismatched_or_unverified_receipts(
    receipt_principal: str, receipt_service_id: UUID, verification: ReceiptVerification
) -> None:
    registry = FakeRegistry()
    _write(registry, lambda uow: uow.add(_service()))
    prepared = _write(
        registry,
        lambda uow: uow.authority.resolve_or_prepare(
            ExportCommand("principal-a", "test", _SERVICE_ID, 7, "export-1")
        ),
    )
    assert isinstance(prepared, PreparedRegistryClaim)
    receipt = TransferReceipt(
        _RECEIPT_ID,
        ReceiptKind.EXPORT,
        receipt_principal,
        receipt_service_id,
        _ROOT,
        1,
        StorageBackend.SQLITE,
        7,
        "active",
        verification,
        _AT,
    )

    result = _write(
        registry, lambda uow: uow.authority.complete_export(prepared.intent, receipt)
    )

    assert isinstance(result, RecoveryRequiredRegistryClaim)


@pytest.mark.parametrize(
    ("materialised", "status", "expected_type"),
    [
        (False, "active", PreparedRegistryClaim),
        (False, "suspended", PreparedRegistryClaim),
        (False, "archived", PreparedRegistryClaim),
        (True, "active", RegistryUnsupportedTransition),
        (True, "suspended", PreparedRegistryClaim),
        (True, "archived", PreparedRegistryClaim),
    ],
)
def test_export_state_policy_is_exact(
    materialised: bool, status: str, expected_type: type[object]
) -> None:
    registry = FakeRegistry()
    _write(registry, lambda uow: uow.add(_service(status=status, materialised=materialised)))

    result = _write(
        registry,
        lambda uow: uow.authority.resolve_or_prepare(
            ExportCommand("principal-a", "test", _SERVICE_ID, 7, "export-1")
        ),
    )

    assert type(result) is expected_type


def test_purge_is_unsupported_before_archival_even_with_explicit_override() -> None:
    registry = FakeRegistry()
    _write(registry, lambda uow: uow.add(_service(status="suspended")))

    result = _write(
        registry,
        lambda uow: uow.authority.resolve_or_prepare(
            PurgeCommand(
                "principal-a",
                "test",
                _SERVICE_ID,
                7,
                "purge-1",
                PurgeAuthority(PurgeAuthorityKind.EXPLICIT_OVERRIDE),
            )
        ),
    )

    assert isinstance(result, RegistryUnsupportedTransition)


def test_prepared_import_reserves_identity_but_is_invisible_until_publish() -> None:
    registry = FakeRegistry()
    identity = ImportIdentity("principal-a", _IMPORT_ID, "imported", f"svc_{_IMPORT_ID.hex}")
    command = ImportCommand(
        "principal-a", "test", identity, StorageBackend.SQLITE, _ROOT, "import-1"
    )
    prepared = _write(registry, lambda uow: uow.authority.resolve_or_prepare(command))

    assert isinstance(prepared, PreparedRegistryClaim)
    assert _write(registry, lambda uow: uow.get("principal-a", _IMPORT_ID)) is None
    assert _write(registry, lambda uow: uow.find_domain("principal-a", "imported")) is None
    assert _write(registry, lambda uow: uow.count("principal-a")) == 0
    collision = _write(
        registry,
        lambda uow: uow.authority.resolve_or_prepare(
            ImportCommand(
                "principal-a", "test", identity, StorageBackend.SQLITE, _ROOT, "import-2"
            )
        ),
    )
    assert collision == RegistryIdentityCollision(IdentityCollisionKind.SERVICE_ID)
    changed_backend = _write(
        registry,
        lambda uow: uow.authority.resolve_or_prepare(
            ImportCommand(
                "principal-a",
                "test",
                identity,
                StorageBackend.POSTGRESQL,
                _ROOT,
                "import-1",
            )
        ),
    )
    assert isinstance(changed_backend, RegistryClaimConflict)

    service = _service(_IMPORT_ID)
    service = Service(
        service_id=service.service_id,
        principal_id=service.principal_id,
        domain_name="imported",
        domain_class=service.domain_class,
        intent_description=service.intent_description,
        created_at=service.created_at,
        updated_at=service.updated_at,
        last_activity_at=service.last_activity_at,
        service_revision=service.service_revision,
    )
    receipt = TransferReceipt(
        _RECEIPT_ID,
        ReceiptKind.IMPORT,
        "principal-a",
        _IMPORT_ID,
        _ROOT,
        1,
        StorageBackend.SQLITE,
        service.service_revision,
        service.operational_status,
        ReceiptVerification.VERIFIED,
        _AT,
    )
    completed = _write(
        registry, lambda uow: uow.authority.publish_import(prepared.intent, service, receipt)
    )

    assert isinstance(completed, CompletedRegistryClaim)
    assert _write(registry, lambda uow: uow.get("principal-a", _IMPORT_ID)) == service
    assert "imported" not in repr(completed)


def test_import_publication_requires_a_verified_receipt() -> None:
    registry = FakeRegistry()
    identity = ImportIdentity("principal-a", _IMPORT_ID, "imported", f"svc_{_IMPORT_ID.hex}")
    prepared = _write(
        registry,
        lambda uow: uow.authority.resolve_or_prepare(
            ImportCommand(
                "principal-a", "test", identity, StorageBackend.SQLITE, _ROOT, "import-1"
            )
        ),
    )
    assert isinstance(prepared, PreparedRegistryClaim)
    service = _service(_IMPORT_ID)
    service = Service(
        service.service_id,
        service.principal_id,
        "imported",
        service.domain_class,
        service.intent_description,
        service.created_at,
        service.updated_at,
        service.last_activity_at,
        service_revision=service.service_revision,
    )
    receipt = TransferReceipt(
        _RECEIPT_ID,
        ReceiptKind.IMPORT,
        "principal-a",
        _IMPORT_ID,
        _ROOT,
        1,
        StorageBackend.SQLITE,
        7,
        "active",
        ReceiptVerification.UNVERIFIED,
        _AT,
    )

    result = _write(
        registry, lambda uow: uow.authority.publish_import(prepared.intent, service, receipt)
    )

    assert isinstance(result, RecoveryRequiredRegistryClaim)


def test_recovery_required_replays_and_blocks_new_service_keys() -> None:
    registry = FakeRegistry()
    _write(registry, lambda uow: uow.add(_service()))
    prepared = _write(registry, lambda uow: uow.authority.resolve_or_prepare(_suspend()))
    assert isinstance(prepared, PreparedRegistryClaim)
    recovered = _write(
        registry,
        lambda uow: uow.authority.mark_recovery_required(prepared.intent, _AT),
    )
    replay = _write(registry, lambda uow: uow.authority.resolve_or_prepare(_suspend()))
    blocked = _write(
        registry,
        lambda uow: uow.authority.resolve_or_prepare(
            ExportCommand("principal-a", "test", _SERVICE_ID, 7, "export-1")
        ),
    )

    assert isinstance(recovered, RecoveryRequiredRegistryClaim)
    assert replay == recovered
    assert blocked == recovered


def test_purge_requires_a_verified_archived_revision_receipt_and_retains_tombstone() -> None:
    registry = FakeRegistry()
    archived = _service(status="archived")
    _write(registry, lambda uow: uow.add(archived))
    authority = PurgeAuthority(PurgeAuthorityKind.VERIFIED_RECEIPT, _RECEIPT_ID)
    command = PurgeCommand("principal-a", "test", _SERVICE_ID, 7, "purge-1", authority)
    rejected = _write(registry, lambda uow: uow.authority.resolve_or_prepare(command))
    assert isinstance(rejected, RegistryUnsupportedTransition)

    receipt = TransferReceipt(
        _RECEIPT_ID,
        ReceiptKind.EXPORT,
        "principal-a",
        _SERVICE_ID,
        _ROOT,
        1,
        StorageBackend.SQLITE,
        7,
        "archived",
        ReceiptVerification.VERIFIED,
        _AT,
    )
    registry.receipts[_RECEIPT_ID] = receipt
    prepared = _write(registry, lambda uow: uow.authority.resolve_or_prepare(command))
    assert isinstance(prepared, PreparedRegistryClaim)
    completed = _write(registry, lambda uow: uow.authority.finalize_purge(prepared.intent, _AT))

    assert isinstance(completed, CompletedRegistryClaim)
    assert _write(registry, lambda uow: uow.get("principal-a", _SERVICE_ID)) is None
    assert registry.receipts[_RECEIPT_ID] == receipt
    tombstone = registry.tombstones[_SERVICE_ID]
    assert tombstone.logical_namespace == f"svc_{_SERVICE_ID.hex}"
    assert not hasattr(tombstone, "domain_name")
    assert "notes" not in repr(tombstone)


def test_explicit_override_is_a_distinct_purge_authority_value() -> None:
    registry = FakeRegistry()
    _write(registry, lambda uow: uow.add(_service(status="archived")))
    command = PurgeCommand(
        "principal-a",
        "test",
        _SERVICE_ID,
        7,
        "purge-override",
        PurgeAuthority(PurgeAuthorityKind.EXPLICIT_OVERRIDE),
    )

    prepared = _write(registry, lambda uow: uow.authority.resolve_or_prepare(command))
    assert isinstance(prepared, PreparedRegistryClaim)
    completed = _write(registry, lambda uow: uow.authority.finalize_purge(prepared.intent, _AT))

    assert isinstance(completed, CompletedRegistryClaim)
    assert registry.tombstones[_SERVICE_ID].authority.kind is PurgeAuthorityKind.EXPLICIT_OVERRIDE


def test_purge_scrubs_old_claims_and_legacy_ledger_to_minimal_replay_evidence() -> None:
    registry = FakeRegistry()
    archived = _service(status="archived")
    _write(registry, lambda uow: uow.add(archived))
    legacy = _write(
        registry,
        lambda uow: uow.complete_non_lifecycle("seed", "principal-a", "old-seed", "b" * 64, archived),
    )
    assert legacy is None
    prior = _write(
        registry,
        lambda uow: uow.authority.resolve_or_prepare(
            ExportCommand("principal-a", "test", _SERVICE_ID, 7, "old-export")
        ),
    )
    assert isinstance(prior, PreparedRegistryClaim)
    receipt = TransferReceipt(
        _RECEIPT_ID,
        ReceiptKind.EXPORT,
        "principal-a",
        _SERVICE_ID,
        _ROOT,
        1,
        StorageBackend.SQLITE,
        7,
        "archived",
        ReceiptVerification.VERIFIED,
        _AT,
    )
    registry.receipts[_RECEIPT_ID] = receipt
    purge = _write(
        registry,
        lambda uow: uow.authority.resolve_or_prepare(
            PurgeCommand(
                "principal-a",
                "test",
                _SERVICE_ID,
                7,
                "purge-scrub",
                PurgeAuthority(PurgeAuthorityKind.VERIFIED_RECEIPT, _RECEIPT_ID),
            )
        ),
    )
    # An active export must be completed before the purge key can be admitted.
    assert isinstance(purge, RegistryOperationInProgress)
    completed_export = _write(registry, lambda uow: uow.authority.complete_export(prior.intent, receipt))
    assert isinstance(completed_export, CompletedRegistryClaim)
    purge = _write(
        registry,
        lambda uow: uow.authority.resolve_or_prepare(
            PurgeCommand(
                "principal-a",
                "test",
                _SERVICE_ID,
                7,
                "purge-scrub",
                PurgeAuthority(PurgeAuthorityKind.VERIFIED_RECEIPT, _RECEIPT_ID),
            )
        ),
    )
    assert isinstance(purge, PreparedRegistryClaim)
    _write(registry, lambda uow: uow.authority.finalize_purge(purge.intent, _AT))

    replay = _write(
        registry,
        lambda uow: uow.authority.resolve_or_prepare(
            ExportCommand("principal-a", "test", _SERVICE_ID, 7, "old-export")
        ),
    )
    changed = _write(
        registry,
        lambda uow: uow.authority.resolve_or_prepare(
            ExportCommand("principal-a", "test", _SERVICE_ID, 6, "old-export")
        ),
    )
    assert isinstance(replay, TombstonedRegistryClaim)
    assert isinstance(changed, RegistryClaimConflict)
    assert "old-seed" not in registry.ledger
    assert isinstance(registry.tombstoned_claims[("principal-a", "old-seed")], TombstonedRegistryClaim)


def test_uncommitted_import_reservation_is_not_durable() -> None:
    registry = FakeRegistry()
    identity = ImportIdentity("principal-a", _IMPORT_ID, "imported", f"svc_{_IMPORT_ID.hex}")
    uow = registry.uow()
    result = uow.authority.resolve_or_prepare(
        ImportCommand(
            "principal-a", "test", identity, StorageBackend.SQLITE, _ROOT, "import-1"
        )
    )
    uow.rollback()
    uow.close()

    assert isinstance(result, PreparedRegistryClaim)
    assert registry.import_reservations == {}


def test_local_audit_retention_is_newest_thousand_per_principal() -> None:
    registry = FakeRegistry()
    _write(registry, lambda uow: uow.add(_service()))
    prepared = _write(registry, lambda uow: uow.authority.resolve_or_prepare(_suspend()))
    assert isinstance(prepared, PreparedRegistryClaim)
    uow = registry.uow()
    uow.local_events = [
        LocalRegistryEvent("suspend", "completed", _SERVICE_ID, principal, "test", _AT)
        for principal in ("principal-a", "principal-b")
        for _ in range(1_000)
    ]
    uow._local_event(prepared.intent, "completed", _AT + timedelta(seconds=1))
    uow.commit()
    uow.close()

    assert sum(event.principal_id == "principal-a" for event in registry.local_events) == 1_000
    assert sum(event.principal_id == "principal-b" for event in registry.local_events) == 1_000


def test_legacy_completion_rejects_a_key_owned_by_an_authority_claim() -> None:
    registry = FakeRegistry()
    service = _service()
    _write(registry, lambda uow: uow.add(service))
    _write(registry, lambda uow: uow.authority.resolve_or_prepare(_suspend(key="shared")))
    uow = registry.uow()
    with pytest.raises(RuntimeError):
        uow.complete_non_lifecycle("seed", "principal-a", "shared", "b" * 64, service)
    uow.rollback()
    uow.close()


def test_import_backend_and_receipt_format_are_strictly_typed() -> None:
    identity = ImportIdentity("principal-a", _IMPORT_ID, "imported", f"svc_{_IMPORT_ID.hex}")
    with pytest.raises(ValueError):
        ImportCommand("principal-a", "test", identity, "sqlite", _ROOT, "import-1")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TransferReceipt(
            _RECEIPT_ID,
            ReceiptKind.IMPORT,
            "principal-a",
            _IMPORT_ID,
            _ROOT,
            True,
            StorageBackend.SQLITE,
            7,
            "active",
            ReceiptVerification.VERIFIED,
            _AT,
        )
