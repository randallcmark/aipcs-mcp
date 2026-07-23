"""Strict public transport requests for the Milestone A data runtime.

These models deliberately validate only transport-owned invariants.  The
materialised service specification validates entity fields, value types,
relationships, and filterability after registry admission.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, StrictInt, ValidationError, field_validator, model_validator

from .contracts import PublicModel
from .errors import AipcsContractError, ErrorCode, error_from_validation
from .manifest_v2 import IDENTIFIER_PATTERN, SERVER_MANAGED_FIELDS
from .numeric_domain import is_finite_real, is_signed_64_integer

MAX_RECORD_JSON_BYTES = 64 * 1024
MAX_STRING_BYTES = 16 * 1024
MAX_STRING_LIST_ITEMS = 256
MAX_STRING_LIST_ITEM_CHARACTERS = 256
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100
MAX_CURSOR_LENGTH = 1024
MAX_SEARCH_FILTERS = 16
MAX_BRANCH_ASSIGNMENTS = 100
MAX_BOOTSTRAP_SERVICES = 100
MAX_SUMMARY_SAMPLE = 3
_BRANCH_SLUG_PATTERN = r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$"
_BRANCH_TYPE_PATTERN = r"^[a-z][a-z0-9_-]{0,79}$"
_ZERO_UUID = UUID(int=0)
_IDENTIFIER = re.compile(IDENTIFIER_PATTERN)

BranchStatus = Literal["active", "archived", "superseded"]
BranchRole = Literal["primary", "related"]
MaintenanceScanType = Literal[
    "expired",
    "stale",
    "low_confidence",
    "superseded",
    "missing_authority",
    "unbranched",
    "duplicate_authority",
    "blob_candidate",
]


class DataRequest(PublicModel):
    """A strict, detached JSON request used only at the public transport seam."""


class _ServiceEntityRequest(DataRequest):
    service_id: UUID
    entity_name: str = Field(pattern=IDENTIFIER_PATTERN)

    @field_validator("service_id", mode="before")
    @classmethod
    def require_canonical_service_id(cls, value: object) -> object:
        _require_canonical_uuid(value, field="service_id")
        return value


class _PageRequest(DataRequest):
    limit: StrictInt = Field(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT)
    cursor: str | None = Field(default=None, min_length=1, max_length=MAX_CURSOR_LENGTH)


class _BranchScopeRequest(_PageRequest):
    branch_id: UUID | None = None
    branch_role: BranchRole | None = None

    @field_validator("branch_id", mode="before")
    @classmethod
    def require_canonical_branch_id(cls, value: object) -> object:
        if value is not None:
            _require_canonical_uuid(value, field="branch_id")
        return value

    @model_validator(mode="after")
    def require_complete_branch_scope(self) -> _BranchScopeRequest:
        if (self.branch_id is None) != (self.branch_role is None):
            raise ValueError("branch_id and branch_role must be supplied together.")
        return self


class _IdempotentRequest(DataRequest):
    idempotency_key: str = Field(min_length=1, max_length=128)


class RecordCreateRequest(_ServiceEntityRequest, _IdempotentRequest):
    record: dict[str, Any]

    @field_validator("record")
    @classmethod
    def require_domain_only_record(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_domain_mapping(value, field="record", allow_empty=True)
        return value


class RecordGetRequest(_ServiceEntityRequest):
    record_id: UUID

    @field_validator("record_id", mode="before")
    @classmethod
    def require_canonical_record_id(cls, value: object) -> object:
        _require_canonical_uuid(value, field="record_id")
        return value


class RecordListRequest(_ServiceEntityRequest, _BranchScopeRequest):
    pass


class RecordSearchRequest(_ServiceEntityRequest, _BranchScopeRequest):
    filters: dict[str, Any] = Field(min_length=1, max_length=MAX_SEARCH_FILTERS)

    @field_validator("filters")
    @classmethod
    def require_structured_filters(cls, value: dict[str, Any]) -> dict[str, Any]:
        for name, item in value.items():
            if not _IDENTIFIER.fullmatch(name):
                raise ValueError("Filter names must be declared identifiers.")
            _validate_filter_value(item)
        return value


class RecordUpdateRequest(_ServiceEntityRequest, _IdempotentRequest):
    record_id: UUID
    updates: dict[str, Any] = Field(min_length=1)
    expected_record_version: StrictInt = Field(ge=1, le=2**63 - 1)

    @field_validator("record_id", mode="before")
    @classmethod
    def require_canonical_record_id(cls, value: object) -> object:
        _require_canonical_uuid(value, field="record_id")
        return value

    @field_validator("updates")
    @classmethod
    def require_domain_only_updates(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_domain_mapping(value, field="updates", allow_empty=False)
        return value


class RecordDeleteRequest(_ServiceEntityRequest, _IdempotentRequest):
    record_id: UUID
    expected_record_version: StrictInt = Field(ge=1, le=2**63 - 1)

    @field_validator("record_id", mode="before")
    @classmethod
    def require_canonical_record_id(cls, value: object) -> object:
        _require_canonical_uuid(value, field="record_id")
        return value


class RecordHistoryRequest(_ServiceEntityRequest, _PageRequest):
    record_id: UUID | None = None

    @field_validator("record_id", mode="before")
    @classmethod
    def require_canonical_record_id(cls, value: object) -> object:
        if value is not None:
            _require_canonical_uuid(value, field="record_id")
        return value


class BranchCreateRequest(_IdempotentRequest):
    service_id: UUID
    slug: str = Field(pattern=_BRANCH_SLUG_PATTERN, min_length=3, max_length=64)
    title: str = Field(min_length=1, max_length=160)
    intent: str = Field(min_length=1, max_length=1_000)
    branch_type: str | None = Field(default=None, pattern=_BRANCH_TYPE_PATTERN, max_length=80)
    parent_branch_id: UUID | None = None
    retrieval_summary: str | None = Field(default=None, max_length=1_000)

    @field_validator("service_id", "parent_branch_id", mode="before")
    @classmethod
    def require_canonical_uuid_fields(cls, value: object, info: Any) -> object:
        if value is not None:
            _require_canonical_uuid(value, field=info.field_name)
        return value


class BranchListRequest(_PageRequest):
    service_id: UUID
    status: BranchStatus | None = None
    branch_type: str | None = Field(default=None, pattern=_BRANCH_TYPE_PATTERN, max_length=80)
    parent_branch_id: UUID | None = None

    @field_validator("service_id", "parent_branch_id", mode="before")
    @classmethod
    def require_canonical_uuid_fields(cls, value: object, info: Any) -> object:
        if value is not None:
            _require_canonical_uuid(value, field=info.field_name)
        return value


class BranchUpdates(DataRequest):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    intent: str | None = Field(default=None, min_length=1, max_length=1_000)
    branch_type: str | None = Field(default=None, pattern=_BRANCH_TYPE_PATTERN, max_length=80)
    parent_branch_id: UUID | None = None
    status: BranchStatus | None = None
    retrieval_summary: str | None = Field(default=None, max_length=1_000)

    @field_validator("parent_branch_id", mode="before")
    @classmethod
    def require_canonical_parent_id(cls, value: object) -> object:
        if value is not None:
            _require_canonical_uuid(value, field="parent_branch_id")
        return value

    @field_validator("title", "intent", "status", mode="before")
    @classmethod
    def reject_null_for_non_nullable_updates(cls, value: object) -> object:
        if value is None:
            raise ValueError("This branch update field cannot be null.")
        return value

    @model_validator(mode="after")
    def require_at_least_one_update(self) -> BranchUpdates:
        if not self.model_fields_set:
            raise ValueError("updates must contain at least one supported field.")
        return self


class BranchUpdateRequest(_IdempotentRequest):
    service_id: UUID
    branch_id: UUID
    expected_branch_revision: StrictInt = Field(ge=1, le=2**63 - 1)
    updates: BranchUpdates

    @field_validator("service_id", "branch_id", mode="before")
    @classmethod
    def require_canonical_uuid_fields(cls, value: object, info: Any) -> object:
        _require_canonical_uuid(value, field=info.field_name)
        return value


class BranchAssignmentTarget(DataRequest):
    entity_name: str = Field(pattern=IDENTIFIER_PATTERN)
    record_id: UUID
    expected_record_version: StrictInt = Field(ge=1, le=2**63 - 1)

    @field_validator("record_id", mode="before")
    @classmethod
    def require_canonical_record_id(cls, value: object) -> object:
        _require_canonical_uuid(value, field="record_id")
        return value


class BranchAssignRecordsRequest(_IdempotentRequest):
    service_id: UUID
    branch_id: UUID
    role: BranchRole
    operation: Literal["assign", "unassign"]
    records: list[BranchAssignmentTarget] = Field(min_length=1, max_length=MAX_BRANCH_ASSIGNMENTS)

    @field_validator("service_id", "branch_id", mode="before")
    @classmethod
    def require_canonical_uuid_fields(cls, value: object, info: Any) -> object:
        _require_canonical_uuid(value, field=info.field_name)
        return value

    @field_validator("records")
    @classmethod
    def require_distinct_targets(
        cls, value: list[BranchAssignmentTarget]
    ) -> list[BranchAssignmentTarget]:
        keys = {(target.entity_name, target.record_id) for target in value}
        if len(keys) != len(value):
            raise ValueError("records must not contain duplicate entity and record targets.")
        return value


class BootstrapRequest(DataRequest):
    limit: StrictInt = Field(default=MAX_BOOTSTRAP_SERVICES, ge=1, le=MAX_BOOTSTRAP_SERVICES)


class ServiceSummaryRequest(DataRequest):
    service_id: UUID
    sample: StrictInt = Field(default=0, ge=0, le=MAX_SUMMARY_SAMPLE)

    @field_validator("service_id", mode="before")
    @classmethod
    def require_canonical_service_id(cls, value: object) -> object:
        _require_canonical_uuid(value, field="service_id")
        return value


class MaintenanceScanRequest(DataRequest):
    service_id: UUID
    entity_name: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    branch_id: UUID | None = None
    scan_types: list[MaintenanceScanType] | None = Field(default=None, min_length=1, max_length=8)
    stale_after_days: StrictInt = Field(default=90, ge=1, le=3_650)
    low_confidence_below: float = Field(default=0.5, strict=True, ge=0, le=1, allow_inf_nan=False)
    limit: StrictInt = Field(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT)

    @field_validator("service_id", "branch_id", mode="before")
    @classmethod
    def require_canonical_uuid_fields(cls, value: object, info: Any) -> object:
        if value is not None:
            _require_canonical_uuid(value, field=info.field_name)
        return value

    @field_validator("scan_types")
    @classmethod
    def require_distinct_scan_types(
        cls, value: list[MaintenanceScanType] | None
    ) -> list[MaintenanceScanType] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("scan_types must not contain duplicates.")
        return value


def parse_record_create(payload: object) -> RecordCreateRequest:
    return _parse(RecordCreateRequest, payload)


def parse_record_get(payload: object) -> RecordGetRequest:
    return _parse(RecordGetRequest, payload)


def parse_record_list(payload: object) -> RecordListRequest:
    return _parse(RecordListRequest, payload)


def parse_record_search(payload: object) -> RecordSearchRequest:
    return _parse(RecordSearchRequest, payload)


def parse_record_update(payload: object) -> RecordUpdateRequest:
    return _parse(RecordUpdateRequest, payload)


def parse_record_delete(payload: object) -> RecordDeleteRequest:
    return _parse(RecordDeleteRequest, payload)


def parse_record_history(payload: object) -> RecordHistoryRequest:
    return _parse(RecordHistoryRequest, payload)


def parse_branch_create(payload: object) -> BranchCreateRequest:
    return _parse(BranchCreateRequest, payload)


def parse_branch_list(payload: object) -> BranchListRequest:
    return _parse(BranchListRequest, payload)


def parse_branch_update(payload: object) -> BranchUpdateRequest:
    return _parse(BranchUpdateRequest, payload)


def parse_branch_assign_records(payload: object) -> BranchAssignRecordsRequest:
    return _parse(BranchAssignRecordsRequest, payload)


def parse_bootstrap(payload: object) -> BootstrapRequest:
    return _parse(BootstrapRequest, payload)


def parse_service_summary(payload: object) -> ServiceSummaryRequest:
    return _parse(ServiceSummaryRequest, payload)


def parse_maintenance_scan(payload: object) -> MaintenanceScanRequest:
    return _parse(MaintenanceScanRequest, payload)


def _parse[T: DataRequest](model: type[T], payload: object) -> T:
    if not isinstance(payload, Mapping):
        raise AipcsContractError(ErrorCode.VALIDATION_FAILED, "Request input must be a JSON object.")
    try:
        return model.model_validate(payload).model_copy(deep=True)
    except ValidationError as error:
        raise error_from_validation(error) from None


def _require_canonical_uuid(value: object, *, field: str) -> None:
    try:
        canonical = str(UUID(value)) if isinstance(value, str) else None
    except ValueError:
        canonical = None
    if canonical != value or canonical == str(_ZERO_UUID):
        raise ValueError(f"{field} must be a non-zero lowercase canonical UUID.")


def _validate_domain_mapping(value: dict[str, Any], *, field: str, allow_empty: bool) -> None:
    if not allow_empty and not value:
        raise ValueError(f"{field} must contain at least one domain field.")
    for name, item in value.items():
        if not _IDENTIFIER.fullmatch(name):
            raise ValueError("Domain field names must be declared identifiers.")
        if name in SERVER_MANAGED_FIELDS:
            raise ValueError("Record inputs cannot set server-managed fields.")
        _validate_json_value(item)
    _validate_canonical_json_size(value)


def _validate_filter_value(value: object) -> None:
    if value is None or isinstance(value, (list, tuple, dict)):
        raise ValueError("Filters must contain one typed scalar or one string-list member.")
    _validate_json_value(value)


def _validate_json_value(value: object) -> None:
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if not is_signed_64_integer(value):
            raise ValueError("Integers must be in the signed-64 range.")
        return
    if is_finite_real(value):
        return
    if type(value) is float:
        raise ValueError("Numbers must be finite.")
    if type(value) is str:
        if "\x00" in value:
            raise ValueError("Strings must not contain NUL characters.")
        if len(value.encode("utf-8")) > MAX_STRING_BYTES:
            raise ValueError("One string may not exceed 16 KiB.")
        return
    if isinstance(value, list):
        if len(value) > MAX_STRING_LIST_ITEMS:
            raise ValueError("A string_list may contain at most 256 values.")
        if any(type(item) is not str for item in value):
            raise ValueError("A string_list must contain only strings.")
        if any(not item or len(item) > MAX_STRING_LIST_ITEM_CHARACTERS for item in value):
            raise ValueError("A string_list member may contain at most 256 characters.")
        if len(value) != len(set(value)):
            raise ValueError("A string_list must not contain duplicates.")
        for item in value:
            _validate_json_value(item)
        return
    raise ValueError("Record values must be JSON scalars or string_lists.")


def _validate_canonical_json_size(value: dict[str, Any]) -> None:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("Record values must be JSON serializable.") from None
    if len(encoded) > MAX_RECORD_JSON_BYTES:
        raise ValueError("One record may not exceed 64 KiB of canonical JSON.")
