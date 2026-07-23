"""SYNTHETIC_FIXTURE. PostgreSQL binding for normalized core data parity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
from aipcs_mcp.storage.postgresql.connection import PostgreSQLDsn
from aipcs_mcp.storage.postgresql.data_store import PostgreSQLMaterialisedDataStore
from aipcs_mcp.storage.postgresql.domain_schema import PostgreSQLDomainSchemaStore
from aipcs_mcp.storage.postgresql.service_store import PostgreSQLServiceStoreCatalog

from .container import PostgresTestTarget


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


class PostgreSQLCoreParityHarness:
    """Production core adapter assembled only against the managed PG fixture."""

    def __init__(self, target: PostgresTestTarget) -> None:
        dsn = PostgreSQLDsn(target.dsn)
        catalog = PostgreSQLServiceStoreCatalog(dsn)
        self._locator = catalog.allocate(UUID(int=1))
        assert catalog.migrate(self._locator).status == "ready"
        manifest = parity_manifest()
        assert PostgreSQLDomainSchemaStore(dsn, service_store_catalog=catalog).materialise(
            self._locator, compile_manifest(manifest)
        ).status == "ready"
        self._specification = compile_record_specification(manifest)
        self._store = PostgreSQLMaterialisedDataStore(
            dsn,
            service_store_catalog=catalog,
            clock=_Clock(),
            record_ids=_Ids(10),
            branch_ids=_Ids(100),
        )

    @property
    def specification(self) -> RecordSpecification:
        return self._specification

    def create(self, principal: str, command: CreateRecordCommand) -> RecordMutationOutcome | DataFailure:
        return self._store.create_record(self._locator, principal, "parity", self._specification, command)

    def get(self, principal: str, query: GetRecordQuery) -> RecordValue | DataFailure:
        return self._store.get_record(self._locator, principal, "parity", self._specification, query)

    def list(self, principal: str, query: ListRecordsQuery) -> RecordPage | DataFailure:
        return self._store.list_records(self._locator, principal, "parity", self._specification, query)

    def search(self, principal: str, query: SearchRecordsQuery) -> RecordPage | DataFailure:
        return self._store.search_records(self._locator, principal, "parity", self._specification, query)

    def update(self, principal: str, command: UpdateRecordCommand) -> RecordMutationOutcome | DataFailure:
        return self._store.update_record(self._locator, principal, "parity", self._specification, command)

    def delete(self, principal: str, command: DeleteRecordCommand) -> DeleteRecordOutcome | DataFailure:
        return self._store.delete_record(self._locator, principal, "parity", self._specification, command)

    def history(self, principal: str, query: RecordHistoryQuery) -> HistoryPage | DataFailure:
        return self._store.record_history(self._locator, principal, "parity", self._specification, query)

    def create_branch(
        self, principal: str, command: CreateBranchCommand
    ) -> BranchMutationOutcome | DataFailure:
        return self._store.create_branch(self._locator, principal, "parity", self._specification, command)

    def list_branches(self, principal: str, query: ListBranchesQuery) -> BranchPage | DataFailure:
        return self._store.list_branches(self._locator, principal, "parity", self._specification, query)

    def update_branch(
        self, principal: str, command: UpdateBranchCommand
    ) -> BranchMutationOutcome | DataFailure:
        return self._store.update_branch(self._locator, principal, "parity", self._specification, command)

    def assign(
        self, principal: str, command: AssignBranchRecordsCommand
    ) -> BranchAssignmentOutcome | DataFailure:
        return self._store.assign_branch_records(
            self._locator, principal, "parity", self._specification, command
        )

    def summary(self, principal: str, query: SummaryQuery) -> ServiceSummary | DataFailure:
        return self._store.service_summary(self._locator, principal, "parity", self._specification, query)

    def maintenance(
        self, principal: str, query: MaintenanceQuery
    ) -> MaintenanceResult | DataFailure:
        return self._store.maintenance_scan(
            self._locator, principal, "parity", self._specification, query
        )


def test_postgresql_satisfies_normalized_crud_history_parity(
    postgres_test_target: PostgresTestTarget,
) -> None:
    assert_data_crud_history_idempotency_and_isolation(
        lambda: PostgreSQLCoreParityHarness(postgres_test_target)
    )


def test_postgresql_satisfies_normalized_filter_cursor_restrict_parity(
    postgres_test_target: PostgresTestTarget,
) -> None:
    assert_data_filters_cursors_and_restrict(lambda: PostgreSQLCoreParityHarness(postgres_test_target))


def test_postgresql_satisfies_normalized_branch_discovery_maintenance_parity(
    postgres_test_target: PostgresTestTarget,
) -> None:
    assert_data_branches_summary_and_maintenance(
        lambda: PostgreSQLCoreParityHarness(postgres_test_target)
    )
