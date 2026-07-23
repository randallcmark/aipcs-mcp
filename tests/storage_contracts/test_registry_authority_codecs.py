from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from aipcs_mcp.application.models import Service
from aipcs_mcp.application.operational_lifecycle import (
    OperationalLifecyclePhase,
    SuspendCommand,
    prepare_operational_intent,
)
from aipcs_mcp.application.registry_authority import (
    ExportCommand,
    ReceiptKind,
    ReceiptVerification,
    StorageBackend,
    TombstonedRegistryClaim,
    TransferReceipt,
    prepare_registry_intent,
)
from aipcs_mcp.storage.errors import StorageMigrationError
from aipcs_mcp.storage.registry_authority_codecs import (
    decode_registry_authority_intent,
    decode_registry_authority_result,
    decode_tombstoned_registry_claim,
    encode_registry_authority_intent,
    encode_registry_authority_result,
    encode_tombstoned_registry_claim,
    validate_import_identity_row,
    validate_registry_authority_claim_row,
    validate_transfer_receipt_row,
)

_AT = datetime(2026, 7, 23, tzinfo=UTC)
_SERVICE_ID = UUID("5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23")
_RECEIPT_ID = UUID("6b5d0b6a-976c-4e32-b4d2-cc0a64e3ee24")


def _service() -> Service:
    return Service(
        _SERVICE_ID, "principal-a", "notes", "project", "durable", _AT, _AT, _AT,
        operational_status="suspended", service_revision=8,
    )


def _receipt(kind: ReceiptKind = ReceiptKind.EXPORT) -> TransferReceipt:
    return TransferReceipt(
        _RECEIPT_ID, kind, "principal-a", _SERVICE_ID, "a" * 64, 1, StorageBackend.SQLITE,
        7, "suspended", ReceiptVerification.VERIFIED, _AT,
    )


def test_intent_and_service_result_round_trip_and_validate_exact_claim_pairing() -> None:
    intent = prepare_operational_intent(SuspendCommand("principal-a", "codec", _SERVICE_ID, 7, "suspend-1"))
    intent = intent.with_phase(OperationalLifecyclePhase.COMPLETED)
    intent_json = encode_registry_authority_intent(intent)
    result_json = encode_registry_authority_result(_service())
    row = {
        "principal_id": "principal-a", "idempotency_key": "suspend-1", "fingerprint": intent.fingerprint,
        "operation_kind": "suspend", "phase": "completed", "service_id": str(_SERVICE_ID),
        "created_via": "codec", "intent_json": intent_json, "result_json": result_json,
        "recovery_category": None,
    }
    assert decode_registry_authority_intent(intent_json) == intent
    assert decode_registry_authority_result(result_json, intent) == _service()
    assert validate_registry_authority_claim_row(row) == _service()
    mismatched_row = {**row, "operation_kind": "archive"}
    with pytest.raises(StorageMigrationError):
        validate_registry_authority_claim_row(mismatched_row)


def test_transfer_result_pairing_tombstoned_rows_and_canonical_rejection_are_exact() -> None:
    intent = prepare_registry_intent(ExportCommand("principal-a", "codec", _SERVICE_ID, 7, "export-1"))
    intent = intent.with_phase(OperationalLifecyclePhase.COMPLETED)
    receipt_json = encode_registry_authority_result(_receipt())
    tombstoned = TombstonedRegistryClaim("old-key", "b" * 64, "export", _SERVICE_ID)
    tombstoned_json = encode_tombstoned_registry_claim(tombstoned)
    row = {
        "principal_id": "principal-a", "idempotency_key": "old-key", "fingerprint": "b" * 64,
        "operation_kind": "export", "phase": "tombstoned", "service_id": str(_SERVICE_ID),
        "created_via": None, "intent_json": None, "result_json": None, "recovery_category": None,
    }
    assert decode_registry_authority_result(receipt_json, intent) == _receipt()
    assert decode_registry_authority_intent(encode_registry_authority_intent(intent)) == intent
    assert decode_tombstoned_registry_claim(tombstoned_json) == tombstoned
    assert validate_registry_authority_claim_row(row) == tombstoned
    for value in (json.dumps(json.loads(encode_registry_authority_intent(intent)), indent=2), receipt_json):
        with pytest.raises(StorageMigrationError) as error:
            decode_registry_authority_intent(value)
        assert error.value.__cause__ is None


def test_wrong_result_pairing_is_bounded_and_does_not_reflect_input() -> None:
    intent = prepare_registry_intent(ExportCommand("principal-a", "codec", _SERVICE_ID, 7, "export-1"))
    intent = intent.with_phase(OperationalLifecyclePhase.COMPLETED)
    with pytest.raises(StorageMigrationError) as error:
        decode_registry_authority_result(encode_registry_authority_result(_receipt(ReceiptKind.IMPORT)), intent)
    assert error.value.__cause__ is None
    assert "principal-a" not in str(error.value)


def test_flattened_receipt_and_import_identity_rows_require_exact_canonical_shapes() -> None:
    receipt = _receipt()
    receipt_row = {
        "receipt_id": str(receipt.receipt_id),
        "principal_id": receipt.principal_id,
        "idempotency_key": "export-1",
        "service_id": str(receipt.service_id),
        "receipt_kind": receipt.kind.value,
        "bundle_root_sha256": receipt.bundle_root_sha256,
        "export_format_version": receipt.export_format_version,
        "storage_backend": receipt.storage_backend.value,
        "service_revision": receipt.service_revision,
        "operational_status": receipt.operational_status,
        "verification": receipt.verification.value,
        "created_at": "2026-07-23T00:00:00.000000Z",
    }
    identity_row = {
        "service_id": str(_SERVICE_ID),
        "principal_id": "principal-a",
        "identity_state": "import_prepared",
        "domain_name": "notes",
        "storage_backend": "sqlite",
        "storage_namespace": f"svc_{_SERVICE_ID.hex}",
        "claim_idempotency_key": "import-1",
        "lifecycle_at": "2026-07-23T00:00:00.000000Z",
    }
    assert validate_transfer_receipt_row(receipt_row) == receipt
    assert validate_import_identity_row(identity_row).logical_namespace == f"svc_{_SERVICE_ID.hex}"
    for row in (
        {**receipt_row, "created_at": "2026-07-23T00:00:00Z"},
        {**receipt_row, "receipt_json": "{}"},
        {**identity_row, "identity_state": "live"},
        {**identity_row, "logical_namespace": identity_row["storage_namespace"]},
    ):
        with pytest.raises(StorageMigrationError):
            if "receipt_id" in row:
                validate_transfer_receipt_row(row)
            else:
                validate_import_identity_row(row)
