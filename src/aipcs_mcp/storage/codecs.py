"""Backend-neutral canonical codecs for durable registry values and evidence."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from aipcs_mcp.application.models import (
    CompletedLifecycleClaim,
    CompletedNonLifecycleClaim,
    MaterialisationStorage,
    PreparedLifecycleClaim,
    RecoveryRequiredLifecycleClaim,
    Service,
)
from aipcs_mcp.lifecycle import LifecycleIntent, LifecycleKind, LifecyclePhase
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.storage.errors import StorageMigrationError

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_LEGACY_RESULT_KEYS = {
    "service_id",
    "principal_id",
    "domain_name",
    "domain_class",
    "intent_description",
    "created_at",
    "updated_at",
    "last_activity_at",
    "manifest",
    "schema_version",
    "design_state",
    "operational_status",
}
_RESULT_KEYS = _LEGACY_RESULT_KEYS | {
    "service_revision",
    "materialised_at",
    "storage",
}
_STORAGE_KEYS = {"backend", "namespace"}


def encode_time(value: datetime) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is not UTC
        or value.utcoffset() != timedelta(0)
    ):
        raise StorageMigrationError()
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def decode_time(value: object) -> datetime:
    try:
        if type(value) is not str or len(value) != 27:
            raise ValueError
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        if encode_time(parsed) != value:
            raise ValueError
    except Exception:
        pass
    else:
        return parsed
    raise StorageMigrationError() from None


def encode_service_values(service: Service) -> tuple[Any, ...]:
    checked = validate_service(service)
    manifest = _manifest_json(checked.manifest)
    storage = checked.storage
    return (
        str(checked.service_id),
        checked.principal_id,
        checked.domain_name,
        checked.domain_class,
        checked.intent_description,
        encode_time(checked.created_at),
        encode_time(checked.updated_at),
        encode_time(checked.last_activity_at),
        manifest,
        checked.schema_version,
        checked.design_state,
        checked.operational_status,
        checked.service_revision,
        None if checked.materialised_at is None else encode_time(checked.materialised_at),
        None if storage is None else storage.backend,
        None if storage is None else storage.namespace,
    )


def decode_service(row: Mapping[str, Any]) -> Service:
    try:
        value = _service_from_values(
            row,
            service_revision=row["service_revision"],
            materialised_at=row["materialised_at"],
            storage_backend=row["storage_backend"],
            storage_namespace=row["storage_namespace"],
        )
        value = validate_service(value)
    except Exception:
        pass
    else:
        return value
    raise StorageMigrationError() from None


def decode_r1_service(row: Mapping[str, Any]) -> Service:
    """Decode only a public-reachable revision-1 service during explicit migration."""

    try:
        value = _service_from_values(
            row,
            service_revision=1,
            materialised_at=None,
            storage_backend=None,
            storage_namespace=None,
        )
        value = validate_service(value)
        if (
            value.design_state != "seeded"
            or value.operational_status != "active"
            or value.updated_at != value.last_activity_at
            or value.created_at > value.updated_at
            or (value.manifest is None and value.created_at != value.updated_at)
            or (
                value.manifest is not None
                and (
                    value.schema_version != 1
                    or value.manifest.schema_version != 1
                    or value.manifest.migration_history
                )
            )
        ):
            raise ValueError
    except Exception:
        pass
    else:
        return value
    raise StorageMigrationError() from None


def encode_result(service: Service) -> str:
    value = validate_service(service)
    storage = value.storage
    return _json(
        {
            "service_id": str(value.service_id),
            "principal_id": value.principal_id,
            "domain_name": value.domain_name,
            "domain_class": value.domain_class,
            "intent_description": value.intent_description,
            "created_at": encode_time(value.created_at),
            "updated_at": encode_time(value.updated_at),
            "last_activity_at": encode_time(value.last_activity_at),
            "manifest": None
            if value.manifest is None
            else value.manifest.model_dump(mode="json", by_alias=True, warnings="error"),
            "schema_version": value.schema_version,
            "design_state": value.design_state,
            "operational_status": value.operational_status,
            "service_revision": value.service_revision,
            "materialised_at": None
            if value.materialised_at is None
            else encode_time(value.materialised_at),
            "storage": None
            if storage is None
            else {"backend": storage.backend, "namespace": storage.namespace},
        }
    )


def decode_result(text: object, principal_id: str, service_id: object) -> Service:
    try:
        raw = _result_document(text, _RESULT_KEYS)
        storage = _storage_from_document(raw["storage"])
        value = _service_from_result(
            raw,
            service_revision=raw["service_revision"],
            materialised_at=raw["materialised_at"],
            storage=storage,
        )
        if value.principal_id != principal_id or value.service_id != _uuid(service_id):
            raise ValueError
        if encode_result(value) != text:
            raise ValueError
        result = value
    except Exception:
        pass
    else:
        return result
    raise StorageMigrationError() from None


def decode_legacy_result(text: object, principal_id: str, service_id: object) -> Service:
    """Decode a byte-preserved R1 completion without consulting current service state."""

    try:
        raw = _result_document(text, _LEGACY_RESULT_KEYS)
        value = _service_from_result(
            raw,
            service_revision=1,
            materialised_at=None,
            storage=None,
        )
        value = decode_r1_service(_r1_row_from_service(value))
        if value.principal_id != principal_id or value.service_id != _uuid(service_id):
            raise ValueError
        if _encode_legacy_result(value) != text:
            raise ValueError
    except Exception:
        pass
    else:
        return value
    raise StorageMigrationError() from None


def encode_manifest(value: ManifestV2) -> str:
    """Return the one canonical JSON representation used by durable intent."""

    encoded = _manifest_json(value)
    if encoded is None:
        raise StorageMigrationError()
    return encoded


def decode_lifecycle_intent(row: Mapping[str, Any]) -> LifecycleIntent:
    """Rehydrate one exact phased lifecycle row as detached immutable evidence."""

    try:
        phase = LifecyclePhase(row["phase"])
        result_json = row["result_json"]
        recovery_category = row["recovery_category"]
        if phase is LifecyclePhase.PREPARED:
            if result_json is not None or recovery_category is not None:
                raise ValueError
        elif phase is LifecyclePhase.COMPLETED:
            if type(result_json) is not str or recovery_category is not None:
                raise ValueError
        elif result_json is not None or recovery_category != "recovery_required":
            raise ValueError
        manifest = _decode_manifest(row["target_manifest_json"])
        if manifest is None:
            raise ValueError
        intent = LifecycleIntent(
            kind=LifecycleKind(row["operation_kind"]),
            phase=phase,
            principal_id=_text(row["principal_id"], 128),
            created_via=_bounded_text(row["created_via"], 64),
            service_id=_uuid(row["service_id"]),
            expected_service_revision=row["expected_service_revision"],
            expected_schema_version=row["expected_schema_version"],
            idempotency_key=_bounded_text(row["idempotency_key"], 128),
            fingerprint=_fingerprint(row["fingerprint"]),
            target_manifest=manifest,
        )
    except Exception:
        pass
    else:
        return intent
    raise StorageMigrationError() from None


def validate_r1_mutation_row(row: Mapping[str, Any]) -> None:
    """Reject an R1 completion that the public registry application could not persist."""

    try:
        principal_id = _text(row["principal_id"], 128)
        _bounded_text(row["idempotency_key"], 128)
        _fingerprint(row["fingerprint"])
        service_id = str(_uuid(row["service_id"]))
        decode_legacy_result(row["result_json"], principal_id, service_id)
    except Exception:
        pass
    else:
        return
    raise StorageMigrationError() from None


def validate_r2_mutation_row(
    row: Mapping[str, Any],
) -> tuple[LifecycleIntent | None, Service | None]:
    """Decode one ledger row and return its typed lifecycle/completion evidence."""

    try:
        lifecycle_intent: LifecycleIntent | None = None
        result: Service | None = None
        principal_id = _text(row["principal_id"], 128)
        _bounded_text(row["idempotency_key"], 128)
        _fingerprint(row["fingerprint"])
        service_id = str(_uuid(row["service_id"]))
        operation_kind = row["operation_kind"]
        if operation_kind in {"legacy", "seed", "design"}:
            if (
                row["phase"] != "completed"
                or row["created_via"] is not None
                or row["expected_service_revision"] is not None
                or row["expected_schema_version"] is not None
                or row["target_manifest_json"] is not None
                or type(row["result_json"]) is not str
                or row["recovery_category"] is not None
            ):
                raise ValueError
            decoder = decode_legacy_result if operation_kind == "legacy" else decode_result
            service = decoder(row["result_json"], principal_id, service_id)
            CompletedNonLifecycleClaim(operation_kind, service)
            result = service
        else:
            intent = decode_lifecycle_intent(row)
            lifecycle_intent = intent
            if intent.phase is LifecyclePhase.PREPARED:
                PreparedLifecycleClaim(intent)
            elif intent.phase is LifecyclePhase.RECOVERY_REQUIRED:
                RecoveryRequiredLifecycleClaim(intent)
            else:
                service = decode_result(row["result_json"], principal_id, service_id)
                CompletedLifecycleClaim(intent, service)
                result = service
    except Exception:
        pass
    else:
        return lifecycle_intent, result
    raise StorageMigrationError() from None


def validate_r1_audit_row(row: Mapping[str, Any]) -> None:
    """Validate one exact public R1 metadata-only audit row."""

    _validate_audit_row(
        row,
        {
            ("seed", "created"),
            ("seed", "duplicate"),
            ("design", "accepted"),
        },
    )


def validate_audit_row(row: Mapping[str, Any]) -> None:
    """Validate one R2 metadata-only audit row without projecting it."""

    _validate_audit_row(
        row,
        {
            ("seed", "created"),
            ("seed", "duplicate"),
            ("design", "accepted"),
            ("materialise", "completed"),
            ("materialise", "recovery_required"),
            ("evolve", "completed"),
            ("evolve", "recovery_required"),
        },
    )


def _validate_audit_row(row: Mapping[str, Any], allowed_pairs: set[tuple[str, str]]) -> None:
    """Validate one metadata-only audit row against an explicit epoch vocabulary."""

    try:
        audit_id = row["audit_id"]
        action = row["action"]
        outcome = row["outcome"]
        if type(audit_id) is not int or audit_id < 1:
            raise ValueError
        if (action, outcome) not in allowed_pairs:
            raise ValueError
        _uuid(row["service_id"])
        _text(row["principal_id"], 128)
        _bounded_text(row["created_via"], 64)
        decode_time(row["at"])
    except Exception:
        pass
    else:
        return
    raise StorageMigrationError() from None


def validate_service(value: Service) -> Service:
    try:
        if type(value) is not Service:
            raise ValueError
        if type(value.service_id) is not UUID:
            raise ValueError
        _uuid(str(value.service_id))
        _text(value.principal_id, 128)
        _domain(value.domain_name)
        _text(value.domain_class, 64)
        _text(value.intent_description, 1000)
        encode_time(value.created_at)
        encode_time(value.updated_at)
        encode_time(value.last_activity_at)
        if not value.created_at <= value.updated_at <= value.last_activity_at:
            raise ValueError
        if value.design_state not in {"seeded", "materialised"}:
            raise ValueError
        if value.operational_status not in {"active", "suspended", "archived"}:
            raise ValueError
        if (value.manifest is None) != (value.schema_version is None):
            raise ValueError
        if value.manifest is not None:
            ManifestV2.model_validate(value.manifest.model_dump(mode="json", by_alias=True))
            if value.schema_version != value.manifest.schema_version:
                raise ValueError
        if value.schema_version is not None and (
            type(value.schema_version) is not int or not 1 <= value.schema_version <= 65
        ):
            raise ValueError
        if (
            type(value.service_revision) is not int
            or not 1 <= value.service_revision <= (1 << 63) - 1
        ):
            raise ValueError
        if (value.materialised_at is None) != (value.storage is None):
            raise ValueError
        if value.materialised_at is not None:
            encode_time(value.materialised_at)
            if not value.created_at <= value.materialised_at <= value.updated_at:
                raise ValueError
        if value.storage is not None:
            if type(value.storage) is not MaterialisationStorage:
                raise ValueError
            _text(value.storage.backend, 10)
            _text(value.storage.namespace, 36)
            if value.storage.namespace != f"svc_{value.service_id.hex}":
                raise ValueError
        if value.design_state == "seeded":
            if value.materialised_at is not None or value.storage is not None:
                raise ValueError
        elif value.manifest is None or value.materialised_at is None or value.storage is None:
            raise ValueError
    except Exception:
        pass
    else:
        return value
    raise StorageMigrationError() from None


def _service_from_values(
    row: Mapping[str, Any],
    *,
    service_revision: object,
    materialised_at: object,
    storage_backend: object,
    storage_namespace: object,
) -> Service:
    manifest = _decode_manifest(row["manifest_json"])
    storage = _storage_from_columns(storage_backend, storage_namespace)
    return Service(
        service_id=_uuid(row["service_id"]),
        principal_id=_text(row["principal_id"], 128),
        domain_name=_domain(row["domain_name"]),
        domain_class=_text(row["domain_class"], 64),
        intent_description=_text(row["intent_description"], 1000),
        created_at=decode_time(row["created_at"]),
        updated_at=decode_time(row["updated_at"]),
        last_activity_at=decode_time(row["last_activity_at"]),
        manifest=manifest,
        schema_version=row["schema_version"],
        design_state=row["design_state"],
        operational_status=row["operational_status"],
        service_revision=service_revision,
        materialised_at=None if materialised_at is None else decode_time(materialised_at),
        storage=storage,
    )


def _service_from_result(
    raw: Mapping[str, Any],
    *,
    service_revision: object,
    materialised_at: object,
    storage: MaterialisationStorage | None,
) -> Service:
    manifest = None if raw["manifest"] is None else ManifestV2.model_validate(raw["manifest"])
    value = Service(
        service_id=_uuid(raw["service_id"]),
        principal_id=_text(raw["principal_id"], 128),
        domain_name=_domain(raw["domain_name"]),
        domain_class=_text(raw["domain_class"], 64),
        intent_description=_text(raw["intent_description"], 1000),
        created_at=decode_time(raw["created_at"]),
        updated_at=decode_time(raw["updated_at"]),
        last_activity_at=decode_time(raw["last_activity_at"]),
        manifest=manifest,
        schema_version=raw["schema_version"],
        design_state=raw["design_state"],
        operational_status=raw["operational_status"],
        service_revision=service_revision,
        materialised_at=None if materialised_at is None else decode_time(materialised_at),
        storage=storage,
    )
    return validate_service(value)


def _result_document(text: object, keys: set[str]) -> Mapping[str, Any]:
    if type(text) is not str:
        raise ValueError
    raw = json.loads(text)
    if type(raw) is not dict or set(raw) != keys:
        raise ValueError
    return raw


def _storage_from_columns(
    backend: object, namespace: object
) -> MaterialisationStorage | None:
    if backend is None and namespace is None:
        return None
    if type(backend) is not str or type(namespace) is not str:
        raise ValueError
    return MaterialisationStorage(backend, namespace)


def _storage_from_document(value: object) -> MaterialisationStorage | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) != _STORAGE_KEYS:
        raise ValueError
    return MaterialisationStorage(value["backend"], value["namespace"])


def _r1_row_from_service(value: Service) -> dict[str, object]:
    return {
        "service_id": str(value.service_id),
        "principal_id": value.principal_id,
        "domain_name": value.domain_name,
        "domain_class": value.domain_class,
        "intent_description": value.intent_description,
        "created_at": encode_time(value.created_at),
        "updated_at": encode_time(value.updated_at),
        "last_activity_at": encode_time(value.last_activity_at),
        "manifest_json": _manifest_json(value.manifest),
        "schema_version": value.schema_version,
        "design_state": value.design_state,
        "operational_status": value.operational_status,
    }


def _encode_legacy_result(value: Service) -> str:
    return _json(
        {
            "service_id": str(value.service_id),
            "principal_id": value.principal_id,
            "domain_name": value.domain_name,
            "domain_class": value.domain_class,
            "intent_description": value.intent_description,
            "created_at": encode_time(value.created_at),
            "updated_at": encode_time(value.updated_at),
            "last_activity_at": encode_time(value.last_activity_at),
            "manifest": None
            if value.manifest is None
            else value.manifest.model_dump(mode="json", by_alias=True, warnings="error"),
            "schema_version": value.schema_version,
            "design_state": value.design_state,
            "operational_status": value.operational_status,
        }
    )


def _fingerprint(value: object) -> str:
    text = _text(value, 64)
    if len(text) != 64 or text != text.lower() or re.search(r"[^0-9a-f]", text):
        raise ValueError
    return text


def _uuid(value: object) -> UUID:
    if type(value) is not str:
        raise ValueError
    parsed = UUID(value)
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError
    return parsed


def _text(value: object, maximum: int) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > maximum
        or not value.isprintable()
    ):
        raise ValueError
    return value


def _bounded_text(value: object, maximum: int) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > maximum:
        raise ValueError
    return value


def _domain(value: object) -> str:
    value = _text(value, 63)
    if not _NAME.fullmatch(value):
        raise ValueError
    return value


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _manifest_json(value: ManifestV2 | None) -> str | None:
    if value is None:
        return None
    return _json(value.model_dump(mode="json", by_alias=True, warnings="error"))


def _decode_manifest(value: object) -> ManifestV2 | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError
    raw = json.loads(value)
    manifest = ManifestV2.model_validate(raw)
    if _manifest_json(manifest) != value:
        raise ValueError
    return manifest
