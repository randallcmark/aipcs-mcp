"""Strict public result documents and detached projections for the data runtime.

The public transport layer may use these models to render successful data
operations.  They intentionally accept only the pure values in
``aipcs_mcp.records`` (or a small, structural bootstrap card) and build each
document from an explicit allow-list.  No error envelope is produced here:
callers continue to use :mod:`aipcs_mcp.errors` for failures.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, JsonValue

from .contracts import PublicModel
from .manifest_v2 import SERVER_MANAGED_FIELDS
from .records import (
    MAX_ASSIGNMENT_TARGETS,
    MAX_BOOTSTRAP_SERVICES,
    MAX_FACET_VALUES,
    MAX_MAINTENANCE_CANDIDATES,
    MAX_PAGE_LIMIT,
    MAX_SAMPLES_PER_ENTITY,
    MAX_SUMMARY_BRANCHES,
    AssignedRecordRef,
    BranchAssignmentOutcome,
    BranchCard,
    BranchMutationOutcome,
    BranchPage,
    BranchValue,
    DeleteRecordOutcome,
    FacetObservation,
    FacetValue,
    FrozenJsonObject,
    HistoryPage,
    MaintenanceCandidate,
    RecordHistoryEvent,
    RecordMutationOutcome,
    RecordPage,
    RecordSpecification,
    RecordValue,
    ServiceSummary,
)
from .records import MaintenanceResult as PureMaintenanceResult


class PublicResultProjectionError(ValueError):
    """Safe, non-diagnostic failure when an internal value cannot be projected."""

    def __init__(self) -> None:
        super().__init__("The operation result could not be safely projected.")


class ResultModel(PublicModel):
    """Strict, closed result documents; only projection helpers construct them."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


class RecordDocument(ResultModel):
    entity_name: str = Field(min_length=1, max_length=64)
    id: UUID
    record_version: int = Field(ge=1, le=2**63 - 1)
    created_at: datetime
    updated_at: datetime
    created_via: str = Field(min_length=1, max_length=128)
    fields: dict[str, JsonValue]


class RecordReference(ResultModel):
    entity_name: str = Field(min_length=1, max_length=64)
    id: UUID
    record_version: int = Field(ge=1, le=2**63 - 1)


class RecordIdentifier(ResultModel):
    """A record identity when the source value has no current revision."""

    entity_name: str = Field(min_length=1, max_length=64)
    id: UUID


class RecordMutationResult(ResultModel):
    record: RecordDocument
    replayed: bool


class RecordDeleteResult(ResultModel):
    id: UUID
    record_version: int = Field(ge=1, le=2**63 - 1)
    replayed: bool


class RecordPageResult(ResultModel):
    records: list[RecordDocument] = Field(max_length=MAX_PAGE_LIMIT)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=1024)


class RecordSnapshot(ResultModel):
    """A detached domain-field snapshot used by a history event."""

    fields: dict[str, JsonValue]


class RecordHistoryEventResult(ResultModel):
    entity_name: str = Field(min_length=1, max_length=64)
    id: UUID
    record_version: int = Field(ge=1, le=2**63 - 1)
    kind: Literal[
        "create",
        "update",
        "delete",
        "primary_assign",
        "primary_move",
        "primary_unassign",
        "related_assign",
        "related_unassign",
    ]
    occurred_at: datetime
    before: RecordSnapshot | None = None
    after: RecordSnapshot | None = None


class RecordHistoryResult(ResultModel):
    events: list[RecordHistoryEventResult] = Field(max_length=MAX_PAGE_LIMIT)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=1024)


class BranchDocument(ResultModel):
    id: UUID
    slug: str = Field(
        pattern=r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$", min_length=3, max_length=64
    )
    title: str = Field(min_length=1, max_length=160)
    intent: str = Field(min_length=1, max_length=1_000)
    type: str | None = Field(default=None, max_length=80)
    parent_id: UUID | None = None
    status: Literal["active", "archived", "superseded"]
    retrieval_summary: str | None = Field(default=None, max_length=1_000)
    created_at: datetime
    updated_at: datetime
    branch_revision: int = Field(ge=1, le=2**63 - 1)


class BranchMutationResult(ResultModel):
    branch: BranchDocument
    replayed: bool


class BranchPageResult(ResultModel):
    branches: list[BranchDocument] = Field(max_length=MAX_PAGE_LIMIT)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=1024)


class BranchAssignmentResult(ResultModel):
    records: list[RecordReference] = Field(min_length=1, max_length=MAX_ASSIGNMENT_TARGETS)
    replayed: bool


class BootstrapServiceCard(ResultModel):
    service_id: UUID
    domain_name: str = Field(min_length=1, max_length=96)
    domain_class: str = Field(min_length=1, max_length=64)
    intent: str = Field(min_length=1, max_length=1_000)
    state: Literal["seeded", "materialised"]
    operational_status: Literal["active", "suspended", "archived"]
    schema_version: int | None = Field(default=None, ge=1)
    recovery_state: Literal["clear", "pending", "recovery_required"]
    entities: list[str] = Field(max_length=32)


class BootstrapResult(ResultModel):
    services: list[BootstrapServiceCard] = Field(max_length=MAX_BOOTSTRAP_SERVICES)
    truncated: bool


class FieldFilterMode(ResultModel):
    field_name: str = Field(min_length=1, max_length=64)
    mode: Literal["exact", "membership"]
    value_shape: Literal["scalar", "single_string_member"]
    facet: bool


class EntityAffordance(ResultModel):
    entity_name: str = Field(min_length=1, max_length=64)
    filter_modes: list[FieldFilterMode] = Field(max_length=64)


class QueryGuidance(ResultModel):
    patterns: list[str] = Field(max_length=32)
    retrieval_guidance: str | None = Field(default=None, max_length=2_000)


class AuthorityFieldAvailability(ResultModel):
    """Declared conventional authority and validity fields; never an inference."""

    entity_name: str = Field(min_length=1, max_length=64)
    provenance_fields: list[Literal["provenance_type", "provenance_note", "provenance_source"]]
    source_fields: list[Literal["source_ref", "source_kind"]]
    scope_fields: list[Literal["scope", "scope_ref"]]
    confidence_field: Literal["confidence"] | None = None
    status_field: Literal["status"] | None = None
    validity_fields: list[Literal["valid_from", "valid_until"]]
    supersession_fields: list[Literal["supersedes", "superseded_by"]]


class EntityRecordCount(ResultModel):
    entity_name: str = Field(min_length=1, max_length=64)
    count: int | None = Field(default=None, ge=0)


class FacetValueCount(ResultModel):
    value: JsonValue
    count: int = Field(ge=0)


class FacetResult(ResultModel):
    entity_name: str = Field(min_length=1, max_length=64)
    field_name: str = Field(min_length=1, max_length=64)
    values: list[FacetValueCount] = Field(max_length=MAX_FACET_VALUES)


class BranchCardResult(ResultModel):
    branch: BranchDocument
    primary_record_count: int = Field(ge=0)
    related_record_count: int = Field(ge=0)


class EntitySamples(ResultModel):
    entity_name: str = Field(min_length=1, max_length=64)
    records: list[RecordDocument] = Field(max_length=MAX_SAMPLES_PER_ENTITY)


class ServiceSummaryResult(ResultModel):
    data_status: Literal["ready", "migration_required"]
    affordances: list[EntityAffordance] = Field(max_length=32)
    authority_field_availability: list[AuthorityFieldAvailability] = Field(max_length=32)
    query_guidance: QueryGuidance
    record_counts: list[EntityRecordCount] = Field(max_length=32)
    facets: list[FacetResult] | None = Field(default=None, max_length=64)
    branches: list[BranchCardResult] | None = Field(default=None, max_length=MAX_SUMMARY_BRANCHES)
    unbranched_record_count: int | None = Field(default=None, ge=0)
    samples: list[EntitySamples] | None = Field(default=None, max_length=32)


class MaintenanceCandidateResult(ResultModel):
    scan_type: Literal[
        "expired",
        "stale",
        "low_confidence",
        "superseded",
        "missing_authority",
        "unbranched",
        "duplicate_authority",
        "blob_candidate",
    ]
    record: RecordIdentifier
    detail: dict[str, JsonValue]


class MaintenanceResult(ResultModel):
    candidates: list[MaintenanceCandidateResult] = Field(max_length=MAX_MAINTENANCE_CANDIDATES)
    unavailable_scan_types: list[
        Literal[
            "expired",
            "stale",
            "low_confidence",
            "superseded",
            "missing_authority",
            "unbranched",
            "duplicate_authority",
            "blob_candidate",
        ]
    ]


def project_record_document(value: RecordValue) -> RecordDocument:
    _require_type(value, RecordValue)
    return RecordDocument(
        entity_name=_identifier(value.entity_name),
        id=_uuid(value.record_id),
        record_version=_revision(value.record_version),
        created_at=_datetime(value.created_at),
        updated_at=_datetime(value.updated_at),
        created_via=_text(value.created_via, 128),
        fields=_fields(value.fields),
    )


def project_record_mutation(value: RecordMutationOutcome) -> RecordMutationResult:
    _require_type(value, RecordMutationOutcome)
    if value.record is None:
        raise PublicResultProjectionError()
    return RecordMutationResult(record=project_record_document(value.record), replayed=_bool(value.replayed))


def project_record_delete(value: DeleteRecordOutcome) -> RecordDeleteResult:
    _require_type(value, DeleteRecordOutcome)
    return RecordDeleteResult(
        id=_uuid(value.deleted_record_id),
        record_version=_revision(value.deleted_record_version),
        replayed=_bool(value.replayed),
    )


def project_record_page(value: RecordPage) -> RecordPageResult:
    _require_type(value, RecordPage)
    return RecordPageResult(
        records=[project_record_document(record) for record in value.records],
        next_cursor=_cursor(value.next_cursor),
    )


def project_record_history(value: HistoryPage) -> RecordHistoryResult:
    _require_type(value, HistoryPage)
    return RecordHistoryResult(
        events=[_history_event(event) for event in value.events], next_cursor=_cursor(value.next_cursor)
    )


def project_branch_document(value: BranchValue) -> BranchDocument:
    _require_type(value, BranchValue)
    return BranchDocument(
        id=_uuid(value.branch_id),
        slug=_text(value.slug, 64),
        title=_text(value.title, 160),
        intent=_text(value.intent, 1_000),
        type=None if value.branch_type is None else _text(value.branch_type, 80),
        parent_id=None if value.parent_branch_id is None else _uuid(value.parent_branch_id),
        status=_branch_status(value.status),
        retrieval_summary=None
        if value.retrieval_summary is None
        else _text(value.retrieval_summary, 1_000),
        created_at=_datetime(value.created_at),
        updated_at=_datetime(value.updated_at),
        branch_revision=_revision(value.branch_revision),
    )


def project_branch_mutation(value: BranchMutationOutcome) -> BranchMutationResult:
    _require_type(value, BranchMutationOutcome)
    return BranchMutationResult(branch=project_branch_document(value.branch), replayed=_bool(value.replayed))


def project_branch_page(value: BranchPage) -> BranchPageResult:
    _require_type(value, BranchPage)
    return BranchPageResult(
        branches=[project_branch_document(branch) for branch in value.branches],
        next_cursor=_cursor(value.next_cursor),
    )


def project_branch_assignment(value: BranchAssignmentOutcome) -> BranchAssignmentResult:
    _require_type(value, BranchAssignmentOutcome)
    return BranchAssignmentResult(
        records=[_record_reference(record) for record in value.records], replayed=_bool(value.replayed)
    )


def project_bootstrap(services: tuple[object, ...], truncated: object) -> BootstrapResult:
    """Project structural registry cards without importing the application layer."""

    if type(services) is not tuple or len(services) > MAX_BOOTSTRAP_SERVICES:
        raise PublicResultProjectionError()
    try:
        cards = [_bootstrap_card(service) for service in services]
    except (TypeError, PublicResultProjectionError):
        raise PublicResultProjectionError() from None
    return BootstrapResult(services=cards, truncated=_bool(truncated))


def project_service_summary(value: ServiceSummary, specification: RecordSpecification) -> ServiceSummaryResult:
    _require_type(value, ServiceSummary)
    _require_type(specification, RecordSpecification)
    affordances = [
        EntityAffordance(
            entity_name=_identifier(entity.name),
            filter_modes=[
                FieldFilterMode(
                    field_name=_identifier(field.name),
                    mode=field.filter_mode,
                    value_shape="scalar" if field.filter_mode == "exact" else "single_string_member",
                    facet=any(
                        facet.entity_name == entity.name and facet.field_name == field.name
                        for facet in specification.discovery_facets
                    ),
                )
                for field in entity.domain_fields
                if field.filter_mode in {"exact", "membership"}
            ],
        )
        for entity in specification.entities
    ]
    authority = [_authority_fields(entity.name, entity.domain_fields) for entity in specification.entities]
    guidance = QueryGuidance(
        patterns=[_text(pattern, 240) for pattern in specification.query_patterns],
        retrieval_guidance=None
        if specification.retrieval_guidance is None
        else _text(specification.retrieval_guidance, 2_000),
    )
    names = [entity.name for entity in specification.entities]
    if value.data_status == "migration_required":
        return ServiceSummaryResult(
            data_status="migration_required",
            affordances=affordances,
            authority_field_availability=authority,
            query_guidance=guidance,
            record_counts=[EntityRecordCount(entity_name=name, count=None) for name in names],
            facets=None,
            branches=None,
            unbranched_record_count=None,
            samples=None,
        )
    if value.data_status != "ready":
        raise PublicResultProjectionError()
    counts = _summary_counts(value, names)
    facets = [_facet(facet, specification) for facet in value.facets]
    branches = [_branch_card(card) for card in value.branches]
    samples = [_samples(entry, specification) for entry in value.samples]
    return ServiceSummaryResult(
        data_status="ready",
        affordances=affordances,
        authority_field_availability=authority,
        query_guidance=guidance,
        record_counts=counts,
        facets=facets,
        branches=branches,
        unbranched_record_count=_count(value.unbranched_record_count),
        samples=samples,
    )


def project_maintenance(value: PureMaintenanceResult) -> MaintenanceResult:
    _require_type(value, PureMaintenanceResult)
    return MaintenanceResult(
        candidates=[_maintenance_candidate(candidate) for candidate in value.candidates],
        unavailable_scan_types=[_scan_type(scan_type) for scan_type in value.unavailable_scan_types],
    )


def _history_event(value: RecordHistoryEvent) -> RecordHistoryEventResult:
    _require_type(value, RecordHistoryEvent)
    before = None if value.before is None else RecordSnapshot(fields=_fields(value.before))
    after = None if value.after is None else RecordSnapshot(fields=_fields(value.after))
    return RecordHistoryEventResult(
        entity_name=_identifier(value.entity_name),
        id=_uuid(value.record_id),
        record_version=_revision(value.record_version),
        kind=_history_kind(value.kind),
        occurred_at=_datetime(value.occurred_at),
        before=before,
        after=after,
    )


def _record_reference(value: AssignedRecordRef) -> RecordReference:
    _require_type(value, AssignedRecordRef)
    return RecordReference(
        entity_name=_identifier(value.entity_name), id=_uuid(value.record_id), record_version=_revision(value.record_version)
    )


def _bootstrap_card(value: object) -> BootstrapServiceCard:
    # BootstrapService is deliberately not imported: public projection remains
    # independent of the application facade and uses a strict structural read.
    return BootstrapServiceCard(
        service_id=_uuid(_attribute(value, "service_id")),
        domain_name=_text(_attribute(value, "domain_name"), 96),
        domain_class=_text(_attribute(value, "domain_class"), 64),
        intent=_text(_attribute(value, "intent_description"), 1_000),
        state=_service_state(_attribute(value, "design_state")),
        operational_status=_operational_status(_attribute(value, "operational_status")),
        schema_version=_optional_revision(_attribute(value, "schema_version")),
        recovery_state=_recovery_state(_attribute(value, "recovery_state")),
        entities=[_identifier(name) for name in _names(_attribute(value, "entity_names"))],
    )


def _summary_counts(value: ServiceSummary, names: list[str]) -> list[EntityRecordCount]:
    counts: dict[str, int] = {}
    for item in value.record_counts:
        if type(item) is not tuple or len(item) != 2:
            raise PublicResultProjectionError()
        name, count = item
        name = _identifier(name)
        if name not in names or name in counts:
            raise PublicResultProjectionError()
        counts[name] = _count(count)
    if set(counts) != set(names):
        raise PublicResultProjectionError()
    return [EntityRecordCount(entity_name=name, count=counts[name]) for name in names]


def _facet(value: FacetObservation, specification: RecordSpecification) -> FacetResult:
    _require_type(value, FacetObservation)
    entity_name, field_name = _identifier(value.entity_name), _identifier(value.field_name)
    if field_name in SERVER_MANAGED_FIELDS or not any(
        facet.entity_name == entity_name and facet.field_name == field_name
        for facet in specification.discovery_facets
    ):
        raise PublicResultProjectionError()
    return FacetResult(
        entity_name=entity_name,
        field_name=field_name,
        values=[_facet_value(item) for item in value.values],
    )


def _authority_fields(entity_name: str, fields: tuple[object, ...]) -> AuthorityFieldAvailability:
    """Report only which recognised conventional fields the manifest declares."""

    names: set[str] = set()
    for field in fields:
        name = _attribute(field, "name")
        names.add(_identifier(name))
    return AuthorityFieldAvailability(
        entity_name=_identifier(entity_name),
        provenance_fields=[
            name for name in ("provenance_type", "provenance_note", "provenance_source") if name in names
        ],
        source_fields=[name for name in ("source_ref", "source_kind") if name in names],
        scope_fields=[name for name in ("scope", "scope_ref") if name in names],
        confidence_field="confidence" if "confidence" in names else None,
        status_field="status" if "status" in names else None,
        validity_fields=[name for name in ("valid_from", "valid_until") if name in names],
        supersession_fields=[name for name in ("supersedes", "superseded_by") if name in names],
    )


def _facet_value(value: FacetValue) -> FacetValueCount:
    _require_type(value, FacetValue)
    return FacetValueCount(value=_json(value.value), count=_count(value.count))


def _branch_card(value: BranchCard) -> BranchCardResult:
    _require_type(value, BranchCard)
    return BranchCardResult(
        branch=project_branch_document(value.branch),
        primary_record_count=_count(value.primary_record_count),
        related_record_count=_count(value.related_record_count),
    )


def _samples(value: object, specification: RecordSpecification) -> EntitySamples:
    if type(value) is not tuple or len(value) != 2:
        raise PublicResultProjectionError()
    entity_name, records = value
    entity_name = _identifier(entity_name)
    if specification.entity(entity_name) is None or type(records) is not tuple:
        raise PublicResultProjectionError()
    documents = [project_record_document(record) for record in records]
    if any(document.entity_name != entity_name for document in documents):
        raise PublicResultProjectionError()
    return EntitySamples(entity_name=entity_name, records=documents)


def _maintenance_candidate(value: MaintenanceCandidate) -> MaintenanceCandidateResult:
    _require_type(value, MaintenanceCandidate)
    return MaintenanceCandidateResult(
        scan_type=_scan_type(value.scan_type),
        record=RecordIdentifier(entity_name=_identifier(value.entity_name), id=_uuid(value.record_id)),
        detail=_fields(value.detail),
    )


def _fields(value: object) -> dict[str, JsonValue]:
    if type(value) is not FrozenJsonObject:
        raise PublicResultProjectionError()
    result = _json(value.to_dict())
    if type(result) is not dict or set(result) & set(SERVER_MANAGED_FIELDS):
        raise PublicResultProjectionError()
    return result


def _json(value: object) -> JsonValue:
    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise PublicResultProjectionError()
        return value
    if type(value) is list or type(value) is tuple:
        return [_json(item) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if type(key) is not str or not key or key in result:
                raise PublicResultProjectionError()
            result[key] = _json(item)
        return result
    raise PublicResultProjectionError()


def _attribute(value: object, name: str) -> object:
    try:
        return getattr(value, name)
    except Exception:
        raise PublicResultProjectionError() from None


def _require_type(value: object, expected: type[object]) -> None:
    if type(value) is not expected:
        raise PublicResultProjectionError()


def _identifier(value: object) -> str:
    if type(value) is not str or not value or len(value) > 64 or not value.isidentifier():
        raise PublicResultProjectionError()
    return value


def _text(value: object, maximum: int) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > maximum or "\x00" in value:
        raise PublicResultProjectionError()
    return value


def _uuid(value: object) -> UUID:
    if type(value) is not UUID or value.int == 0:
        raise PublicResultProjectionError()
    return value


def _revision(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or not 1 <= value <= 2**63 - 1:
        raise PublicResultProjectionError()
    return value


def _optional_revision(value: object) -> int | None:
    return None if value is None else _revision(value)


def _count(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise PublicResultProjectionError()
    return value


def _bool(value: object) -> bool:
    if type(value) is not bool:
        raise PublicResultProjectionError()
    return value


def _datetime(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is not UTC or value.utcoffset() != UTC.utcoffset(None):
        raise PublicResultProjectionError()
    return value


def _cursor(value: object) -> str | None:
    if value is None:
        return None
    raw = _attribute(value, "value")
    if type(raw) is not str or not raw or len(raw) > 1024:
        raise PublicResultProjectionError()
    return raw


def _names(value: object) -> tuple[object, ...]:
    if type(value) is not tuple or len(value) > 32:
        raise PublicResultProjectionError()
    return value


def _history_kind(value: object) -> Literal[
    "create", "update", "delete", "primary_assign", "primary_move", "primary_unassign", "related_assign", "related_unassign"
]:
    if value not in {
        "create", "update", "delete", "primary_assign", "primary_move", "primary_unassign", "related_assign", "related_unassign"
    }:
        raise PublicResultProjectionError()
    return value  # type: ignore[return-value]


def _branch_status(value: object) -> Literal["active", "archived", "superseded"]:
    if value not in {"active", "archived", "superseded"}:
        raise PublicResultProjectionError()
    return value  # type: ignore[return-value]


def _service_state(value: object) -> Literal["seeded", "materialised"]:
    if value not in {"seeded", "materialised"}:
        raise PublicResultProjectionError()
    return value  # type: ignore[return-value]


def _operational_status(value: object) -> Literal["active", "suspended", "archived"]:
    if value not in {"active", "suspended", "archived"}:
        raise PublicResultProjectionError()
    return value  # type: ignore[return-value]


def _recovery_state(value: object) -> Literal["clear", "pending", "recovery_required"]:
    if value not in {"clear", "pending", "recovery_required"}:
        raise PublicResultProjectionError()
    return value  # type: ignore[return-value]


def _scan_type(value: object) -> Literal[
    "expired", "stale", "low_confidence", "superseded", "missing_authority", "unbranched", "duplicate_authority", "blob_candidate"
]:
    if value not in {
        "expired", "stale", "low_confidence", "superseded", "missing_authority", "unbranched", "duplicate_authority", "blob_candidate"
    }:
        raise PublicResultProjectionError()
    return value  # type: ignore[return-value]
