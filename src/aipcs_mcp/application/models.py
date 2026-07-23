"""Immutable internal lifecycle values."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from aipcs_mcp.contracts import ServiceMetadata
from aipcs_mcp.lifecycle import (
    MAX_SCHEMA_VERSION,
    MAX_SERVICE_REVISION,
    LifecycleIntent,
    LifecycleKind,
    LifecyclePhase,
    LifecycleResultCategory,
)
from aipcs_mcp.manifest_v2 import ManifestV2

NonLifecycleKind = Literal["seed", "design"]
CompletedNonLifecycleKind = Literal["legacy", "seed", "design"]
ServiceSaveResult = Literal["saved", "stale_revision"]

_STORAGE_BACKENDS = frozenset({"sqlite", "postgresql"})
_STORAGE_NAMESPACE = re.compile(r"^svc_[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class MaterialisationStorage:
    """Safe logical service-store identity, never a location or credential."""

    backend: Literal["sqlite", "postgresql"]
    namespace: str

    def __post_init__(self) -> None:
        if (
            type(self.backend) is not str
            or self.backend not in _STORAGE_BACKENDS
            or type(self.namespace) is not str
            or not _STORAGE_NAMESPACE.fullmatch(self.namespace)
            or self.namespace == f"svc_{'0' * 32}"
        ):
            raise ValueError("Materialisation storage is invalid.")


@dataclass(frozen=True)
class ApplicationContext:
    principal_id: str
    created_via: str


@dataclass(frozen=True)
class SeedCommand:
    domain_name: str
    domain_class: str
    intent_description: str
    idempotency_key: str


@dataclass(frozen=True)
class DesignCommand:
    service_id: UUID
    manifest: ManifestV2
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class Service:
    service_id: UUID
    principal_id: str
    domain_name: str
    domain_class: str
    intent_description: str
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime
    manifest: ManifestV2 | None = None
    schema_version: int | None = None
    design_state: Literal["seeded", "materialised"] = "seeded"
    operational_status: Literal["active", "suspended", "archived"] = "active"
    service_revision: int = 1
    materialised_at: datetime | None = None
    storage: MaterialisationStorage | None = None

    def __post_init__(self) -> None:
        if type(self.service_id) is not UUID or self.service_id.int == 0:
            raise ValueError("Service id is invalid.")
        if not all(
            _is_utc_datetime(value)
            for value in (self.created_at, self.updated_at, self.last_activity_at)
        ):
            raise ValueError("Service timestamps are invalid.")
        if not self.created_at <= self.updated_at <= self.last_activity_at:
            raise ValueError("Service timestamp order is invalid.")
        if type(self.service_revision) is not int or not 1 <= self.service_revision <= MAX_SERVICE_REVISION:
            raise ValueError("Service revision is invalid.")
        if self.design_state not in {"seeded", "materialised"}:
            raise ValueError("Service design state is invalid.")
        if self.operational_status not in {"active", "suspended", "archived"}:
            raise ValueError("Service operational status is invalid.")
        if (self.manifest is None) != (self.schema_version is None):
            raise ValueError("Service manifest and schema version must be paired.")
        if self.schema_version is not None and (
            type(self.schema_version) is not int or not 1 <= self.schema_version <= MAX_SCHEMA_VERSION
        ):
            raise ValueError("Service schema version is invalid.")
        if self.manifest is not None:
            try:
                snapshot = ManifestV2.model_validate(
                    self.manifest.model_dump(mode="json", by_alias=True, warnings="error")
                )
            except Exception as error:
                raise ValueError("Service manifest is invalid.") from error
            if snapshot.schema_version != self.schema_version:
                raise ValueError("Service manifest and schema version do not match.")
            object.__setattr__(self, "manifest", snapshot)
        if (self.materialised_at is None) != (self.storage is None):
            raise ValueError("Service materialisation metadata must be paired.")
        if self.materialised_at is not None and not _is_utc_datetime(self.materialised_at):
            raise ValueError("Service materialisation time is invalid.")
        if self.materialised_at is not None and not (
            self.created_at <= self.materialised_at <= self.updated_at
        ):
            raise ValueError("Service materialisation time is outside the service lifecycle.")
        if self.storage is not None:
            if type(self.storage) is not MaterialisationStorage:
                raise ValueError("Service materialisation storage is invalid.")
            if self.storage.namespace != f"svc_{self.service_id.hex}":
                raise ValueError("Service materialisation storage is not service scoped.")
        if self.design_state == "seeded":
            if self.materialised_at is not None or self.storage is not None:
                raise ValueError("Seeded service cannot carry materialisation metadata.")
        elif self.manifest is None or self.materialised_at is None or self.storage is None:
            raise ValueError("Materialised service requires schema and materialisation metadata.")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    action: str
    outcome: str
    service_id: UUID
    principal_id: str
    created_via: str
    at: datetime


@dataclass(frozen=True, slots=True)
class NewClaim:
    """A new non-lifecycle key with no persisted result."""


@dataclass(frozen=True, slots=True)
class ConflictClaim:
    """A global idempotency key reused with another fingerprint."""


@dataclass(frozen=True, slots=True)
class CompletedNonLifecycleClaim:
    """A detached seed, design, or historical legacy replay result."""

    operation_kind: CompletedNonLifecycleKind
    service: Service

    def __post_init__(self) -> None:
        if type(self.operation_kind) is not str or self.operation_kind not in {
            "legacy",
            "seed",
            "design",
        }:
            raise ValueError("Non-lifecycle completion kind is invalid.")
        snapshot = _service_snapshot(self.service)
        if self.operation_kind == "design" and (
            snapshot.design_state != "seeded"
            or snapshot.operational_status != "active"
            or snapshot.manifest is None
            or snapshot.schema_version != 1
            or snapshot.service_revision != 2
            or snapshot.updated_at != snapshot.last_activity_at
        ):
            raise ValueError("Design completion snapshot is invalid.")
        object.__setattr__(self, "service", snapshot)


@dataclass(frozen=True, slots=True)
class CompletedLifecycleClaim:
    """One completed lifecycle intent and its only terminal registry snapshot."""

    intent: LifecycleIntent
    service: Service

    def __post_init__(self) -> None:
        _require_lifecycle_intent(self.intent, LifecyclePhase.COMPLETED)
        snapshot = _service_snapshot(self.service)
        _validate_terminal_service(self.intent, snapshot)
        object.__setattr__(self, "service", snapshot)


@dataclass(frozen=True, slots=True)
class PreparedLifecycleClaim:
    """An exact prepared lifecycle intent with no terminal service result."""

    intent: LifecycleIntent

    def __post_init__(self) -> None:
        _require_lifecycle_intent(self.intent, LifecyclePhase.PREPARED)


@dataclass(frozen=True, slots=True)
class RecoveryRequiredLifecycleClaim:
    """A terminal bounded recovery result with its admitted immutable intent."""

    intent: LifecycleIntent
    category: LifecycleResultCategory = LifecycleResultCategory.RECOVERY_REQUIRED

    def __post_init__(self) -> None:
        _require_lifecycle_intent(self.intent, LifecyclePhase.RECOVERY_REQUIRED)
        if self.category is not LifecycleResultCategory.RECOVERY_REQUIRED:
            raise ValueError("Lifecycle recovery category is invalid.")


@dataclass(frozen=True, slots=True)
class StaleRevision:
    """A new lifecycle key whose expected registry revision is stale."""


@dataclass(frozen=True, slots=True)
class UnsupportedTransition:
    """A present service is outside the materialise/evolve state contract."""

    category: LifecycleResultCategory = LifecycleResultCategory.UNSUPPORTED_TRANSITION

    def __post_init__(self) -> None:
        if self.category is not LifecycleResultCategory.UNSUPPORTED_TRANSITION:
            raise ValueError("Lifecycle unsupported-transition category is invalid.")


@dataclass(frozen=True, slots=True)
class OperationInProgress:
    """A different key is blocked by an existing prepared lifecycle intent."""


@dataclass(frozen=True, slots=True)
class MaterialiseCompletion:
    """D-owned proof input for one prepared materialise finalization."""

    prepared_intent: LifecycleIntent
    at: datetime
    storage: MaterialisationStorage

    def __post_init__(self) -> None:
        _require_lifecycle_intent(
            self.prepared_intent, LifecyclePhase.PREPARED, LifecycleKind.MATERIALISE
        )
        if not _is_utc_datetime(self.at):
            raise ValueError("Materialise completion time is invalid.")
        if type(self.storage) is not MaterialisationStorage:
            raise ValueError("Materialise completion storage is invalid.")
        if self.storage.namespace != f"svc_{self.prepared_intent.service_id.hex}":
            raise ValueError("Materialise completion storage is not service scoped.")


@dataclass(frozen=True, slots=True)
class EvolveCompletion:
    """D-owned proof input for one prepared evolve finalization."""

    prepared_intent: LifecycleIntent
    at: datetime

    def __post_init__(self) -> None:
        _require_lifecycle_intent(self.prepared_intent, LifecyclePhase.PREPARED, LifecycleKind.EVOLVE)
        if not _is_utc_datetime(self.at):
            raise ValueError("Evolve completion time is invalid.")


type NonLifecycleRegistryOutcome = NewClaim | ConflictClaim | CompletedNonLifecycleClaim
type LifecycleRegistryOutcome = (
    ConflictClaim
    | CompletedLifecycleClaim
    | PreparedLifecycleClaim
    | RecoveryRequiredLifecycleClaim
    | StaleRevision
    | UnsupportedTransition
    | OperationInProgress
)
type LifecycleCompletion = MaterialiseCompletion | EvolveCompletion


def _is_utc_datetime(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is UTC and value.utcoffset() == timedelta(0)


def _service_snapshot(service: object) -> Service:
    if type(service) is not Service:
        raise ValueError("Service completion snapshot is invalid.")
    manifest = service.manifest.model_copy(deep=True) if service.manifest is not None else None
    return replace(service, manifest=manifest)


def _require_lifecycle_intent(
    intent: object,
    phase: LifecyclePhase,
    kind: LifecycleKind | None = None,
) -> None:
    if type(intent) is not LifecycleIntent or intent.phase is not phase:
        raise ValueError("Lifecycle intent phase is invalid.")
    if kind is not None and intent.kind is not kind:
        raise ValueError("Lifecycle intent kind is invalid.")


def _validate_terminal_service(intent: LifecycleIntent, service: Service) -> None:
    if (
        service.principal_id != intent.principal_id
        or service.service_id != intent.service_id
        or service.service_revision != intent.expected_service_revision + 1
    ):
        raise ValueError("Lifecycle terminal service identity or revision is invalid.")
    target = intent.target_manifest
    if service.manifest != target or service.schema_version != target.schema_version:
        raise ValueError("Lifecycle terminal service target is invalid.")
    if service.design_state != "materialised":
        raise ValueError("Lifecycle terminal service must be materialised.")
    if service.operational_status != "active":
        raise ValueError("Lifecycle terminal service must be active.")
    if service.updated_at != service.last_activity_at:
        raise ValueError("Lifecycle terminal service timestamps are invalid.")
    if intent.kind is LifecycleKind.MATERIALISE:
        if (
            service.schema_version != 1
            or service.materialised_at is None
            or service.materialised_at != service.updated_at
            or service.storage is None
        ):
            raise ValueError("Materialise terminal service is invalid.")
    elif service.materialised_at is None or service.storage is None:
        raise ValueError("Evolve terminal service is invalid.")


def project(service: Service) -> ServiceMetadata:
    return ServiceMetadata(
        service_id=service.service_id,
        domain_name=service.domain_name,
        domain_class=service.domain_class,
        intent_description=service.intent_description,
        design_state=service.design_state,
        operational_status=service.operational_status,
        schema=service.manifest.model_copy(deep=True) if service.manifest is not None else None,
        schema_version=service.schema_version,
        created_at=service.created_at,
        updated_at=service.updated_at,
        last_activity_at=service.last_activity_at,
    )
