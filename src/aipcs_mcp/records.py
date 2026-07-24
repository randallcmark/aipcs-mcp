"""Pure, immutable vocabulary for generic records, discovery, and topology.

This module deliberately has no knowledge of a database, transport, request
model, principal registry, or service-store location.  It is the lossless
record-facing companion to :mod:`aipcs_mcp.relational`: relational compilation
keeps only physical layout facts while this compilation retains the manifest
metadata needed to validate and describe records to an adapter.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from .manifest_v2 import IDENTIFIER_PATTERN, SERVER_MANAGED_FIELDS, ManifestV2
from .numeric_domain import is_exact_number, is_finite_real, is_signed_64_integer

MAX_RECORD_JSON_BYTES = 64 * 1024
MAX_STRING_BYTES = 16 * 1024
MAX_STRING_LIST_ITEMS = 256
MAX_STRING_LIST_ITEM_CHARS = 256
MAX_SEARCH_FILTERS = 16
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100
MAX_ASSIGNMENT_TARGETS = 100
MAX_BOOTSTRAP_SERVICES = 100
MAX_SUMMARY_BRANCHES = 100
MAX_FACET_VALUES = 20
MAX_SAMPLES_PER_ENTITY = 3
MAX_MAINTENANCE_CANDIDATES = 100
MAX_IDEMPOTENCY_KEY_LENGTH = 128

_IDENTIFIER = re.compile(IDENTIFIER_PATTERN)
_LOGICAL_TYPES = frozenset(
    {"string", "integer", "number", "boolean", "datetime", "uuid", "string_list"}
)
_SERVER_FIELDS = frozenset(SERVER_MANAGED_FIELDS)
_BRANCH_STATUSES = frozenset({"active", "archived", "superseded"})
_BRANCH_ROLES = frozenset({"primary", "related"})
_ASSIGNMENT_OPERATIONS = frozenset({"assign", "unassign"})
_FAILURE_CODES = frozenset(
    {
        "validation_failed",
        "not_found",
        "already_exists",
        "changed_fingerprint",
        "stale_record_version",
        "stale_branch_revision",
        "constraint_violation",
        "storage_migration_required",
        "storage_busy",
        "operation_uncertain",
        "storage_unavailable",
        "write_admission_closed",
        "internal_error",
    }
)
_MAINTENANCE_SCAN_TYPES = frozenset(
    {
        "expired",
        "stale",
        "low_confidence",
        "superseded",
        "missing_authority",
        "unbranched",
        "duplicate_authority",
        "blob_candidate",
    }
)

LogicalType = Literal[
    "string", "integer", "number", "boolean", "datetime", "uuid", "string_list"
]
FilterMode = Literal["exact", "membership", "none"]
BranchStatus = Literal["active", "archived", "superseded"]
BranchRole = Literal["primary", "related"]
AssignmentOperation = Literal["assign", "unassign"]
DataFailureCode = Literal[
    "validation_failed",
    "not_found",
    "already_exists",
    "changed_fingerprint",
    "stale_record_version",
    "stale_branch_revision",
    "constraint_violation",
    "storage_migration_required",
    "storage_busy",
    "operation_uncertain",
    "storage_unavailable",
    "write_admission_closed",
    "internal_error",
]


class RecordContractError(ValueError):
    """Bounded failure for invalid pure record and discovery values."""

    message = "The record contract value is invalid."

    def __init__(self) -> None:
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class FrozenJsonObject(Mapping[str, object]):
    """An immutable, recursively detached JSON object with a canonical size bound."""

    entries: tuple[tuple[str, object], ...]

    def __post_init__(self) -> None:
        if type(self.entries) is not tuple:
            raise RecordContractError()
        names: list[str] = []
        for entry in self.entries:
            if type(entry) is not tuple or len(entry) != 2:
                raise RecordContractError()
            name, value = entry
            if type(name) is not str or not name or "\x00" in name:
                raise RecordContractError()
            _validate_json(value)
            names.append(name)
        if len(names) != len(set(names)) or names != sorted(names):
            raise RecordContractError()
        object.__setattr__(self, "entries", tuple((name, _freeze_json(value)) for name, value in self.entries))
        if _json_size(self) > MAX_RECORD_JSON_BYTES:
            raise RecordContractError()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> FrozenJsonObject:
        if not isinstance(value, Mapping):
            raise RecordContractError()
        items = tuple(value.items())
        if any(type(key) is not str for key, _ in items):
            raise RecordContractError()
        return cls(tuple((key, _freeze_json(item)) for key, item in sorted(items)))

    def __getitem__(self, key: str) -> object:
        for name, value in self.entries:
            if name == key:
                return value
        raise KeyError(key)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return (name for name, _ in self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def to_dict(self) -> dict[str, object]:
        return {name: _thaw_json(value) for name, value in self.entries}


@dataclass(frozen=True, slots=True)
class RecordField:
    name: str
    logical_type: LogicalType
    required: bool
    primary_key: bool
    description: str | None
    allowed_values: tuple[str | int | float | bool, ...] | None
    retrieval_mode: Literal["annotation", "membership"] | None

    def __post_init__(self) -> None:
        if (
            not _identifier(self.name)
            or type(self.logical_type) is not str
            or self.logical_type not in _LOGICAL_TYPES
            or type(self.required) is not bool
            or type(self.primary_key) is not bool
            or (self.primary_key and not self.required)
            or (self.description is not None and type(self.description) is not str)
            or (self.allowed_values is not None and type(self.allowed_values) is not tuple)
            or self.retrieval_mode not in {None, "annotation", "membership"}
        ):
            raise RecordContractError()
        if self.retrieval_mode == "annotation" and self.logical_type != "string":
            raise RecordContractError()
        if self.retrieval_mode == "membership" and self.logical_type != "string_list":
            raise RecordContractError()
        if self.allowed_values is not None:
            if not self.allowed_values or len(self.allowed_values) > 32:
                raise RecordContractError()
            if len({(type(value), value) for value in self.allowed_values}) != len(
                self.allowed_values
            ) or any(
                not _matches_allowed_value(value, self.logical_type)
                for value in self.allowed_values
            ):
                raise RecordContractError()

    @property
    def filter_mode(self) -> FilterMode:
        if self.retrieval_mode == "annotation":
            return "none"
        if self.logical_type == "string_list":
            return "membership" if self.retrieval_mode == "membership" else "none"
        return "exact"


@dataclass(frozen=True, slots=True)
class RecordEntity:
    name: str
    fields: tuple[RecordField, ...]
    description: str | None

    def __post_init__(self) -> None:
        if (
            not _identifier(self.name)
            or type(self.fields) is not tuple
            or not self.fields
            or len(self.fields) > 64
            or any(type(field) is not RecordField for field in self.fields)
            or len({field.name for field in self.fields}) != len(self.fields)
            or tuple(field.name for field in self.fields)
            != tuple(sorted(field.name for field in self.fields))
            or (self.description is not None and type(self.description) is not str)
        ):
            raise RecordContractError()
        by_name = {field.name: field for field in self.fields}
        for name, expected in SERVER_MANAGED_FIELDS.items():
            field = by_name.get(name)
            if field is None or (
                field.logical_type,
                field.required,
                field.primary_key,
                field.description,
                field.allowed_values,
                field.retrieval_mode,
            ) != (
                expected["type"],
                expected["required"],
                expected["primary_key"],
                None,
                None,
                None,
            ):
                raise RecordContractError()

    @property
    def domain_fields(self) -> tuple[RecordField, ...]:
        return tuple(field for field in self.fields if field.name not in _SERVER_FIELDS)

    def field(self, name: str) -> RecordField | None:
        return next((field for field in self.fields if field.name == name), None)


@dataclass(frozen=True, slots=True)
class RecordRelationship:
    name: str
    source_entity: str
    source_field: str
    target_entity: str
    target_field: str
    on_delete: Literal["restrict"]

    def __post_init__(self) -> None:
        if (
            not all(
                _identifier(value)
                for value in (
                    self.name,
                    self.source_entity,
                    self.source_field,
                    self.target_entity,
                    self.target_field,
                )
            )
            or type(self.on_delete) is not str
            or self.on_delete != "restrict"
        ):
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class DiscoveryFacet:
    entity_name: str
    field_name: str
    filter_mode: FilterMode

    def __post_init__(self) -> None:
        if (
            not _identifier(self.entity_name)
            or not _identifier(self.field_name)
            or self.filter_mode not in {"exact", "membership", "none"}
        ):
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class RecordSpecification:
    """Detached record and retrieval facts compiled from one accepted manifest."""

    schema_version: int
    entities: tuple[RecordEntity, ...]
    relationships: tuple[RecordRelationship, ...]
    discovery_facets: tuple[DiscoveryFacet, ...]
    query_patterns: tuple[str, ...]
    retrieval_guidance: str | None

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or not 1 <= self.schema_version <= 65
            or type(self.entities) is not tuple
            or not self.entities
            or len(self.entities) > 32
            or any(type(entity) is not RecordEntity for entity in self.entities)
            or tuple(entity.name for entity in self.entities)
            != tuple(sorted(entity.name for entity in self.entities))
            or type(self.relationships) is not tuple
            or len(self.relationships) > 128
            or any(type(value) is not RecordRelationship for value in self.relationships)
            or tuple(value.name for value in self.relationships)
            != tuple(sorted(value.name for value in self.relationships))
            or type(self.discovery_facets) is not tuple
            or len(self.discovery_facets) > 64
            or any(type(value) is not DiscoveryFacet for value in self.discovery_facets)
            or type(self.query_patterns) is not tuple
            or len(self.query_patterns) > 32
            or any(type(value) is not str or not value or len(value) > 240 for value in self.query_patterns)
            or (self.retrieval_guidance is not None and type(self.retrieval_guidance) is not str)
        ):
            raise RecordContractError()
        names = {entity.name for entity in self.entities}
        if len(names) != len(self.entities):
            raise RecordContractError()
        for relation in self.relationships:
            source, target = self.entity(relation.source_entity), self.entity(relation.target_entity)
            if (
                source is None
                or target is None
                or source.field(relation.source_field) is None
                or target.field(relation.target_field) is None
            ):
                raise RecordContractError()
        for facet in self.discovery_facets:
            field = self.field(facet.entity_name, facet.field_name)
            if field is None or field.filter_mode != facet.filter_mode:
                raise RecordContractError()

    def entity(self, entity_name: str) -> RecordEntity | None:
        return next((entity for entity in self.entities if entity.name == entity_name), None)

    def field(self, entity_name: str, field_name: str) -> RecordField | None:
        entity = self.entity(entity_name)
        return None if entity is None else entity.field(field_name)

    def validate_create(self, entity_name: str, record: Mapping[str, object]) -> FrozenJsonObject:
        return self._validate_record(entity_name, record, require_required=True)

    def validate_updates(self, entity_name: str, updates: Mapping[str, object]) -> FrozenJsonObject:
        return self._validate_record(entity_name, updates, require_required=False, require_nonempty=True)

    def validate_filter(self, entity_name: str, field_name: str, value: object) -> RecordFilter:
        field = self.field(entity_name, field_name)
        if field is None or field.name in _SERVER_FIELDS or field.filter_mode == "none":
            raise RecordContractError()
        if field.filter_mode == "membership":
            if type(value) is not str or not value or len(value) > MAX_STRING_LIST_ITEM_CHARS:
                raise RecordContractError()
        elif not _matches_type(value, field.logical_type):
            raise RecordContractError()
        return RecordFilter(entity_name, field_name, field.filter_mode, _freeze_json(value))

    def _validate_record(
        self,
        entity_name: str,
        record: Mapping[str, object],
        *,
        require_required: bool,
        require_nonempty: bool = False,
    ) -> FrozenJsonObject:
        entity = self.entity(entity_name)
        if entity is None or not isinstance(record, Mapping):
            raise RecordContractError()
        if require_nonempty and not record:
            raise RecordContractError()
        fields = {field.name: field for field in entity.domain_fields}
        if set(record) - set(fields) or set(record) & _SERVER_FIELDS:
            raise RecordContractError()
        if require_required and any(field.required and field.name not in record for field in fields.values()):
            raise RecordContractError()
        for name, value in record.items():
            field = fields.get(name)
            if field is None:
                raise RecordContractError()
            if value is None:
                if field.required:
                    raise RecordContractError()
                continue
            if not _matches_type(value, field.logical_type):
                raise RecordContractError()
            if field.allowed_values is not None and not _matches_enumeration(
                value, field.logical_type, field.allowed_values
            ):
                raise RecordContractError()
        return FrozenJsonObject.from_mapping(record)


def compile_record_specification(manifest: ManifestV2) -> RecordSpecification:
    """Revalidate and detach one manifest before compiling record-facing facts."""

    if type(manifest) is not ManifestV2:
        raise RecordContractError()
    try:
        checked = ManifestV2.model_validate(manifest.model_dump(mode="json", by_alias=True))
    except Exception as error:
        raise RecordContractError() from error
    entities = tuple(
        RecordEntity(
            entity.name,
            tuple(
                RecordField(
                    attribute.name,
                    attribute.type,
                    attribute.required,
                    attribute.primary_key,
                    attribute.description,
                    None if attribute.allowed_values is None else tuple(attribute.allowed_values),
                    attribute.retrieval_mode,
                )
                for attribute in sorted(entity.attributes, key=lambda value: value.name)
            ),
            entity.description,
        )
        for entity in sorted(checked.entities, key=lambda value: value.name)
    )
    entity_by_name = {entity.name: entity for entity in entities}
    facets = tuple(
        DiscoveryFacet(
            facet.entity,
            facet.field,
            entity_by_name[facet.entity].field(facet.field).filter_mode,  # validated manifest
        )
        for facet in sorted(checked.discovery_facets, key=lambda value: (value.entity, value.field))
    )
    return RecordSpecification(
        checked.schema_version,
        entities,
        tuple(
            RecordRelationship(
                relation.name,
                relation.from_.entity,
                relation.from_.field,
                relation.to.entity,
                relation.to.field,
                relation.on_delete,
            )
            for relation in sorted(checked.relationships, key=lambda value: value.name)
        ),
        facets,
        tuple(checked.query_patterns),
        checked.retrieval_guidance,
    )


@dataclass(frozen=True, slots=True)
class PageCursor:
    """Opaque, query-bound cursor bytes represented as unambiguous URL-safe text."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str or not self.value or len(self.value) > 1024 or not re.fullmatch(
            r"[A-Za-z0-9_-]+", self.value
        ):
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class PageRequest:
    limit: int = DEFAULT_PAGE_LIMIT
    cursor: PageCursor | None = None

    def __post_init__(self) -> None:
        if (
            type(self.limit) is not int
            or isinstance(self.limit, bool)
            or not 1 <= self.limit <= MAX_PAGE_LIMIT
            or (self.cursor is not None and type(self.cursor) is not PageCursor)
        ):
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class RecordFilter:
    entity_name: str
    field_name: str
    mode: FilterMode
    value: object

    def __post_init__(self) -> None:
        if (
            not _identifier(self.entity_name)
            or not _identifier(self.field_name)
            or self.mode not in {"exact", "membership"}
        ):
            raise RecordContractError()
        _validate_json(self.value)


@dataclass(frozen=True, slots=True)
class CreateRecordCommand:
    entity_name: str
    record: FrozenJsonObject
    idempotency_key: str

    def __post_init__(self) -> None:
        _require_entity(self.entity_name)
        if type(self.record) is not FrozenJsonObject or _SERVER_FIELDS & set(self.record):
            raise RecordContractError()
        _require_idempotency_key(self.idempotency_key)


@dataclass(frozen=True, slots=True)
class GetRecordQuery:
    entity_name: str
    record_id: UUID

    def __post_init__(self) -> None:
        _require_entity(self.entity_name)
        _require_uuid(self.record_id)


@dataclass(frozen=True, slots=True)
class ListRecordsQuery:
    entity_name: str
    branch_id: UUID | None = None
    branch_role: BranchRole | None = None
    page: PageRequest = PageRequest()

    def __post_init__(self) -> None:
        _require_entity(self.entity_name)
        if self.branch_id is not None:
            _require_uuid(self.branch_id)
        if (self.branch_id is None) != (self.branch_role is None) or self.branch_role not in {
            None,
            "primary",
            "related",
        }:
            raise RecordContractError()
        if type(self.page) is not PageRequest:
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class SearchRecordsQuery:
    entity_name: str
    filters: tuple[RecordFilter, ...]
    branch_id: UUID | None = None
    branch_role: BranchRole | None = None
    page: PageRequest = PageRequest()

    def __post_init__(self) -> None:
        _require_entity(self.entity_name)
        if (
            type(self.filters) is not tuple
            or not self.filters
            or len(self.filters) > MAX_SEARCH_FILTERS
            or any(type(value) is not RecordFilter or value.entity_name != self.entity_name for value in self.filters)
            or len({value.field_name for value in self.filters}) != len(self.filters)
        ):
            raise RecordContractError()
        if self.branch_id is not None:
            _require_uuid(self.branch_id)
        if (self.branch_id is None) != (self.branch_role is None) or self.branch_role not in {
            None,
            "primary",
            "related",
        }:
            raise RecordContractError()
        if type(self.page) is not PageRequest:
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class UpdateRecordCommand:
    entity_name: str
    record_id: UUID
    updates: FrozenJsonObject
    expected_record_version: int
    idempotency_key: str

    def __post_init__(self) -> None:
        _require_entity(self.entity_name)
        _require_uuid(self.record_id)
        if (
            type(self.updates) is not FrozenJsonObject
            or not self.updates
            or _SERVER_FIELDS & set(self.updates)
        ):
            raise RecordContractError()
        _require_revision(self.expected_record_version)
        _require_idempotency_key(self.idempotency_key)


@dataclass(frozen=True, slots=True)
class DeleteRecordCommand:
    entity_name: str
    record_id: UUID
    expected_record_version: int
    idempotency_key: str

    def __post_init__(self) -> None:
        _require_entity(self.entity_name)
        _require_uuid(self.record_id)
        _require_revision(self.expected_record_version)
        _require_idempotency_key(self.idempotency_key)


@dataclass(frozen=True, slots=True)
class RecordHistoryQuery:
    entity_name: str
    record_id: UUID | None = None
    page: PageRequest = PageRequest()

    def __post_init__(self) -> None:
        _require_entity(self.entity_name)
        if self.record_id is not None:
            _require_uuid(self.record_id)
        if type(self.page) is not PageRequest:
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class CreateBranchCommand:
    slug: str
    title: str
    intent: str
    branch_type: str | None
    parent_branch_id: UUID | None
    retrieval_summary: str | None
    idempotency_key: str

    def __post_init__(self) -> None:
        _require_slug(self.slug)
        _require_text(self.title, 160)
        _require_text(self.intent, 1_000)
        _require_optional_branch_type(self.branch_type)
        if self.parent_branch_id is not None:
            _require_uuid(self.parent_branch_id)
        _require_optional_text(self.retrieval_summary, 1_000)
        _require_idempotency_key(self.idempotency_key)


@dataclass(frozen=True, slots=True)
class ListBranchesQuery:
    status: BranchStatus | None = None
    branch_type: str | None = None
    parent_branch_id: UUID | None = None
    page: PageRequest = PageRequest()

    def __post_init__(self) -> None:
        if self.status not in {None, "active", "archived", "superseded"}:
            raise RecordContractError()
        _require_optional_branch_type(self.branch_type)
        if self.parent_branch_id is not None:
            _require_uuid(self.parent_branch_id)
        if type(self.page) is not PageRequest:
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class UpdateBranchCommand:
    branch_id: UUID
    updates: FrozenJsonObject
    expected_branch_revision: int
    idempotency_key: str

    def __post_init__(self) -> None:
        _require_uuid(self.branch_id)
        _require_revision(self.expected_branch_revision)
        _require_idempotency_key(self.idempotency_key)
        if type(self.updates) is not FrozenJsonObject or not self.updates:
            raise RecordContractError()
        allowed = {"title", "intent", "branch_type", "parent_branch_id", "status", "retrieval_summary"}
        values = self.updates.to_dict()
        if set(values) - allowed:
            raise RecordContractError()
        if "title" in values:
            _require_text(values["title"], 160)
        if "intent" in values:
            _require_text(values["intent"], 1_000)
        if "branch_type" in values:
            _require_optional_branch_type(values["branch_type"])
        if "retrieval_summary" in values:
            _require_optional_text(values["retrieval_summary"], 1_000)
        if "parent_branch_id" in values and values["parent_branch_id"] is not None:
            if type(values["parent_branch_id"]) is not str or not _canonical_uuid(
                values["parent_branch_id"]
            ):
                raise RecordContractError()
        if "status" in values and (
            type(values["status"]) is not str or values["status"] not in _BRANCH_STATUSES
        ):
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class BranchAssignmentTarget:
    entity_name: str
    record_id: UUID
    expected_record_version: int

    def __post_init__(self) -> None:
        _require_entity(self.entity_name)
        _require_uuid(self.record_id)
        _require_revision(self.expected_record_version)


@dataclass(frozen=True, slots=True)
class AssignBranchRecordsCommand:
    branch_id: UUID
    role: BranchRole
    operation: AssignmentOperation
    targets: tuple[BranchAssignmentTarget, ...]
    idempotency_key: str

    def __post_init__(self) -> None:
        _require_uuid(self.branch_id)
        if self.role not in _BRANCH_ROLES or self.operation not in _ASSIGNMENT_OPERATIONS:
            raise RecordContractError()
        if (
            type(self.targets) is not tuple
            or not self.targets
            or len(self.targets) > MAX_ASSIGNMENT_TARGETS
            or any(type(target) is not BranchAssignmentTarget for target in self.targets)
            or len({(target.entity_name, target.record_id) for target in self.targets}) != len(self.targets)
        ):
            raise RecordContractError()
        _require_idempotency_key(self.idempotency_key)


@dataclass(frozen=True, slots=True)
class SummaryQuery:
    sample_sizes: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if type(self.sample_sizes) is not tuple or len(self.sample_sizes) > 32:
            raise RecordContractError()
        names: list[str] = []
        for entry in self.sample_sizes:
            if type(entry) is not tuple or len(entry) != 2:
                raise RecordContractError()
            name, size = entry
            _require_entity(name)
            if type(size) is not int or isinstance(size, bool) or not 0 <= size <= MAX_SAMPLES_PER_ENTITY:
                raise RecordContractError()
            names.append(name)
        if names != sorted(names) or len(names) != len(set(names)):
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class MaintenanceQuery:
    scan_types: tuple[str, ...]
    entity_name: str | None = None
    branch_id: UUID | None = None
    max_candidates: int = MAX_MAINTENANCE_CANDIDATES
    stale_after_days: int = 90
    low_confidence_below: float = 0.5

    def __post_init__(self) -> None:
        if (
            type(self.scan_types) is not tuple
            or not self.scan_types
            or len(self.scan_types) > len(_MAINTENANCE_SCAN_TYPES)
            or any(type(value) is not str or value not in _MAINTENANCE_SCAN_TYPES for value in self.scan_types)
            or len(set(self.scan_types)) != len(self.scan_types)
            or type(self.max_candidates) is not int
            or isinstance(self.max_candidates, bool)
            or not 1 <= self.max_candidates <= MAX_MAINTENANCE_CANDIDATES
        ):
            raise RecordContractError()
        if self.entity_name is not None:
            _require_entity(self.entity_name)
        if self.branch_id is not None:
            _require_uuid(self.branch_id)
        if (
            type(self.stale_after_days) is not int
            or isinstance(self.stale_after_days, bool)
            or not 1 <= self.stale_after_days <= 3_650
        ):
            raise RecordContractError()
        if (
            type(self.low_confidence_below) not in {int, float}
            or isinstance(self.low_confidence_below, bool)
            or not math.isfinite(self.low_confidence_below)
            or not 0 <= self.low_confidence_below <= 1
        ):
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class RecordValue:
    entity_name: str
    record_id: UUID
    record_version: int
    created_at: datetime
    updated_at: datetime
    created_via: str
    fields: FrozenJsonObject

    def __post_init__(self) -> None:
        _require_entity(self.entity_name)
        _require_uuid(self.record_id)
        _require_revision(self.record_version)
        if not _utc_datetime(self.created_at) or not _utc_datetime(self.updated_at) or self.created_at > self.updated_at:
            raise RecordContractError()
        _require_text(self.created_via, 128)
        if type(self.fields) is not FrozenJsonObject or _SERVER_FIELDS & set(self.fields):
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class RecordHistoryEvent:
    entity_name: str
    record_id: UUID
    record_version: int
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
    before: FrozenJsonObject | None
    after: FrozenJsonObject | None

    def __post_init__(self) -> None:
        _require_entity(self.entity_name)
        _require_uuid(self.record_id)
        _require_revision(self.record_version)
        if self.kind not in {
            "create",
            "update",
            "delete",
            "primary_assign",
            "primary_move",
            "primary_unassign",
            "related_assign",
            "related_unassign",
        }:
            raise RecordContractError()
        if not _utc_datetime(self.occurred_at) or (
            self.before is not None and type(self.before) is not FrozenJsonObject
        ) or (self.after is not None and type(self.after) is not FrozenJsonObject):
            raise RecordContractError()
        if self.kind == "create" and (self.before is not None or self.after is None):
            raise RecordContractError()
        if self.kind == "delete" and (self.before is None or self.after is not None):
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class BranchValue:
    branch_id: UUID
    slug: str
    title: str
    intent: str
    branch_type: str | None
    parent_branch_id: UUID | None
    status: BranchStatus
    retrieval_summary: str | None
    created_at: datetime
    updated_at: datetime
    branch_revision: int

    def __post_init__(self) -> None:
        _require_uuid(self.branch_id)
        _require_slug(self.slug)
        _require_text(self.title, 160)
        _require_text(self.intent, 1_000)
        _require_optional_branch_type(self.branch_type)
        if self.parent_branch_id is not None:
            _require_uuid(self.parent_branch_id)
        if self.status not in _BRANCH_STATUSES:
            raise RecordContractError()
        _require_optional_text(self.retrieval_summary, 1_000)
        if not _utc_datetime(self.created_at) or not _utc_datetime(self.updated_at) or self.created_at > self.updated_at:
            raise RecordContractError()
        _require_revision(self.branch_revision)


@dataclass(frozen=True, slots=True)
class RecordPage:
    records: tuple[RecordValue, ...]
    next_cursor: PageCursor | None

    def __post_init__(self) -> None:
        if (
            type(self.records) is not tuple
            or len(self.records) > MAX_PAGE_LIMIT
            or any(type(value) is not RecordValue for value in self.records)
            or (self.next_cursor is not None and type(self.next_cursor) is not PageCursor)
        ):
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class HistoryPage:
    events: tuple[RecordHistoryEvent, ...]
    next_cursor: PageCursor | None

    def __post_init__(self) -> None:
        if (
            type(self.events) is not tuple
            or len(self.events) > MAX_PAGE_LIMIT
            or any(type(value) is not RecordHistoryEvent for value in self.events)
            or (self.next_cursor is not None and type(self.next_cursor) is not PageCursor)
        ):
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class BranchPage:
    branches: tuple[BranchValue, ...]
    next_cursor: PageCursor | None

    def __post_init__(self) -> None:
        if (
            type(self.branches) is not tuple
            or len(self.branches) > MAX_PAGE_LIMIT
            or any(type(value) is not BranchValue for value in self.branches)
            or (self.next_cursor is not None and type(self.next_cursor) is not PageCursor)
        ):
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class FacetValue:
    value: object
    count: int

    def __post_init__(self) -> None:
        if (
            not _is_scalar_json(self.value)
            or type(self.count) is not int
            or isinstance(self.count, bool)
            or self.count < 0
        ):
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class FacetObservation:
    entity_name: str
    field_name: str
    values: tuple[FacetValue, ...]

    def __post_init__(self) -> None:
        _require_entity(self.entity_name)
        _require_entity(self.field_name)
        if (
            type(self.values) is not tuple
            or len(self.values) > MAX_FACET_VALUES
            or any(type(value) is not FacetValue for value in self.values)
        ):
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class BranchCard:
    branch: BranchValue
    primary_record_count: int
    related_record_count: int

    def __post_init__(self) -> None:
        if type(self.branch) is not BranchValue or any(
            type(value) is not int or isinstance(value, bool) or value < 0
            for value in (self.primary_record_count, self.related_record_count)
        ):
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class ServiceSummary:
    data_status: Literal["ready", "migration_required"]
    record_counts: tuple[tuple[str, int], ...]
    unbranched_record_count: int
    facets: tuple[FacetObservation, ...]
    branches: tuple[BranchCard, ...]
    samples: tuple[tuple[str, tuple[RecordValue, ...]], ...]

    def __post_init__(self) -> None:
        if self.data_status not in {"ready", "migration_required"}:
            raise RecordContractError()
        if (
            type(self.unbranched_record_count) is not int
            or isinstance(self.unbranched_record_count, bool)
            or self.unbranched_record_count < 0
        ):
            raise RecordContractError()
        if (
            type(self.record_counts) is not tuple
            or type(self.facets) is not tuple
            or type(self.branches) is not tuple
            or type(self.samples) is not tuple
            or len(self.branches) > MAX_SUMMARY_BRANCHES
            or any(type(value) is not FacetObservation for value in self.facets)
            or any(type(value) is not BranchCard for value in self.branches)
        ):
            raise RecordContractError()
        names = []
        for entry in self.record_counts:
            if type(entry) is not tuple or len(entry) != 2:
                raise RecordContractError()
            name, count = entry
            _require_entity(name)
            if type(count) is not int or isinstance(count, bool) or count < 0:
                raise RecordContractError()
            names.append(name)
        if names != sorted(names) or len(names) != len(set(names)):
            raise RecordContractError()
        sample_names = []
        for entry in self.samples:
            if type(entry) is not tuple or len(entry) != 2:
                raise RecordContractError()
            name, records = entry
            _require_entity(name)
            if (
                type(records) is not tuple
                or len(records) > MAX_SAMPLES_PER_ENTITY
                or any(type(record) is not RecordValue or record.entity_name != name for record in records)
            ):
                raise RecordContractError()
            sample_names.append(name)
        if sample_names != sorted(sample_names) or len(sample_names) != len(set(sample_names)):
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class MaintenanceCandidate:
    scan_type: str
    entity_name: str
    record_id: UUID
    detail: FrozenJsonObject

    def __post_init__(self) -> None:
        if self.scan_type not in _MAINTENANCE_SCAN_TYPES:
            raise RecordContractError()
        _require_entity(self.entity_name)
        _require_uuid(self.record_id)
        if type(self.detail) is not FrozenJsonObject:
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    candidates: tuple[MaintenanceCandidate, ...]
    unavailable_scan_types: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.candidates) is not tuple
            or len(self.candidates) > MAX_MAINTENANCE_CANDIDATES
            or any(type(value) is not MaintenanceCandidate for value in self.candidates)
            or type(self.unavailable_scan_types) is not tuple
            or any(value not in _MAINTENANCE_SCAN_TYPES for value in self.unavailable_scan_types)
            or tuple(sorted(self.unavailable_scan_types)) != self.unavailable_scan_types
            or len(set(self.unavailable_scan_types)) != len(self.unavailable_scan_types)
        ):
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class DataFailure:
    code: DataFailureCode

    def __post_init__(self) -> None:
        if type(self.code) is not str or self.code not in _FAILURE_CODES:
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class RecordMutationOutcome:
    record: RecordValue | None
    replayed: bool

    def __post_init__(self) -> None:
        if (self.record is not None and type(self.record) is not RecordValue) or type(self.replayed) is not bool:
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class DeleteRecordOutcome:
    deleted_record_id: UUID
    deleted_record_version: int
    replayed: bool

    def __post_init__(self) -> None:
        _require_uuid(self.deleted_record_id)
        _require_revision(self.deleted_record_version)
        if type(self.replayed) is not bool:
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class BranchMutationOutcome:
    branch: BranchValue
    replayed: bool

    def __post_init__(self) -> None:
        if type(self.branch) is not BranchValue or type(self.replayed) is not bool:
            raise RecordContractError()


@dataclass(frozen=True, slots=True)
class AssignedRecordRef:
    entity_name: str
    record_id: UUID
    record_version: int

    def __post_init__(self) -> None:
        _require_entity(self.entity_name)
        _require_uuid(self.record_id)
        _require_revision(self.record_version)


@dataclass(frozen=True, slots=True)
class BranchAssignmentOutcome:
    records: tuple[AssignedRecordRef, ...]
    replayed: bool

    def __post_init__(self) -> None:
        if (
            type(self.records) is not tuple
            or not self.records
            or len(self.records) > MAX_ASSIGNMENT_TARGETS
            or any(type(value) is not AssignedRecordRef for value in self.records)
            or type(self.replayed) is not bool
        ):
            raise RecordContractError()


type RecordReadOutcome = RecordValue | DataFailure
type RecordPageOutcome = RecordPage | DataFailure
type HistoryOutcome = HistoryPage | DataFailure
type BranchPageOutcome = BranchPage | DataFailure
type SummaryOutcome = ServiceSummary | DataFailure
type MaintenanceOutcome = MaintenanceResult | DataFailure
type RecordMutationResult = RecordMutationOutcome | DataFailure
type DeleteRecordResult = DeleteRecordOutcome | DataFailure
type BranchMutationResult = BranchMutationOutcome | DataFailure
type BranchAssignmentResult = BranchAssignmentOutcome | DataFailure


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return FrozenJsonObject.from_mapping(value)
    if type(value) is list or type(value) is tuple:
        return tuple(_freeze_json(item) for item in value)
    _validate_json(value)
    return value


def _thaw_json(value: object) -> object:
    if type(value) is FrozenJsonObject:
        return value.to_dict()
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


def _json_value(value: object) -> object:
    if type(value) is FrozenJsonObject:
        return {name: _json_value(item) for name, item in value.entries}
    if type(value) is tuple:
        return [_json_value(item) for item in value]
    return value


def _json_size(value: object) -> int:
    try:
        return len(json.dumps(_json_value(value), separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as error:
        raise RecordContractError() from error


def _validate_json(value: object) -> None:
    if type(value) is FrozenJsonObject:
        return
    if type(value) is tuple or type(value) is list:
        for item in value:
            _validate_json(item)
        return
    if isinstance(value, Mapping):
        FrozenJsonObject.from_mapping(value)
        return
    if not _is_scalar_json(value):
        raise RecordContractError()
    if type(value) is str and len(value.encode("utf-8")) > MAX_STRING_BYTES:
        raise RecordContractError()


def _is_scalar_json(value: object) -> bool:
    return (
        value is None
        or type(value) in {str, bool}
        or is_signed_64_integer(value)
        or is_finite_real(value)
    )


def _matches_type(value: object, logical_type: LogicalType) -> bool:
    if logical_type == "string":
        return type(value) is str and len(value.encode("utf-8")) <= MAX_STRING_BYTES and "\x00" not in value
    if logical_type == "integer":
        return is_signed_64_integer(value)
    if logical_type == "number":
        return is_exact_number(value)
    if logical_type == "boolean":
        return type(value) is bool
    if logical_type == "uuid":
        return type(value) is str and _canonical_uuid(value)
    if logical_type == "datetime":
        return type(value) is str and _canonical_datetime(value)
    if logical_type == "string_list":
        return (
            type(value) in {list, tuple}
            and len(value) <= MAX_STRING_LIST_ITEMS
            and all(
                type(item) is str
                and 0 < len(item) <= MAX_STRING_LIST_ITEM_CHARS
                and "\x00" not in item
                for item in value
            )
            and len(set(value)) == len(value)
        )
    return False


def _matches_allowed_value(value: object, logical_type: LogicalType) -> bool:
    if logical_type == "string_list":
        return (
            type(value) is str
            and 0 < len(value) <= MAX_STRING_LIST_ITEM_CHARS
            and "\x00" not in value
        )
    return _matches_type(value, logical_type)


def _matches_enumeration(
    value: object,
    logical_type: LogicalType,
    allowed_values: tuple[str | int | float | bool, ...],
) -> bool:
    if logical_type == "string_list":
        return type(value) in {list, tuple} and all(
            item in allowed_values for item in value
        )
    return value in allowed_values


def _canonical_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value and UUID(value).int != 0
    except ValueError:
        return False


def _canonical_datetime(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return _utc_datetime(parsed)


def _identifier(value: object) -> bool:
    return type(value) is str and bool(_IDENTIFIER.fullmatch(value))


def _require_entity(value: object) -> None:
    if not _identifier(value):
        raise RecordContractError()


def _require_uuid(value: object) -> None:
    if type(value) is not UUID or value.int == 0:
        raise RecordContractError()


def _require_revision(value: object) -> None:
    if type(value) is not int or isinstance(value, bool) or value < 1:
        raise RecordContractError()


def _require_idempotency_key(value: object) -> None:
    if (
        type(value) is not str
        or not 1 <= len(value) <= MAX_IDEMPOTENCY_KEY_LENGTH
        or "\x00" in value
        or value.startswith("__aipcs_")
    ):
        raise RecordContractError()


def _require_text(value: object, maximum: int) -> None:
    if type(value) is not str or not value or len(value) > maximum or "\x00" in value:
        raise RecordContractError()


def _require_optional_text(value: object, maximum: int) -> None:
    if value is not None:
        _require_text(value, maximum)


def _require_slug(value: object) -> None:
    if type(value) is not str or not re.fullmatch(r"[a-z][a-z0-9-]{1,62}[a-z0-9]", value):
        raise RecordContractError()


def _require_optional_branch_type(value: object) -> None:
    if value is not None and (
        type(value) is not str or not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", value)
    ):
        raise RecordContractError()


def _utc_datetime(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is UTC and value.utcoffset() == timedelta(0)
