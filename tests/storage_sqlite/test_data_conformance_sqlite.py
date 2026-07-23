"""SYNTHETIC_FIXTURE. SQLite binding for materialised-data public parity cases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from storage_contracts.data_conformance import (
    assert_data_branches_summary_and_maintenance,
    assert_data_crud_history_idempotency_and_isolation,
    assert_data_filters_cursors_and_restrict,
)
from storage_contracts.parity_fixtures import parity_manifest

from aipcs_mcp.records import (
    AssignBranchRecordsCommand,
    BranchAssignmentOutcome,
    BranchMutationOutcome,
    BranchPage,
    CreateBranchCommand,
    CreateRecordCommand,
    DataFailure,
    DeleteRecordCommand,
    DeleteRecordOutcome,
    GetRecordQuery,
    HistoryPage,
    ListBranchesQuery,
    ListRecordsQuery,
    MaintenanceQuery,
    MaintenanceResult,
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
    compile_record_specification,
)
from aipcs_mcp.relational import compile_manifest
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteServiceStoreCatalog
from aipcs_mcp.storage.sqlite.data_store import SQLiteMaterialisedDataStore
from aipcs_mcp.storage.sqlite.domain_schema import SQLiteDomainSchemaStore


class _Clock:
    def __init__(self) -> None:
        self._value = datetime(2026, 7, 23, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self._value
        self._value += timedelta(seconds=1)
        return value


class _Ids:
    def __init__(self, start: int) -> None:
        self._value = start

    def __call__(self) -> UUID:
        value = UUID(int=self._value)
        self._value += 1
        return value


class SQLiteDataParityHarness:
    """SQLite-only assembly behind the backend-neutral data harness protocol."""

    def __init__(self, root: Path) -> None:
        location = SQLiteLocationPolicy(root)
        catalog = SQLiteServiceStoreCatalog(location)
        self._locator = catalog.allocate(UUID(int=1))
        assert catalog.migrate(self._locator).status == "ready"
        manifest = parity_manifest()
        assert SQLiteDomainSchemaStore(location).materialise(
            self._locator, compile_manifest(manifest)
        ).status == "ready"
        self._specification = compile_record_specification(manifest)
        self._store = SQLiteMaterialisedDataStore(
            location, clock=_Clock(), record_ids=_Ids(10), branch_ids=_Ids(100)
        )

    @property
    def specification(self) -> RecordSpecification:
        return self._specification

    def create(self, principal: str, command: CreateRecordCommand) -> RecordMutationOutcome | DataFailure:
        return self._store.create_record(
            self._locator, principal, "parity", self._specification, command
        )

    def get(self, principal: str, query: GetRecordQuery) -> RecordValue | DataFailure:
        return self._store.get_record(self._locator, principal, "parity", self._specification, query)

    def list(self, principal: str, query: ListRecordsQuery) -> RecordPage | DataFailure:
        return self._store.list_records(self._locator, principal, "parity", self._specification, query)

    def search(self, principal: str, query: SearchRecordsQuery) -> RecordPage | DataFailure:
        return self._store.search_records(self._locator, principal, "parity", self._specification, query)

    def update(self, principal: str, command: UpdateRecordCommand) -> RecordMutationOutcome | DataFailure:
        return self._store.update_record(
            self._locator, principal, "parity", self._specification, command
        )

    def delete(self, principal: str, command: DeleteRecordCommand) -> DeleteRecordOutcome | DataFailure:
        return self._store.delete_record(
            self._locator, principal, "parity", self._specification, command
        )

    def history(self, principal: str, query: RecordHistoryQuery) -> HistoryPage | DataFailure:
        return self._store.record_history(
            self._locator, principal, "parity", self._specification, query
        )

    def create_branch(
        self, principal: str, command: CreateBranchCommand
    ) -> BranchMutationOutcome | DataFailure:
        return self._store.create_branch(
            self._locator, principal, "parity", self._specification, command
        )

    def list_branches(self, principal: str, query: ListBranchesQuery) -> BranchPage | DataFailure:
        return self._store.list_branches(
            self._locator, principal, "parity", self._specification, query
        )

    def update_branch(
        self, principal: str, command: UpdateBranchCommand
    ) -> BranchMutationOutcome | DataFailure:
        return self._store.update_branch(
            self._locator, principal, "parity", self._specification, command
        )

    def assign(
        self, principal: str, command: AssignBranchRecordsCommand
    ) -> BranchAssignmentOutcome | DataFailure:
        return self._store.assign_branch_records(
            self._locator, principal, "parity", self._specification, command
        )

    def summary(self, principal: str, query: SummaryQuery) -> ServiceSummary | DataFailure:
        return self._store.service_summary(
            self._locator, principal, "parity", self._specification, query
        )

    def maintenance(
        self, principal: str, query: MaintenanceQuery
    ) -> MaintenanceResult | DataFailure:
        return self._store.maintenance_scan(
            self._locator, principal, "parity", self._specification, query
        )


def test_sqlite_satisfies_data_crud_history_idempotency_and_isolation(tmp_path: Path) -> None:
    assert_data_crud_history_idempotency_and_isolation(
        lambda: SQLiteDataParityHarness(tmp_path / "crud")
    )


def test_sqlite_satisfies_data_filters_cursors_and_restrict(tmp_path: Path) -> None:
    assert_data_filters_cursors_and_restrict(lambda: SQLiteDataParityHarness(tmp_path / "queries"))


def test_sqlite_satisfies_data_branches_summary_and_maintenance(tmp_path: Path) -> None:
    assert_data_branches_summary_and_maintenance(
        lambda: SQLiteDataParityHarness(tmp_path / "discovery")
    )
