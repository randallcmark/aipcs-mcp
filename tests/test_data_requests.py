"""Public Milestone A request contracts are strict before application admission."""

from __future__ import annotations

import pytest

from aipcs_mcp.data_requests import (
    MAX_BRANCH_ASSIGNMENTS,
    BootstrapRequest,
    BranchAssignRecordsRequest,
    BranchCreateRequest,
    BranchListRequest,
    BranchUpdateRequest,
    MaintenanceScanRequest,
    RecordCreateRequest,
    RecordHistoryRequest,
    RecordListRequest,
    RecordSearchRequest,
    RecordUpdateRequest,
    ServiceSummaryRequest,
    parse_bootstrap,
    parse_branch_assign_records,
    parse_branch_create,
    parse_branch_update,
    parse_maintenance_scan,
    parse_record_create,
    parse_record_list,
    parse_record_search,
    parse_record_update,
    parse_service_summary,
)
from aipcs_mcp.errors import DATA_ERROR_CODES, AipcsContractError, ErrorCode
from aipcs_mcp.numeric_domain import SIGNED_64_MAX, SIGNED_64_MIN

SERVICE_ID = "5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23"
RECORD_ID = "aa5d0b6a-976c-4e32-b4d2-cc0a64e3ee23"
BRANCH_ID = "bb5d0b6a-976c-4e32-b4d2-cc0a64e3ee23"


def test_record_request_schemas_are_flat_and_require_frozen_inputs() -> None:
    create = parse_record_create(
        {
            "service_id": SERVICE_ID,
            "entity_name": "note",
            "record": {"title": "A note", "tags": ["a", "b"]},
            "idempotency_key": "create-note-1",
        }
    )
    assert isinstance(create, RecordCreateRequest)
    assert create.model_dump(mode="json") == {
        "service_id": SERVICE_ID,
        "entity_name": "note",
        "record": {"title": "A note", "tags": ["a", "b"]},
        "idempotency_key": "create-note-1",
    }
    assert RecordUpdateRequest.model_json_schema()["additionalProperties"] is False
    cursor_schema = RecordHistoryRequest.model_json_schema()["properties"]["cursor"]
    assert cursor_schema["anyOf"][0]["maxLength"] == 1024


@pytest.mark.parametrize("field", ["id", "owner_id", "created_at", "updated_at", "created_via", "record_version"])
def test_record_create_rejects_every_server_managed_field(field: str) -> None:
    with pytest.raises(AipcsContractError) as raised:
        parse_record_create(
            {
                "service_id": SERVICE_ID,
                "entity_name": "note",
                "record": {field: "caller-controlled"},
                "idempotency_key": "create-note-1",
            }
        )
    assert raised.value.code is ErrorCode.VALIDATION_FAILED


@pytest.mark.parametrize(
    "payload",
    [
        {"service_id": SERVICE_ID.upper(), "entity_name": "note", "record": {}, "idempotency_key": "x"},
        {"service_id": SERVICE_ID, "entity_name": "note", "record": {}, "idempotency_key": "x", "principal_id": "no"},
        {"service_id": SERVICE_ID, "entity_name": "note", "record": {"x": "a" * (16 * 1024 + 1)}, "idempotency_key": "x"},
        {"service_id": SERVICE_ID, "entity_name": "note", "record": {"x": "has\x00nul"}, "idempotency_key": "x"},
        {"service_id": SERVICE_ID, "entity_name": "note", "record": {"tags": ["same", "same"]}, "idempotency_key": "x"},
        {"service_id": SERVICE_ID, "entity_name": "note", "record": {"tags": [""]}, "idempotency_key": "x"},
    ],
)
def test_record_create_fails_closed_for_hostile_transport_input(payload: dict[str, object]) -> None:
    with pytest.raises(AipcsContractError) as raised:
        parse_record_create(payload)
    assert raised.value.code is ErrorCode.VALIDATION_FAILED


def test_record_transport_accepts_only_storage_safe_json_numbers() -> None:
    accepted = parse_record_create(
        {
            "service_id": SERVICE_ID,
            "entity_name": "note",
            "record": {
                "minimum": SIGNED_64_MIN,
                "maximum": SIGNED_64_MAX,
                "real": float.fromhex("0x1.fffffffffffffp+1023"),
            },
            "idempotency_key": "numeric-boundaries",
        }
    )
    assert accepted.record["minimum"] == SIGNED_64_MIN
    assert accepted.record["maximum"] == SIGNED_64_MAX

    for invalid in (
        {"record": {"count": SIGNED_64_MIN - 1}},
        {"record": {"count": SIGNED_64_MAX + 1}},
        {"record": {"score": float("inf")}},
        {"record": {"score": float("-inf")}},
        {"record": {"score": float("nan")}},
        {"updates": {"count": SIGNED_64_MAX + 1}},
        {"filters": {"count": SIGNED_64_MAX + 1}},
    ):
        payload = {
            "service_id": SERVICE_ID,
            "entity_name": "note",
            "idempotency_key": "invalid-number",
            **invalid,
        }
        if "filters" in invalid:
            parser = parse_record_search
        elif "updates" in invalid:
            payload.update(
                {
                    "record_id": RECORD_ID,
                    "expected_record_version": 1,
                }
            )
            parser = parse_record_update
        else:
            parser = parse_record_create
        with pytest.raises(AipcsContractError) as raised:
            parser(payload)
        assert raised.value.code is ErrorCode.VALIDATION_FAILED


def test_cursor_pagination_has_no_offset_and_search_is_bounded_and_typed() -> None:
    request = parse_record_search(
        {
            "service_id": SERVICE_ID,
            "entity_name": "note",
            "branch_id": BRANCH_ID,
            "branch_role": "related",
            "filters": {"status": "active", "priority": 2},
            "cursor": "opaque-query-bound-cursor",
        }
    )
    assert isinstance(request, RecordSearchRequest)
    assert request.limit == 50
    assert request.branch_role == "related"
    for invalid in (
        {"service_id": SERVICE_ID, "entity_name": "note", "filters": {"status": "active"}, "offset": 0},
        {"service_id": SERVICE_ID, "entity_name": "note", "filters": {"status": ["active"]}},
        {"service_id": SERVICE_ID, "entity_name": "note", "filters": {}},
        {"service_id": SERVICE_ID, "entity_name": "note", "branch_role": "primary"},
        {"service_id": SERVICE_ID, "entity_name": "note", "branch_id": BRANCH_ID},
    ):
        with pytest.raises(AipcsContractError) as raised:
            parse_record_search(invalid)
        assert raised.value.code is ErrorCode.VALIDATION_FAILED

    listed = parse_record_list({"service_id": SERVICE_ID, "entity_name": "note", "limit": 100})
    assert isinstance(listed, RecordListRequest)
    assert listed.limit == 100


def test_branch_requests_freeze_ids_patch_and_bulk_assignment_shape() -> None:
    create = parse_branch_create(
        {
            "service_id": SERVICE_ID,
            "slug": "release-notes",
            "title": "Release notes",
            "intent": "Organise release notes.",
            "branch_type": "release_notes",
            "parent_branch_id": BRANCH_ID,
            "retrieval_summary": "Release-focused retrieval.",
            "idempotency_key": "branch-create-1",
        }
    )
    assert isinstance(create, BranchCreateRequest)
    assert BranchListRequest.model_json_schema()["properties"]["limit"]["default"] == 50

    update = parse_branch_update(
        {
            "service_id": SERVICE_ID,
            "branch_id": BRANCH_ID,
            "expected_branch_revision": 3,
            "updates": {"parent_branch_id": None, "retrieval_summary": None},
            "idempotency_key": "branch-update-1",
        }
    )
    assert isinstance(update, BranchUpdateRequest)
    assert update.updates.model_fields_set == {"parent_branch_id", "retrieval_summary"}

    assignment = parse_branch_assign_records(
        {
            "service_id": SERVICE_ID,
            "branch_id": BRANCH_ID,
            "role": "primary",
            "operation": "assign",
            "records": [
                {"entity_name": "note", "record_id": RECORD_ID, "expected_record_version": 2}
            ],
            "idempotency_key": "branch-assign-1",
        }
    )
    assert isinstance(assignment, BranchAssignRecordsRequest)
    assert len(assignment.records) == 1

    for payload in (
        {"service_id": SERVICE_ID, "slug": "No", "title": "t", "intent": "i", "idempotency_key": "x"},
        {"service_id": SERVICE_ID, "branch_id": BRANCH_ID, "expected_branch_revision": 1, "updates": {}, "idempotency_key": "x"},
        {
            "service_id": SERVICE_ID,
            "branch_id": BRANCH_ID,
            "expected_branch_revision": 1,
            "updates": {"title": None},
            "idempotency_key": "x",
        },
        {
            "service_id": SERVICE_ID,
            "branch_id": BRANCH_ID,
            "role": "primary",
            "operation": "assign",
            "records": [],
            "idempotency_key": "x",
        },
    ):
        parser = parse_branch_create if "slug" in payload else parse_branch_update
        if "records" in payload:
            parser = parse_branch_assign_records
        with pytest.raises(AipcsContractError) as raised:
            parser(payload)
        assert raised.value.code is ErrorCode.VALIDATION_FAILED
    assert MAX_BRANCH_ASSIGNMENTS == 100


def test_discovery_and_maintenance_requests_enforce_public_caps() -> None:
    bootstrap = parse_bootstrap({})
    assert isinstance(bootstrap, BootstrapRequest)
    assert bootstrap.limit == 100

    summary = parse_service_summary({"service_id": SERVICE_ID, "sample": 3})
    assert isinstance(summary, ServiceSummaryRequest)
    assert summary.sample == 3
    for payload in (
        {"service_id": SERVICE_ID, "sample": "all"},
        {"service_id": SERVICE_ID, "sample": 4},
    ):
        with pytest.raises(AipcsContractError) as raised:
            parse_service_summary(payload)
        assert raised.value.code is ErrorCode.VALIDATION_FAILED

    scan = parse_maintenance_scan(
        {
            "service_id": SERVICE_ID,
            "entity_name": "note",
            "branch_id": BRANCH_ID,
            "scan_types": ["stale", "low_confidence"],
            "stale_after_days": 90,
            "low_confidence_below": 0.5,
            "limit": 100,
        }
    )
    assert isinstance(scan, MaintenanceScanRequest)
    assert scan.limit == 100
    assert "cursor" not in MaintenanceScanRequest.model_json_schema()["properties"]
    for payload in (
        {"service_id": SERVICE_ID, "scan_types": ["stale", "stale"]},
        {"service_id": SERVICE_ID, "stale_after_days": 0},
        {"service_id": SERVICE_ID, "low_confidence_below": 1.1},
        {"service_id": SERVICE_ID, "limit": 101},
    ):
        with pytest.raises(AipcsContractError) as raised:
            parse_maintenance_scan(payload)
        assert raised.value.code is ErrorCode.VALIDATION_FAILED


def test_branch_assignment_rejects_duplicate_record_targets() -> None:
    payload = {
        "service_id": SERVICE_ID,
        "branch_id": BRANCH_ID,
        "role": "related",
        "operation": "assign",
        "records": [
            {"entity_name": "note", "record_id": RECORD_ID, "expected_record_version": 1},
            {"entity_name": "note", "record_id": RECORD_ID, "expected_record_version": 2},
        ],
        "idempotency_key": "branch-assign-duplicates",
    }
    with pytest.raises(AipcsContractError) as raised:
        parse_branch_assign_records(payload)
    assert raised.value.code is ErrorCode.VALIDATION_FAILED


def test_data_error_vocabulary_is_the_frozen_closed_set() -> None:
    assert {code.value for code in DATA_ERROR_CODES} == {
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
        "internal_error",
    }
