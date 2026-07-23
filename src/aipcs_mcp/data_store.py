"""Backend-neutral materialised record-store port.

The application admits a principal and resolves the logical
``ServiceStoreLocator``.  An adapter then owns local transactions, persistence,
idempotency, CAS, history, relationship enforcement, topology, and aggregates.
No location, SQL, driver, or migration details cross this port.
"""

from __future__ import annotations

from typing import Protocol

from .records import (
    AssignBranchRecordsCommand,
    BranchAssignmentResult,
    BranchMutationResult,
    BranchPageOutcome,
    CreateBranchCommand,
    CreateRecordCommand,
    DeleteRecordCommand,
    DeleteRecordResult,
    GetRecordQuery,
    HistoryOutcome,
    ListBranchesQuery,
    ListRecordsQuery,
    MaintenanceOutcome,
    MaintenanceQuery,
    RecordHistoryQuery,
    RecordMutationResult,
    RecordPageOutcome,
    RecordReadOutcome,
    RecordSpecification,
    SearchRecordsQuery,
    SummaryOutcome,
    SummaryQuery,
    UpdateBranchCommand,
    UpdateRecordCommand,
)
from .storage.contracts import ServiceStoreLocator


class MaterialisedDataStore(Protocol):
    """One service-local data adapter operating exclusively on detached values."""

    def create_record(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        command: CreateRecordCommand,
    ) -> RecordMutationResult: ...

    def get_record(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        query: GetRecordQuery,
    ) -> RecordReadOutcome: ...

    def list_records(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        query: ListRecordsQuery,
    ) -> RecordPageOutcome: ...

    def search_records(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        query: SearchRecordsQuery,
    ) -> RecordPageOutcome: ...

    def update_record(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        command: UpdateRecordCommand,
    ) -> RecordMutationResult: ...

    def delete_record(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        command: DeleteRecordCommand,
    ) -> DeleteRecordResult: ...

    def record_history(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        query: RecordHistoryQuery,
    ) -> HistoryOutcome: ...

    def create_branch(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        command: CreateBranchCommand,
    ) -> BranchMutationResult: ...

    def list_branches(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        query: ListBranchesQuery,
    ) -> BranchPageOutcome: ...

    def update_branch(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        command: UpdateBranchCommand,
    ) -> BranchMutationResult: ...

    def assign_branch_records(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        command: AssignBranchRecordsCommand,
    ) -> BranchAssignmentResult: ...

    def service_summary(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        query: SummaryQuery,
    ) -> SummaryOutcome: ...

    def maintenance_scan(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        query: MaintenanceQuery,
    ) -> MaintenanceOutcome: ...
