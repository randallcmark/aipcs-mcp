"""Backend-neutral canonical codecs for shared registry-authority evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from aipcs_mcp.application.models import Service
from aipcs_mcp.application.operational_lifecycle import (
    OperationalLifecycleIntent,
    OperationalLifecycleKind,
    OperationalLifecyclePhase,
)
from aipcs_mcp.application.registry_authority import (
    CompletedRegistryClaim,
    ImportIdentity,
    PortableIntent,
    PortableOperationKind,
    PurgeAuthority,
    PurgeAuthorityKind,
    PurgeTombstone,
    ReceiptKind,
    ReceiptVerification,
    RegistryAuthorityIntent,
    StorageBackend,
    TombstonedRegistryClaim,
    TransferReceipt,
)
from aipcs_mcp.lifecycle import MAX_IDEMPOTENCY_KEY_LENGTH, MAX_PRINCIPAL_LENGTH
from aipcs_mcp.storage.codecs import decode_result, decode_time, encode_result, encode_time
from aipcs_mcp.storage.errors import StorageMigrationError


def encode_registry_authority_intent(value: RegistryAuthorityIntent) -> str:
    """Encode one exact typed intent for an ``intent_json`` column."""

    try:
        if type(value) is OperationalLifecycleIntent:
            payload: dict[str, object] = {
                "type": "operational",
                "kind": value.kind.value,
                "phase": value.phase.value,
                "principal_id": value.principal_id,
                "created_via": value.created_via,
                "service_id": str(value.service_id),
                "expected_service_revision": value.expected_service_revision,
                "target_operational_status": value.target_operational_status,
                "idempotency_key": value.idempotency_key,
                "fingerprint": value.fingerprint,
            }
        elif type(value) is PortableIntent:
            payload = _portable_intent_payload(value)
        else:
            raise ValueError
        return _json(payload)
    except Exception:
        raise StorageMigrationError() from None


def decode_registry_authority_intent(value: object) -> RegistryAuthorityIntent:
    """Decode only an exact canonical ``intent_json`` document."""

    try:
        raw = _object(value)
        kind = raw["type"]
        if kind == "operational":
            _keys(
                raw,
                {
                    "type", "kind", "phase", "principal_id", "created_via", "service_id",
                    "expected_service_revision", "target_operational_status", "idempotency_key", "fingerprint",
                },
            )
            result: RegistryAuthorityIntent = OperationalLifecycleIntent(
                kind=OperationalLifecycleKind(raw["kind"]),
                phase=OperationalLifecyclePhase(raw["phase"]),
                principal_id=raw["principal_id"],
                created_via=raw["created_via"],
                service_id=_uuid(raw["service_id"]),
                expected_service_revision=raw["expected_service_revision"],
                target_operational_status=raw["target_operational_status"],
                idempotency_key=raw["idempotency_key"],
                fingerprint=raw["fingerprint"],
            )
        elif kind == "portable":
            result = _portable_intent_from_payload(raw)
        else:
            raise ValueError
        if encode_registry_authority_intent(result) != value:
            raise ValueError
        return result
    except Exception:
        raise StorageMigrationError() from None


def encode_registry_authority_result(
    value: object,
) -> str:
    """Encode one terminal ``result_json`` without storage location information."""

    try:
        if type(value) is Service:
            payload = {"type": "service", "service": json.loads(encode_result(value))}
        elif type(value) is TransferReceipt:
            payload = {"type": "transfer_receipt", "receipt": _receipt_payload(value)}
        elif type(value) is PurgeTombstone:
            payload = {"type": "purge_tombstone", "tombstone": _tombstone_payload(value)}
        elif type(value) is TombstonedRegistryClaim:
            payload = {"type": "tombstoned_claim", "claim": _tombstoned_payload(value)}
        else:
            raise ValueError
        return _json(payload)
    except Exception:
        raise StorageMigrationError() from None


def decode_registry_authority_result(value: object, intent: RegistryAuthorityIntent | None) -> object:
    """Decode an exact result and validate its pairing with an optional claim intent."""

    try:
        raw = _object(value)
        result_type = raw["type"]
        if result_type == "service":
            _keys(raw, {"type", "service"})
            service_raw = raw["service"]
            if type(service_raw) is not dict:
                raise ValueError
            result = decode_result(
                _json(service_raw), service_raw["principal_id"], service_raw["service_id"]
            )
        elif result_type == "transfer_receipt":
            _keys(raw, {"type", "receipt"})
            result = _receipt_from_payload(_mapping(raw["receipt"]))
        elif result_type == "purge_tombstone":
            _keys(raw, {"type", "tombstone"})
            result = _tombstone_from_payload(_mapping(raw["tombstone"]))
        elif result_type == "tombstoned_claim":
            _keys(raw, {"type", "claim"})
            result = _tombstoned_from_payload(_mapping(raw["claim"]))
        else:
            raise ValueError
        if encode_registry_authority_result(result) != value:
            raise ValueError
        _validate_result_pairing(intent, result)
        return result
    except Exception:
        raise StorageMigrationError() from None


def encode_tombstoned_registry_claim(value: TombstonedRegistryClaim) -> str:
    try:
        if type(value) is not TombstonedRegistryClaim:
            raise ValueError
        return _json(_tombstoned_payload(value))
    except Exception:
        raise StorageMigrationError() from None


def decode_tombstoned_registry_claim(value: object) -> TombstonedRegistryClaim:
    try:
        result = _tombstoned_from_payload(_object(value))
        if encode_tombstoned_registry_claim(result) != value:
            raise ValueError
        return result
    except Exception:
        raise StorageMigrationError() from None


def validate_registry_authority_claim_row(row: Mapping[str, Any]) -> RegistryAuthorityIntent | object:
    """Validate one new-authority claim row, never legacy mutation evidence.

    Legacy/seed/design rows remain owned by ``storage.codecs``.  This codec is
    deliberately scoped to the new authority kinds and frozen physical schema.
    """

    try:
        _keys(
            row,
            {
                "principal_id",
                "idempotency_key",
                "service_id",
                "operation_kind",
                "phase",
                "created_via",
                "fingerprint",
                "intent_json",
                "result_json",
                "recovery_category",
            },
        )
        operation_kind = row["operation_kind"]
        if operation_kind not in {"suspend", "resume", "archive", "restore", "export", "import", "purge"}:
            raise ValueError
        if row["phase"] == "tombstoned":
            if (
                row["created_via"] is not None
                or row["intent_json"] is not None
                or row["result_json"] is not None
                or row["recovery_category"] is not None
            ):
                raise ValueError
            _text(row["principal_id"], MAX_PRINCIPAL_LENGTH)
            return TombstonedRegistryClaim(
                row["idempotency_key"], row["fingerprint"], operation_kind, _uuid(row["service_id"])
            )
        intent = decode_registry_authority_intent(row["intent_json"])
        if (
            row["principal_id"] != intent.principal_id
            or row["idempotency_key"] != intent.idempotency_key
            or row["fingerprint"] != intent.fingerprint
            or operation_kind != intent.kind.value
            or _uuid(row["service_id"]) != intent.service_id
            or row["phase"] != intent.phase.value
            or row["created_via"] != intent.created_via
        ):
            raise ValueError
        result_json = row["result_json"]
        recovery_category = row["recovery_category"]
        if intent.phase is OperationalLifecyclePhase.PREPARED:
            if result_json is not None or recovery_category is not None:
                raise ValueError
            return intent
        if intent.phase is OperationalLifecyclePhase.RECOVERY_REQUIRED:
            if result_json is not None or recovery_category != "recovery_required":
                raise ValueError
            return intent
        if recovery_category is not None or type(result_json) is not str:
            raise ValueError
        return decode_registry_authority_result(result_json, intent)
    except Exception:
        raise StorageMigrationError() from None


def validate_import_identity_row(row: Mapping[str, Any]) -> ImportIdentity:
    try:
        _keys(
            row,
            {
                "service_id",
                "principal_id",
                "identity_state",
                "domain_name",
                "storage_backend",
                "storage_namespace",
                "claim_idempotency_key",
                "lifecycle_at",
            },
        )
        if row["identity_state"] != "import_prepared":
            raise ValueError
        StorageBackend(row["storage_backend"])
        _text(row["claim_idempotency_key"], MAX_IDEMPOTENCY_KEY_LENGTH)
        _canonical_time(row["lifecycle_at"])
        identity = ImportIdentity(
            row["principal_id"], _uuid(row["service_id"]), row["domain_name"], row["storage_namespace"]
        )
        return identity
    except Exception:
        raise StorageMigrationError() from None


def validate_transfer_receipt_row(row: Mapping[str, Any]) -> TransferReceipt:
    try:
        _keys(
            row,
            {
                "receipt_id",
                "principal_id",
                "idempotency_key",
                "service_id",
                "receipt_kind",
                "bundle_root_sha256",
                "export_format_version",
                "storage_backend",
                "service_revision",
                "operational_status",
                "verification",
                "created_at",
            },
        )
        _text(row["idempotency_key"], MAX_IDEMPOTENCY_KEY_LENGTH)
        return TransferReceipt(
            _uuid(row["receipt_id"]),
            ReceiptKind(row["receipt_kind"]),
            row["principal_id"],
            _uuid(row["service_id"]),
            row["bundle_root_sha256"],
            row["export_format_version"],
            StorageBackend(row["storage_backend"]),
            row["service_revision"],
            row["operational_status"],
            ReceiptVerification(row["verification"]),
            _canonical_time(row["created_at"]),
        )
    except Exception:
        raise StorageMigrationError() from None


def _portable_intent_payload(value: PortableIntent) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "portable",
        "kind": value.kind.value,
        "phase": value.phase.value,
        "principal_id": value.principal_id,
        "created_via": value.created_via,
        "service_id": str(value.service_id),
        "idempotency_key": value.idempotency_key,
        "fingerprint": value.fingerprint,
    }
    if value.kind is PortableOperationKind.IMPORT:
        assert value.identity is not None and value.bundle_root_sha256 is not None
        assert value.destination_backend is not None
        payload.update(
            {
                "identity": _identity_payload(value.identity),
                "bundle_root_sha256": value.bundle_root_sha256,
                "destination_backend": value.destination_backend.value,
            }
        )
    else:
        payload["expected_service_revision"] = value.expected_service_revision
        if value.kind is PortableOperationKind.PURGE:
            assert value.purge_authority is not None
            payload["purge_authority"] = _authority_payload(value.purge_authority)
    return payload


def _portable_intent_from_payload(raw: Mapping[str, object]) -> PortableIntent:
    common = {
        "kind": PortableOperationKind(raw["kind"]),
        "phase": OperationalLifecyclePhase(raw["phase"]),
        "principal_id": raw["principal_id"],
        "created_via": raw["created_via"],
        "service_id": _uuid(raw["service_id"]),
        "idempotency_key": raw["idempotency_key"],
        "fingerprint": raw["fingerprint"],
    }
    kind = common["kind"]
    if kind is PortableOperationKind.IMPORT:
        _keys(raw, set(common) | {"type", "identity", "bundle_root_sha256", "destination_backend"})
        return PortableIntent(
            **common,
            expected_service_revision=None,
            identity=_identity_from_payload(_mapping(raw["identity"])),
            bundle_root_sha256=raw["bundle_root_sha256"],
            destination_backend=StorageBackend(raw["destination_backend"]),
        )
    if kind is PortableOperationKind.EXPORT:
        _keys(raw, set(common) | {"type", "expected_service_revision"})
        return PortableIntent(**common, expected_service_revision=raw["expected_service_revision"])
    _keys(raw, set(common) | {"type", "expected_service_revision", "purge_authority"})
    return PortableIntent(
        **common,
        expected_service_revision=raw["expected_service_revision"],
        purge_authority=_authority_from_payload(_mapping(raw["purge_authority"])),
    )


def _identity_payload(value: ImportIdentity) -> dict[str, object]:
    return {
        "principal_id": value.principal_id,
        "service_id": str(value.service_id),
        "domain_name": value.domain_name,
        "logical_namespace": value.logical_namespace,
    }


def _identity_from_payload(raw: Mapping[str, object]) -> ImportIdentity:
    _keys(raw, {"principal_id", "service_id", "domain_name", "logical_namespace"})
    return ImportIdentity(raw["principal_id"], _uuid(raw["service_id"]), raw["domain_name"], raw["logical_namespace"])


def _authority_payload(value: PurgeAuthority) -> dict[str, object]:
    return {
        "kind": value.kind.value,
        "receipt_id": None if value.receipt_id is None else str(value.receipt_id),
    }


def _authority_from_payload(raw: Mapping[str, object]) -> PurgeAuthority:
    _keys(raw, {"kind", "receipt_id"})
    return PurgeAuthority(
        PurgeAuthorityKind(raw["kind"]),
        None if raw["receipt_id"] is None else _uuid(raw["receipt_id"]),
    )


def _receipt_payload(value: TransferReceipt) -> dict[str, object]:
    return {
        "receipt_id": str(value.receipt_id),
        "kind": value.kind.value,
        "principal_id": value.principal_id,
        "service_id": str(value.service_id),
        "bundle_root_sha256": value.bundle_root_sha256,
        "export_format_version": value.export_format_version,
        "storage_backend": value.storage_backend.value,
        "service_revision": value.service_revision,
        "operational_status": value.operational_status,
        "verification": value.verification.value,
        "created_at": encode_time(value.created_at),
    }


def _receipt_from_payload(raw: Mapping[str, object]) -> TransferReceipt:
    _keys(raw, {"receipt_id", "kind", "principal_id", "service_id", "bundle_root_sha256", "export_format_version", "storage_backend", "service_revision", "operational_status", "verification", "created_at"})
    return TransferReceipt(
        _uuid(raw["receipt_id"]), ReceiptKind(raw["kind"]), raw["principal_id"], _uuid(raw["service_id"]),
        raw["bundle_root_sha256"], raw["export_format_version"], StorageBackend(raw["storage_backend"]),
        raw["service_revision"], raw["operational_status"], ReceiptVerification(raw["verification"]),
        decode_time(raw["created_at"]),
    )


def _tombstone_payload(value: PurgeTombstone) -> dict[str, object]:
    return {
        "principal_id": value.principal_id,
        "service_id": str(value.service_id),
        "logical_namespace": value.logical_namespace,
        "created_via": value.created_via,
        "purged_at": encode_time(value.purged_at),
        "authority": _authority_payload(value.authority),
        "receipt_id": None if value.receipt_id is None else str(value.receipt_id),
    }


def _tombstone_from_payload(raw: Mapping[str, object]) -> PurgeTombstone:
    _keys(raw, {"principal_id", "service_id", "logical_namespace", "created_via", "purged_at", "authority", "receipt_id"})
    return PurgeTombstone(
        raw["principal_id"], _uuid(raw["service_id"]), raw["logical_namespace"], raw["created_via"],
        decode_time(raw["purged_at"]), _authority_from_payload(_mapping(raw["authority"])),
        None if raw["receipt_id"] is None else _uuid(raw["receipt_id"]),
    )


def _tombstoned_payload(value: TombstonedRegistryClaim) -> dict[str, object]:
    return {
        "idempotency_key": value.idempotency_key,
        "fingerprint": value.fingerprint,
        "operation_kind": value.operation_kind,
        "service_id": str(value.service_id),
    }


def _tombstoned_from_payload(raw: Mapping[str, object]) -> TombstonedRegistryClaim:
    _keys(raw, {"idempotency_key", "fingerprint", "operation_kind", "service_id"})
    return TombstonedRegistryClaim(raw["idempotency_key"], raw["fingerprint"], raw["operation_kind"], _uuid(raw["service_id"]))


def _validate_result_pairing(intent: RegistryAuthorityIntent | None, result: object) -> None:
    if intent is None:
        if type(result) is not TombstonedRegistryClaim:
            raise ValueError
        return
    if intent.phase is not OperationalLifecyclePhase.COMPLETED:
        raise ValueError
    CompletedRegistryClaim(intent, result)  # type: ignore[arg-type]


def _object(value: object) -> dict[str, object]:
    if type(value) is not str:
        raise ValueError
    return _mapping(json.loads(value))


def _mapping(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError
    return value


def _keys(value: Mapping[str, object], expected: set[str]) -> None:
    if set(value) != expected:
        raise ValueError


def _uuid(value: object) -> UUID:
    if type(value) is not str:
        raise ValueError
    parsed = UUID(value)
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError
    return parsed


def _canonical_time(value: object):
    if type(value) is not str:
        raise ValueError
    parsed = decode_time(value)
    if encode_time(parsed) != value:
        raise ValueError
    return parsed


def _text(value: object, maximum: int) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum or value != value.strip():
        raise ValueError
    return value


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
