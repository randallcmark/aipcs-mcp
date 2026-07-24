"""Pure portable-lifecycle commands, results, and logical bundle payloads."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.records import (
    BranchValue,
    FrozenJsonObject,
    RecordHistoryEvent,
    RecordValue,
    compile_record_specification,
)

from .models import MaterialisationStorage, Service
from .portable_codec import decode_canonical_json_object, decode_non_trailer_frame
from .portable_models import PortableBundleContractError, PortableBundleSummary
from .portable_store import (
    BranchMembership,
    PortableStoreMember,
    WriteAdmissionFence,
    validate_portable_members,
)
from .registry_authority import PurgeTombstone, StorageBackend, TransferReceipt

_DOMAIN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class PortableResultCategory(StrEnum):
    COMPLETED = "completed"
    VALIDATED = "validated"
    MALFORMED_INPUT = "malformed_input"
    CHANGED_FINGERPRINT = "changed_fingerprint"
    STALE_REVISION = "stale_revision"
    UNSUPPORTED_TRANSITION = "unsupported_transition"
    OPERATION_IN_PROGRESS = "operation_in_progress"
    IDENTITY_COLLISION = "identity_collision"
    TOMBSTONED = "tombstoned"
    RECOVERY_REQUIRED = "recovery_required"
    OPERATION_UNCERTAIN = "operation_uncertain"
    STORAGE_BUSY = "storage_busy"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    INTERNAL_FAILURE = "internal_failure"


@dataclass(frozen=True, slots=True)
class ImportBundleRequest:
    principal_id: str
    created_via: str
    destination_backend: StorageBackend
    idempotency_key: str
    dry_run: bool = False

    def __post_init__(self) -> None:
        if (
            not _text(self.principal_id, 128)
            or not _text(self.created_via, 64)
            or type(self.destination_backend) is not StorageBackend
            or not _text(self.idempotency_key, 128)
            or type(self.dry_run) is not bool
        ):
            raise PortableBundleContractError()

    def __repr__(self) -> str:
        return "ImportBundleRequest(<redacted>)"


@dataclass(frozen=True, slots=True)
class PortableExecutionResult:
    category: PortableResultCategory
    service: Service | None = None
    receipt: TransferReceipt | None = None
    summary: PortableBundleSummary | None = None
    artifact_written: bool = False
    tombstone: PurgeTombstone | None = None

    def __post_init__(self) -> None:
        if (
            type(self.category) is not PortableResultCategory
            or (self.service is not None and type(self.service) is not Service)
            or (self.receipt is not None and type(self.receipt) is not TransferReceipt)
            or (
                self.tombstone is not None
                and type(self.tombstone) is not PurgeTombstone
            )
            or (self.summary is not None and type(self.summary) is not PortableBundleSummary)
            or type(self.artifact_written) is not bool
        ):
            raise PortableBundleContractError()


@dataclass(frozen=True, slots=True)
class PortableBundleContent:
    service: Service
    members: tuple[PortableStoreMember, ...]
    export_fence: WriteAdmissionFence | None

    def __post_init__(self) -> None:
        if (
            type(self.service) is not Service
            or type(self.members) is not tuple
            or any(type(member) is not PortableStoreMember for member in self.members)
            or (
                self.export_fence is not None
                and type(self.export_fence) is not WriteAdmissionFence
            )
        ):
            raise PortableBundleContractError()
        if self.service.design_state == "seeded":
            if self.members or self.export_fence is not None:
                raise PortableBundleContractError()
            return
        if (
            self.service.manifest is None
            or self.export_fence is None
            or self.export_fence.state != "closed"
            or self.service.operational_status not in {"suspended", "archived"}
        ):
            raise PortableBundleContractError()
        try:
            checked = validate_portable_members(
                self.members, compile_record_specification(self.service.manifest)
            )
        except Exception:
            raise PortableBundleContractError() from None
        if checked != self.members:
            raise PortableBundleContractError()


def encode_service_payload(
    service: Service, export_fence: WriteAdmissionFence | None
) -> dict[str, object]:
    """Encode the closed logical service frame without deployment-local ownership."""

    if (
        type(service) is not Service
        or _DOMAIN.fullmatch(service.domain_name) is None
        or not _text(service.domain_class, 64)
        or not _text(service.intent_description, 1_000)
    ):
        raise PortableBundleContractError()
    if service.design_state == "seeded":
        if export_fence is not None:
            raise PortableBundleContractError()
    elif (
        export_fence is None
        or export_fence.state != "closed"
        or service.operational_status not in {"suspended", "archived"}
    ):
        raise PortableBundleContractError()
    manifest = (
        None
        if service.manifest is None
        else service.manifest.model_dump(mode="json", by_alias=True, warnings="error")
    )
    return {
        "created_at": _format_time(service.created_at),
        "design_state": service.design_state,
        "domain_class": service.domain_class,
        "domain_name": service.domain_name,
        "export_fence_generation": (
            None if export_fence is None else export_fence.generation
        ),
        "export_fence_state": None if export_fence is None else export_fence.state,
        "intent_description": service.intent_description,
        "last_activity_at": _format_time(service.last_activity_at),
        "manifest": manifest,
        "materialised_at": (
            None if service.materialised_at is None else _format_time(service.materialised_at)
        ),
        "operational_status": service.operational_status,
        "schema_version": service.schema_version,
        "service_id": str(service.service_id),
        "service_revision": service.service_revision,
        "updated_at": _format_time(service.updated_at),
    }


def encode_member_payload(member: PortableStoreMember) -> dict[str, object]:
    value = member.value
    if type(value) is RecordValue:
        return {
            "created_at": _format_time(value.created_at),
            "created_via": value.created_via,
            "entity_name": value.entity_name,
            "fields": value.fields.to_dict(),
            "record_id": str(value.record_id),
            "record_version": value.record_version,
            "updated_at": _format_time(value.updated_at),
        }
    if type(value) is RecordHistoryEvent:
        return {
            "after": None if value.after is None else value.after.to_dict(),
            "before": None if value.before is None else value.before.to_dict(),
            "entity_name": value.entity_name,
            "kind": value.kind,
            "occurred_at": _format_time(value.occurred_at),
            "record_id": str(value.record_id),
            "record_version": value.record_version,
        }
    if type(value) is BranchValue:
        return {
            "branch_id": str(value.branch_id),
            "branch_revision": value.branch_revision,
            "branch_type": value.branch_type,
            "created_at": _format_time(value.created_at),
            "intent": value.intent,
            "parent_branch_id": (
                None if value.parent_branch_id is None else str(value.parent_branch_id)
            ),
            "retrieval_summary": value.retrieval_summary,
            "slug": value.slug,
            "status": value.status,
            "title": value.title,
            "updated_at": _format_time(value.updated_at),
        }
    if type(value) is BranchMembership:
        return {
            "branch_id": str(value.branch_id),
            "entity_name": value.entity_name,
            "record_id": str(value.record_id),
            "role": value.role,
        }
    raise PortableBundleContractError()


def decode_bundle_content(
    lines: Iterable[bytes],
    *,
    principal_id: str,
    destination_backend: StorageBackend,
) -> PortableBundleContent:
    """Decode an already canonical/digest-validated stream into detached logical values."""

    if not _text(principal_id, 128) or type(destination_backend) is not StorageBackend:
        raise PortableBundleContractError()
    service_payload: dict[str, object] | None = None
    raw_members: list[tuple[str, dict[str, object]]] = []
    try:
        for line in lines:
            decoded = decode_canonical_json_object(line[:-1])
            if decoded.get("kind") == "trailer":
                continue
            frame = decode_non_trailer_frame(decoded)
            payload = frame.payload.to_dict()
            if frame.kind == "header":
                continue
            if frame.kind == "service":
                if service_payload is not None:
                    raise PortableBundleContractError()
                service_payload = payload
            elif frame.kind in {"record", "history", "branch", "branch_membership"}:
                raw_members.append((frame.kind, payload))
            elif frame.kind == "audit":
                raise PortableBundleContractError()
        if service_payload is None:
            raise PortableBundleContractError()
        service, fence = _decode_service(
            service_payload, principal_id, destination_backend
        )
        members = tuple(_decode_member(kind, payload) for kind, payload in raw_members)
        return PortableBundleContent(service, members, fence)
    except PortableBundleContractError:
        raise
    except Exception:
        raise PortableBundleContractError() from None


def _decode_service(
    payload: dict[str, object],
    principal_id: str,
    destination_backend: StorageBackend,
) -> tuple[Service, WriteAdmissionFence | None]:
    _exact(
        payload,
        {
            "created_at",
            "design_state",
            "domain_class",
            "domain_name",
            "export_fence_generation",
            "export_fence_state",
            "intent_description",
            "last_activity_at",
            "manifest",
            "materialised_at",
            "operational_status",
            "schema_version",
            "service_id",
            "service_revision",
            "updated_at",
        },
    )
    service_id = _uuid(payload["service_id"])
    domain_name = payload["domain_name"]
    domain_class = payload["domain_class"]
    intent = payload["intent_description"]
    if (
        type(domain_name) is not str
        or _DOMAIN.fullmatch(domain_name) is None
        or not _text(domain_class, 64)
        or not _text(intent, 1_000)
    ):
        raise PortableBundleContractError()
    manifest_value = payload["manifest"]
    manifest = None if manifest_value is None else ManifestV2.model_validate(manifest_value)
    design_state = payload["design_state"]
    materialised = design_state == "materialised"
    if design_state not in {"seeded", "materialised"}:
        raise PortableBundleContractError()
    generation = payload["export_fence_generation"]
    fence_state = payload["export_fence_state"]
    if materialised:
        fence = WriteAdmissionFence(generation, fence_state)  # type: ignore[arg-type]
        if fence.state != "closed":
            raise PortableBundleContractError()
        storage = MaterialisationStorage(
            destination_backend.value, f"svc_{service_id.hex}"
        )
    else:
        if generation is not None or fence_state is not None:
            raise PortableBundleContractError()
        fence = None
        storage = None
    materialised_at = payload["materialised_at"]
    service = Service(
        service_id=service_id,
        principal_id=principal_id,
        domain_name=domain_name,
        domain_class=domain_class,  # type: ignore[arg-type]
        intent_description=intent,  # type: ignore[arg-type]
        created_at=_time(payload["created_at"]),
        updated_at=_time(payload["updated_at"]),
        last_activity_at=_time(payload["last_activity_at"]),
        manifest=manifest,
        schema_version=payload["schema_version"],  # type: ignore[arg-type]
        design_state=design_state,  # type: ignore[arg-type]
        operational_status=payload["operational_status"],  # type: ignore[arg-type]
        service_revision=payload["service_revision"],  # type: ignore[arg-type]
        materialised_at=None if materialised_at is None else _time(materialised_at),
        storage=storage,
    )
    return service, fence


def _decode_member(kind: str, payload: dict[str, object]) -> PortableStoreMember:
    if kind == "record":
        _exact(
            payload,
            {
                "created_at",
                "created_via",
                "entity_name",
                "fields",
                "record_id",
                "record_version",
                "updated_at",
            },
        )
        value = RecordValue(
            payload["entity_name"],  # type: ignore[arg-type]
            _uuid(payload["record_id"]),
            payload["record_version"],  # type: ignore[arg-type]
            _time(payload["created_at"]),
            _time(payload["updated_at"]),
            payload["created_via"],  # type: ignore[arg-type]
            _json_object(payload["fields"]),
        )
    elif kind == "history":
        _exact(
            payload,
            {
                "after",
                "before",
                "entity_name",
                "kind",
                "occurred_at",
                "record_id",
                "record_version",
            },
        )
        value = RecordHistoryEvent(
            payload["entity_name"],  # type: ignore[arg-type]
            _uuid(payload["record_id"]),
            payload["record_version"],  # type: ignore[arg-type]
            payload["kind"],  # type: ignore[arg-type]
            _time(payload["occurred_at"]),
            _optional_json_object(payload["before"]),
            _optional_json_object(payload["after"]),
        )
    elif kind == "branch":
        _exact(
            payload,
            {
                "branch_id",
                "branch_revision",
                "branch_type",
                "created_at",
                "intent",
                "parent_branch_id",
                "retrieval_summary",
                "slug",
                "status",
                "title",
                "updated_at",
            },
        )
        parent = payload["parent_branch_id"]
        value = BranchValue(
            _uuid(payload["branch_id"]),
            payload["slug"],  # type: ignore[arg-type]
            payload["title"],  # type: ignore[arg-type]
            payload["intent"],  # type: ignore[arg-type]
            payload["branch_type"],  # type: ignore[arg-type]
            None if parent is None else _uuid(parent),
            payload["status"],  # type: ignore[arg-type]
            payload["retrieval_summary"],  # type: ignore[arg-type]
            _time(payload["created_at"]),
            _time(payload["updated_at"]),
            payload["branch_revision"],  # type: ignore[arg-type]
        )
    elif kind == "branch_membership":
        _exact(payload, {"branch_id", "entity_name", "record_id", "role"})
        value = BranchMembership(
            _uuid(payload["branch_id"]),
            payload["entity_name"],  # type: ignore[arg-type]
            _uuid(payload["record_id"]),
            payload["role"],  # type: ignore[arg-type]
        )
    else:
        raise PortableBundleContractError()
    return PortableStoreMember(kind, value)  # type: ignore[arg-type]


def _exact(payload: dict[str, object], names: set[str]) -> None:
    if type(payload) is not dict or set(payload) != names:
        raise PortableBundleContractError()


def _uuid(value: object) -> UUID:
    if type(value) is not str:
        raise PortableBundleContractError()
    try:
        parsed = UUID(value)
    except Exception:
        raise PortableBundleContractError() from None
    if parsed.int == 0 or str(parsed) != value:
        raise PortableBundleContractError()
    return parsed


def _time(value: object) -> datetime:
    if type(value) is not str:
        raise PortableBundleContractError()
    try:
        parsed = datetime.strptime(value, _UTC_FORMAT).replace(tzinfo=UTC)
    except Exception:
        raise PortableBundleContractError() from None
    if _format_time(parsed) != value:
        raise PortableBundleContractError()
    return parsed


def _format_time(value: datetime) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is not UTC
        or value.utcoffset() is None
    ):
        raise PortableBundleContractError()
    return value.strftime(_UTC_FORMAT)


def _json_object(value: object) -> FrozenJsonObject:
    if type(value) is not dict:
        raise PortableBundleContractError()
    return FrozenJsonObject.from_mapping(value)


def _optional_json_object(value: object) -> FrozenJsonObject | None:
    return None if value is None else _json_object(value)


def _text(value: object, maximum: int) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and len(value) <= maximum
    )
