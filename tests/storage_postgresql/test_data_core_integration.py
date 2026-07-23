"""SYNTHETIC_FIXTURE. Managed PostgreSQL CRUD/history core workflow."""

from __future__ import annotations

from importlib import import_module
from uuid import UUID

from storage_contracts.parity_fixtures import parity_manifest

from aipcs_mcp.records import (
    AssignBranchRecordsCommand,
    BranchAssignmentTarget,
    CreateBranchCommand,
    CreateRecordCommand,
    DeleteRecordCommand,
    FrozenJsonObject,
    GetRecordQuery,
    ListBranchesQuery,
    RecordHistoryQuery,
    RecordMutationOutcome,
    UpdateRecordCommand,
    compile_record_specification,
)
from aipcs_mcp.relational import compile_manifest
from aipcs_mcp.storage.postgresql.connection import PostgreSQLDsn
from aipcs_mcp.storage.postgresql.data_store import PostgreSQLMaterialisedDataStore
from aipcs_mcp.storage.postgresql.domain_schema import PostgreSQLDomainSchemaStore
from aipcs_mcp.storage.postgresql.service_store import PostgreSQLServiceStoreCatalog

from .container import PostgresTestTarget


def test_postgresql_crud_history_replay_and_restrict_core(
    postgres_test_target: PostgresTestTarget,
) -> None:
    dsn = PostgreSQLDsn(postgres_test_target.dsn)
    catalog = PostgreSQLServiceStoreCatalog(dsn)
    locator = catalog.allocate(UUID(int=1))
    assert catalog.migrate(locator).status == "ready"
    manifest = parity_manifest()
    assert PostgreSQLDomainSchemaStore(dsn, service_store_catalog=catalog).materialise(
        locator, compile_manifest(manifest)
    ).status == "ready"
    store = PostgreSQLMaterialisedDataStore(dsn, service_store_catalog=catalog)
    specification = compile_record_specification(manifest)
    command = CreateRecordCommand(
        "note", FrozenJsonObject.from_mapping({"title": "parent", "tags": ["x"]}), "create-1"
    )
    created = store.create_record(locator, "principal", "test", specification, command)
    assert isinstance(created, RecordMutationOutcome)
    assert created.replayed is False
    replayed = store.create_record(locator, "principal", "test", specification, command)
    assert isinstance(replayed, RecordMutationOutcome)
    assert replayed.replayed is True
    child = store.create_record(
        locator,
        "principal",
        "test",
        specification,
        CreateRecordCommand(
            "note",
                FrozenJsonObject.from_mapping(
                    {"title": "child", "tags": ["child"], "parent_id": str(created.record.record_id)}
                ),
            "create-2",
        ),
    )
    assert isinstance(child, RecordMutationOutcome)
    changed = store.update_record(
        locator,
        "principal",
        "test",
        specification,
        UpdateRecordCommand(
            "note", created.record.record_id, FrozenJsonObject.from_mapping({"rank": 2}), 1, "update-1"
        ),
    )
    assert isinstance(changed, RecordMutationOutcome)
    assert changed.record.record_version == 2
    assert store.delete_record(
        locator,
        "principal",
        "test",
        specification,
        DeleteRecordCommand("note", created.record.record_id, 2, "delete-1"),
    ).code == "constraint_violation"
    found = store.get_record(
        locator, "principal", "test", specification, GetRecordQuery("note", created.record.record_id)
    )
    assert found.record_version == 2
    history = store.record_history(
        locator, "principal", "test", specification, RecordHistoryQuery("note", created.record.record_id)
    )
    assert [item.kind for item in history.events] == ["create", "update"]
    branch = store.create_branch(
        locator,
        "principal",
        "test",
        specification,
        CreateBranchCommand("primary-notes", "Primary notes", "Test branch", None, None, None, "branch-1"),
    )
    assert branch.branch.slug == "primary-notes"
    assignment = store.assign_branch_records(
        locator,
        "principal",
        "test",
        specification,
        AssignBranchRecordsCommand(
            branch.branch.branch_id,
            "primary",
            "assign",
            (BranchAssignmentTarget("note", child.record.record_id, 1),),
            "assign-1",
        ),
    )
    assert assignment.records[0].record_version == 2
    assert store.list_branches(
        locator, "principal", "test", specification, ListBranchesQuery()
    ).branches[0].branch_id == branch.branch.branch_id
    connection = import_module("psycopg").connect(postgres_test_target.dsn)
    try:
        connection.execute(
            f'ALTER TABLE "{locator.namespace}"."note" ALTER COLUMN "title" DROP NOT NULL'
        )
        connection.commit()
    finally:
        connection.close()
    assert store.get_record(
        locator, "principal", "test", specification, GetRecordQuery("note", created.record.record_id)
    ).code == "storage_migration_required"
