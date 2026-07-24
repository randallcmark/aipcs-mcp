"""Pure contract tests for backend-neutral portable service-store values."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from aipcs_mcp.application.portable_store import (
    BranchMembership,
    FenceTransitionResult,
    PortableStoreContractError,
    PortableStoreMember,
    StageResult,
    WriteAdmissionFence,
    portable_member_sort_key,
)
from aipcs_mcp.records import (
    BranchValue,
    CreateRecordCommand,
    FrozenJsonObject,
    RecordContractError,
    RecordHistoryEvent,
    RecordValue,
)

AT = datetime(2026, 7, 24, tzinfo=UTC)
RECORD_ID = UUID("00000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("00000000-0000-4000-8000-000000000002")


def test_fence_and_result_values_are_exact_and_bounded() -> None:
    open_fence = WriteAdmissionFence(1, "open")
    closed_fence = WriteAdmissionFence(2, "closed")
    assert StageResult("staged", closed_fence).fence == closed_fence
    assert FenceTransitionResult("applied", open_fence).fence == open_fence
    for value in ((0, "open"), (1, "other"), (True, "open")):
        with pytest.raises(PortableStoreContractError):
            WriteAdmissionFence(*value)  # type: ignore[arg-type]


def test_membership_excludes_physical_assignment_evidence() -> None:
    value = BranchMembership(BRANCH_ID, "note", RECORD_ID, "primary")
    assert value == BranchMembership(BRANCH_ID, "note", RECORD_ID, "primary")
    assert not hasattr(value, "assigned_at")


def test_internal_fence_key_namespace_cannot_collide_with_agent_requests() -> None:
    with pytest.raises(RecordContractError):
        CreateRecordCommand(
            "note",
            FrozenJsonObject.from_mapping({"body": "x"}),
            "__aipcs_write_admission",
        )


def test_member_vocabulary_has_one_canonical_cross_backend_order() -> None:
    fields = FrozenJsonObject.from_mapping({"body": "x"})
    record = RecordValue("note", RECORD_ID, 1, AT, AT, "test", fields)
    history = RecordHistoryEvent("note", RECORD_ID, 1, "create", AT, None, fields)
    branch = BranchValue(
        BRANCH_ID, "main", "Main", "Primary memory", None, None, "active", None, AT, AT, 1
    )
    membership = BranchMembership(BRANCH_ID, "note", RECORD_ID, "primary")
    members = [
        PortableStoreMember("branch_membership", membership),
        PortableStoreMember("history", history),
        PortableStoreMember("branch", branch),
        PortableStoreMember("record", record),
    ]
    assert [member.kind for member in sorted(members, key=portable_member_sort_key)] == [
        "record",
        "history",
        "branch",
        "branch_membership",
    ]
    with pytest.raises(PortableStoreContractError):
        PortableStoreMember("record", branch)  # type: ignore[arg-type]
