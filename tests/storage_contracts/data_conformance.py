"""Backend-neutral public materialised-data parity cases for future adapters."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from aipcs_mcp.records import (
    AssignBranchRecordsCommand,
    BranchAssignmentOutcome,
    BranchAssignmentTarget,
    BranchMutationOutcome,
    BranchPage,
    BranchValue,
    CreateBranchCommand,
    CreateRecordCommand,
    DataFailure,
    DeleteRecordCommand,
    DeleteRecordOutcome,
    FrozenJsonObject,
    GetRecordQuery,
    HistoryPage,
    ListBranchesQuery,
    ListRecordsQuery,
    MaintenanceQuery,
    MaintenanceResult,
    PageRequest,
    RecordHistoryQuery,
    RecordMutationOutcome,
    RecordPage,
    RecordSpecification,
    RecordValue,
    SearchRecordsQuery,
    ServiceSummary,
    SummaryQuery,
    UpdateBranchCommand,
    UpdateRecordCommand,
)

PRINCIPAL = "parity-principal"
OTHER_PRINCIPAL = "other-parity-principal"


class DataParityHarness(Protocol):
    """Backend adapter calls expressed through the materialised-data public port."""

    @property
    def specification(self) -> RecordSpecification: ...

    def create(self, principal: str, command: CreateRecordCommand) -> RecordMutationOutcome | DataFailure: ...

    def get(self, principal: str, query: GetRecordQuery) -> RecordValue | DataFailure: ...

    def list(self, principal: str, query: ListRecordsQuery) -> RecordPage | DataFailure: ...

    def search(self, principal: str, query: SearchRecordsQuery) -> RecordPage | DataFailure: ...

    def update(self, principal: str, command: UpdateRecordCommand) -> RecordMutationOutcome | DataFailure: ...

    def delete(self, principal: str, command: DeleteRecordCommand) -> DeleteRecordOutcome | DataFailure: ...

    def history(self, principal: str, query: RecordHistoryQuery) -> HistoryPage | DataFailure: ...

    def create_branch(
        self, principal: str, command: CreateBranchCommand
    ) -> BranchMutationOutcome | DataFailure: ...

    def list_branches(self, principal: str, query: ListBranchesQuery) -> BranchPage | DataFailure: ...

    def update_branch(
        self, principal: str, command: UpdateBranchCommand
    ) -> BranchMutationOutcome | DataFailure: ...

    def assign(
        self, principal: str, command: AssignBranchRecordsCommand
    ) -> BranchAssignmentOutcome | DataFailure: ...

    def summary(self, principal: str, query: SummaryQuery) -> ServiceSummary | DataFailure: ...

    def maintenance(
        self, principal: str, query: MaintenanceQuery
    ) -> MaintenanceResult | DataFailure: ...


DataParityHarnessFactory = Callable[[], DataParityHarness]


def assert_data_crud_history_idempotency_and_isolation(factory: DataParityHarnessFactory) -> None:
    """Assert CRUD, history, replay/change rejection, CAS, and principal isolation."""

    harness = factory()
    command = _create("first", "create-first", rank=1, tags=["alpha", "shared"])
    created = _created(harness.create(PRINCIPAL, command))
    assert created.record_version == 1
    assert created.fields.to_dict()["title"] == "first"
    replay = _created(harness.create(PRINCIPAL, command))
    assert replay == created
    assert harness.create(PRINCIPAL, _create("changed", "create-first")) == DataFailure(
        "changed_fingerprint"
    )
    assert harness.get(OTHER_PRINCIPAL, GetRecordQuery("note", created.record_id)) == DataFailure(
        "not_found"
    )
    other_page = _page(harness.list(OTHER_PRINCIPAL, ListRecordsQuery("note")))
    assert other_page.records == ()

    updated = _created(
        harness.update(
            PRINCIPAL,
            UpdateRecordCommand(
                "note",
                created.record_id,
                FrozenJsonObject.from_mapping({"title": "updated", "rank": 2}),
                1,
                "update-first",
            ),
        )
    )
    assert updated.record_version == 2 and updated.fields.to_dict()["title"] == "updated"
    assert harness.update(
        PRINCIPAL,
        UpdateRecordCommand(
            "note", created.record_id, FrozenJsonObject.from_mapping({"rank": 3}), 1, "stale"
        ),
    ) == DataFailure("stale_record_version")
    assert _created(
        harness.update(
            PRINCIPAL,
            UpdateRecordCommand(
                "note",
                created.record_id,
                FrozenJsonObject.from_mapping({"title": "updated", "rank": 2}),
                1,
                "update-first",
            ),
        )
    ) == updated

    deleted = harness.delete(
        PRINCIPAL, DeleteRecordCommand("note", created.record_id, 2, "delete-first")
    )
    assert isinstance(deleted, DeleteRecordOutcome) and deleted.deleted_record_version == 3
    deleted_replay = harness.delete(
        PRINCIPAL, DeleteRecordCommand("note", created.record_id, 2, "delete-first")
    )
    assert isinstance(deleted_replay, DeleteRecordOutcome) and deleted_replay.replayed is True
    history = _history(harness.history(PRINCIPAL, RecordHistoryQuery("note", created.record_id)))
    assert [(event.record_version, event.kind) for event in history.events] == [
        (1, "create"),
        (2, "update"),
        (3, "delete"),
    ]


def assert_data_filters_cursors_and_restrict(factory: DataParityHarnessFactory) -> None:
    """Assert deterministic ordering, scalar/list filters, query-bound cursors, and restrict."""

    harness = factory()
    parent = _created(harness.create(PRINCIPAL, _create("a", "a", rank=1, tags=["shared"])))
    child = _created(
        harness.create(
            PRINCIPAL,
            _create("b", "b", rank=2, tags=["shared", "b"], parent_id=str(parent.record_id)),
        )
    )
    _created(harness.create(PRINCIPAL, _create("c", "c", rank=3, tags=["c"])))
    first = _page(harness.list(PRINCIPAL, ListRecordsQuery("note", page=PageRequest(limit=2))))
    assert [record.fields.to_dict()["title"] for record in first.records] == ["a", "b"]
    assert first.records[0].fields.to_dict() == {
        "parent_id": None,
        "rank": 1,
        "tags": ["shared"],
        "title": "a",
    }
    assert first.next_cursor is not None
    second = _page(
        harness.list(PRINCIPAL, ListRecordsQuery("note", page=PageRequest(limit=2, cursor=first.next_cursor)))
    )
    assert [record.fields.to_dict()["title"] for record in second.records] == ["c"]
    tag = harness.specification.validate_filter("note", "tags", "shared")
    membership = _page(harness.search(PRINCIPAL, SearchRecordsQuery("note", (tag,))))
    assert [record.fields.to_dict()["title"] for record in membership.records] == ["a", "b"]
    rank = harness.specification.validate_filter("note", "rank", 2)
    exact = _page(harness.search(PRINCIPAL, SearchRecordsQuery("note", (rank,))))
    assert [record.fields.to_dict()["title"] for record in exact.records] == ["b"]
    assert harness.search(
        PRINCIPAL,
        SearchRecordsQuery("note", (tag,), page=PageRequest(limit=2, cursor=first.next_cursor)),
    ) == DataFailure("validation_failed")
    assert harness.delete(
        PRINCIPAL, DeleteRecordCommand("note", parent.record_id, 1, "blocked-delete")
    ) == DataFailure("constraint_violation")
    assert harness.delete(
        PRINCIPAL, DeleteRecordCommand("note", child.record_id, 1, "delete-child")
    ) == _deleted(child.record_id, 2)


def assert_data_branches_summary_and_maintenance(factory: DataParityHarnessFactory) -> None:
    """Assert branch CAS/assignment plus discovery summaries, samples, and maintenance."""

    harness = factory()
    first = _created(harness.create(PRINCIPAL, _create("first", "first", tags=["shared"])))
    second = _created(harness.create(PRINCIPAL, _create("second", "second", tags=["shared"])))
    branch_command = CreateBranchCommand(
        "main", "Main", "Primary retrieval branch.", "topic", None, None, "branch-main"
    )
    branch = _branch(harness.create_branch(PRINCIPAL, branch_command))
    assert _branch(harness.create_branch(PRINCIPAL, branch_command)) == branch
    branch_page = _branches(
        harness.list_branches(PRINCIPAL, ListBranchesQuery(page=PageRequest(limit=1)))
    )
    assert [value.slug for value in branch_page.branches] == ["main"]
    assert harness.update_branch(
        PRINCIPAL,
        UpdateBranchCommand(
            branch.branch_id, FrozenJsonObject.from_mapping({"title": "stale"}), 2, "stale-branch"
        ),
    ) == DataFailure("stale_branch_revision")
    assigned = harness.assign(
        PRINCIPAL,
        AssignBranchRecordsCommand(
            branch.branch_id,
            "primary",
            "assign",
            (BranchAssignmentTarget("note", first.record_id, 1),),
            "assign-first",
        ),
    )
    assert isinstance(assigned, BranchAssignmentOutcome) and assigned.records[0].record_version == 2
    replay = harness.assign(
        PRINCIPAL,
        AssignBranchRecordsCommand(
            branch.branch_id,
            "primary",
            "assign",
            (BranchAssignmentTarget("note", first.record_id, 1),),
            "assign-first",
        ),
    )
    assert isinstance(replay, BranchAssignmentOutcome) and replay.replayed is True
    assert harness.assign(
        OTHER_PRINCIPAL,
        AssignBranchRecordsCommand(
            branch.branch_id,
            "related",
            "assign",
            (BranchAssignmentTarget("note", first.record_id, 2),),
            "other-assign",
        ),
    ) == DataFailure("not_found")
    summary = harness.summary(PRINCIPAL, SummaryQuery((("note", 1),)))
    assert isinstance(summary, ServiceSummary)
    assert summary.record_counts == (("note", 2),) and summary.unbranched_record_count == 1
    assert [(facet.field_name, tuple(value.value for value in facet.values)) for facet in summary.facets] == [
        ("tags", ("shared",)),
        ("title", ("first", "second")),
    ]
    assert len(summary.samples[0][1]) == 1
    maintenance = harness.maintenance(
        PRINCIPAL, MaintenanceQuery(("unbranched",), max_candidates=10)
    )
    assert isinstance(maintenance, MaintenanceResult)
    assert [(value.scan_type, value.record_id) for value in maintenance.candidates] == [
        ("unbranched", second.record_id)
    ]
    other_summary = harness.summary(OTHER_PRINCIPAL, SummaryQuery())
    assert isinstance(other_summary, ServiceSummary) and other_summary.record_counts == (("note", 0),)


def _create(title: str, key: str, **extra: object) -> CreateRecordCommand:
    return CreateRecordCommand("note", FrozenJsonObject.from_mapping({"title": title, **extra}), key)


def _created(result: RecordMutationOutcome | DataFailure) -> RecordValue:
    assert isinstance(result, RecordMutationOutcome) and result.record is not None
    return result.record


def _page(result: RecordPage | DataFailure) -> RecordPage:
    assert isinstance(result, RecordPage)
    return result


def _history(result: HistoryPage | DataFailure) -> HistoryPage:
    assert isinstance(result, HistoryPage)
    return result


def _branch(result: BranchMutationOutcome | DataFailure) -> BranchValue:
    assert isinstance(result, BranchMutationOutcome)
    return result.branch


def _branches(result: BranchPage | DataFailure) -> BranchPage:
    assert isinstance(result, BranchPage)
    return result


def _deleted(record_id: UUID, version: int) -> DeleteRecordOutcome:
    return DeleteRecordOutcome(record_id, version, False)
