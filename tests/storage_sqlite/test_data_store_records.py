"""SYNTHETIC_FIXTURE. SQLite materialised record-store conformance."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from fixtures import entity

from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.numeric_domain import (
    IEEE_754_EXACT_INTEGER_MAX,
    SIGNED_64_MAX,
    SIGNED_64_MIN,
)
from aipcs_mcp.records import (
    AssignBranchRecordsCommand,
    BranchAssignmentTarget,
    CreateBranchCommand,
    CreateRecordCommand,
    DataFailure,
    DeleteRecordCommand,
    FrozenJsonObject,
    GetRecordQuery,
    ListBranchesQuery,
    ListRecordsQuery,
    MaintenanceQuery,
    PageRequest,
    RecordHistoryQuery,
    SearchRecordsQuery,
    SummaryQuery,
    UpdateBranchCommand,
    UpdateRecordCommand,
    compile_record_specification,
)
from aipcs_mcp.relational import compile_manifest
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteServiceStoreCatalog
from aipcs_mcp.storage.sqlite import data_store as data_store_module
from aipcs_mcp.storage.sqlite.data_store import SQLiteMaterialisedDataStore
from aipcs_mcp.storage.sqlite.domain_schema import SQLiteDomainSchemaStore
from aipcs_mcp.storage.sqlite.service_store_migrations import (
    META,
    MIGRATION,
    R3_BRANCH_TABLE,
    R3_DDL,
    R3_HISTORY_TABLE,
    R3_MUTATION_TABLE,
    R3_RECORD_BRANCH_TABLE,
)

PRINCIPAL = "record-test-principal"
OTHER = "other-principal"
CREATED_VIA = "mcp"
START = datetime(2026, 7, 23, 12, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.value = START

    def __call__(self) -> datetime:
        value = self.value
        self.value += timedelta(seconds=1)
        return value


class _Ids:
    def __init__(self) -> None:
        self.value = 10

    def __call__(self) -> UUID:
        value = UUID(int=self.value)
        self.value += 1
        return value


class _BranchIds:
    def __init__(self) -> None:
        self.value = 100

    def __call__(self) -> UUID:
        value = UUID(int=self.value)
        self.value += 1
        return value


def _manifest() -> ManifestV2:
    return ManifestV2.model_validate(
        {
            "manifest_version": 2,
            "schema_version": 1,
            "entities": [
                entity(
                    "note",
                    [
                        {"name": "title", "type": "string", "required": True},
                        {"name": "rank", "type": "integer"},
                        {"name": "score", "type": "number"},
                        {"name": "active", "type": "boolean"},
                        {
                            "name": "tags",
                            "type": "string_list",
                            "retrieval_mode": "membership",
                        },
                        {"name": "parent_id", "type": "uuid"},
                    ],
                )
            ],
            "relationships": [
                {
                    "name": "note_parent_fk",
                    "from": {"entity": "note", "field": "parent_id"},
                    "to": {"entity": "note", "field": "id"},
                    "on_delete": "restrict",
                }
            ],
            "indices": [
                {
                    "name": "note_owner_title_idx",
                    "entity": "note",
                    "fields": ["owner_id", "title"],
                }
            ],
            "query_patterns": ["Find notes by title or tag."],
            "discovery_facets": [
                {"entity": "note", "field": "title"},
                {"entity": "note", "field": "tags"},
            ],
            "retrieval_guidance": "Use exact title and membership tag filters.",
            "migration_history": [],
        }
    )


def _fixture(tmp_path: Path, manifest: ManifestV2 | None = None):
    root = tmp_path / "data"
    location = SQLiteLocationPolicy(root)
    catalog = SQLiteServiceStoreCatalog(location)
    locator = catalog.allocate(UUID(int=1))
    assert catalog.migrate(locator).status == "ready"
    manifest = manifest or _manifest()
    assert SQLiteDomainSchemaStore(location).materialise(
        locator, compile_manifest(manifest)
    ).status == "ready"
    store = SQLiteMaterialisedDataStore(
        location,
        clock=_Clock(),
        record_ids=_Ids(),
        branch_ids=_BranchIds(),
    )
    return store, locator, compile_record_specification(manifest)


def _database(tmp_path: Path, locator) -> Path:  # type: ignore[no-untyped-def]
    return tmp_path / "data" / "service-stores" / f"{locator.namespace}.sqlite"


def _downgrade_exact_r3_to_r2(tmp_path: Path, locator) -> Path:  # type: ignore[no-untyped-def]
    database = _database(tmp_path, locator)
    with sqlite3.connect(database) as connection:
        for table in (
            R3_RECORD_BRANCH_TABLE,
            R3_BRANCH_TABLE,
            R3_HISTORY_TABLE,
            R3_MUTATION_TABLE,
        ):
            connection.execute(f'DROP TABLE "{table}"')
        connection.execute(f'DELETE FROM "{MIGRATION}" WHERE "revision"=3')
        connection.execute(
            f'UPDATE "{META}" SET "applied_revision"=2,"dirty"=0 WHERE "singleton"=1'
        )
        connection.commit()
    return database


def _prepare_r3(database: Path, *, alter: bool = False) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(f'UPDATE "{META}" SET "dirty"=1')
        for statement in R3_DDL:
            connection.execute(statement)
        if alter:
            connection.execute(
                f'ALTER TABLE "{R3_MUTATION_TABLE}" ADD COLUMN "hostile" TEXT'
            )
        connection.commit()


def _create(store, locator, specification, title, key, **extra):  # type: ignore[no-untyped-def]
    return store.create_record(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        CreateRecordCommand(
            "note",
            FrozenJsonObject.from_mapping({"title": title, **extra}),
            key,
        ),
    )


def test_crud_history_atomic_replay_cas_and_principal_isolation(tmp_path: Path) -> None:
    store, locator, specification = _fixture(tmp_path)

    created = _create(
        store,
        locator,
        specification,
        "first",
        "create-first",
        rank=1,
        active=True,
        tags=["alpha", "shared"],
    )
    record = created.record
    assert record is not None
    assert record.record_version == 1
    assert record.created_via == "mcp"
    assert record.fields.to_dict()["active"] is True

    replay = _create(
        store,
        locator,
        specification,
        "first",
        "create-first",
        rank=1,
        active=True,
        tags=["alpha", "shared"],
    )
    assert replay.record == record and replay.replayed is True
    assert store.create_record(
        locator,
        PRINCIPAL,
        "admin-cli",
        specification,
        CreateRecordCommand(
            "note",
            FrozenJsonObject.from_mapping(
                {"title": "first", "rank": 1, "active": True, "tags": ["alpha", "shared"]}
            ),
            "create-first",
        ),
    ) == DataFailure("changed_fingerprint")
    assert (
        _create(store, locator, specification, "changed", "create-first")
        == DataFailure("changed_fingerprint")
    )

    assert store.get_record(
        locator, OTHER, CREATED_VIA, specification, GetRecordQuery("note", record.record_id)
    ) == DataFailure("not_found")
    assert store.list_records(
        locator, OTHER, CREATED_VIA, specification, ListRecordsQuery("note")
    ).records == ()
    other_history = store.record_history(
        locator,
        OTHER,
        CREATED_VIA,
        specification,
        RecordHistoryQuery("note", record.record_id),
    )
    assert getattr(other_history, "events", None) == (), other_history

    updated = store.update_record(
        locator,
        PRINCIPAL,
        "admin-cli",
        specification,
        UpdateRecordCommand(
            "note",
            record.record_id,
            FrozenJsonObject.from_mapping({"title": "updated", "rank": 2}),
            1,
            "update-first",
        ),
    )
    assert updated.record is not None
    assert updated.record.record_version == 2
    assert updated.record.created_via == "mcp"
    assert updated.record.fields.to_dict()["title"] == "updated"
    assert store.update_record(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        UpdateRecordCommand(
            "note",
            record.record_id,
            FrozenJsonObject.from_mapping({"rank": 3}),
            1,
            "stale-update",
        ),
    ) == DataFailure("stale_record_version")

    update_replay = store.update_record(
        locator,
        PRINCIPAL,
        "admin-cli",
        specification,
        UpdateRecordCommand(
            "note",
            record.record_id,
            FrozenJsonObject.from_mapping({"title": "updated", "rank": 2}),
            1,
            "update-first",
        ),
    )
    assert update_replay.record == updated.record and update_replay.replayed is True

    deleted = store.delete_record(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        DeleteRecordCommand("note", record.record_id, 2, "delete-first"),
    )
    assert deleted.deleted_record_version == 3 and deleted.replayed is False
    assert store.get_record(
        locator, PRINCIPAL, CREATED_VIA, specification, GetRecordQuery("note", record.record_id)
    ) == DataFailure("not_found")
    delete_replay = store.delete_record(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        DeleteRecordCommand("note", record.record_id, 2, "delete-first"),
    )
    assert delete_replay.deleted_record_version == 3 and delete_replay.replayed is True

    history = store.record_history(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        RecordHistoryQuery("note", record.record_id),
    )
    assert [(event.record_version, event.kind) for event in history.events] == [
        (1, "create"),
        (2, "update"),
        (3, "delete"),
    ]
    assert history.events[0].after.to_dict()["title"] == "first"
    assert history.events[1].before.to_dict()["title"] == "first"
    assert history.events[2].before.to_dict()["title"] == "updated"


def test_search_cursor_is_query_bound_and_string_lists_use_membership(tmp_path: Path) -> None:
    store, locator, specification = _fixture(tmp_path)
    for index, title in enumerate(("a", "b", "c"), start=1):
        _create(
            store,
            locator,
            specification,
            title,
            f"create-{title}",
            rank=index,
            tags=["shared", title],
        )

    first = store.list_records(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        ListRecordsQuery("note", page=PageRequest(limit=2)),
    )
    assert [value.fields.to_dict()["title"] for value in first.records] == ["a", "b"]
    assert first.next_cursor is not None
    second = store.list_records(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        ListRecordsQuery("note", page=PageRequest(limit=2, cursor=first.next_cursor)),
    )
    assert [value.fields.to_dict()["title"] for value in second.records] == ["c"]
    assert second.next_cursor is None
    assert store.list_records(
        locator,
        OTHER,
        CREATED_VIA,
        specification,
        ListRecordsQuery("note", page=PageRequest(limit=2, cursor=first.next_cursor)),
    ) == DataFailure("validation_failed")

    tag_filter = specification.validate_filter("note", "tags", "shared")
    searched = store.search_records(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        SearchRecordsQuery("note", (tag_filter,), page=PageRequest(limit=2)),
    )
    assert len(searched.records) == 2 and searched.next_cursor is not None
    rank_filter = specification.validate_filter("note", "rank", 2)
    exact = store.search_records(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        SearchRecordsQuery("note", (rank_filter,)),
    )
    assert [value.fields.to_dict()["title"] for value in exact.records] == ["b"]
    assert store.search_records(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        SearchRecordsQuery(
            "note",
            (tag_filter,),
            page=PageRequest(limit=2, cursor=first.next_cursor),
        ),
    ) == DataFailure("validation_failed")


def test_relationships_are_principal_scoped_and_delete_is_restrict(tmp_path: Path) -> None:
    store, locator, specification = _fixture(tmp_path)
    parent = _create(store, locator, specification, "parent", "parent").record
    assert parent is not None
    child = _create(
        store,
        locator,
        specification,
        "child",
        "child",
        parent_id=str(parent.record_id),
    ).record
    assert child is not None

    assert store.delete_record(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        DeleteRecordCommand("note", parent.record_id, 1, "delete-parent-too-soon"),
    ) == DataFailure("constraint_violation")
    assert store.delete_record(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        DeleteRecordCommand("note", child.record_id, 1, "delete-child"),
    ).deleted_record_version == 2
    assert store.delete_record(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        DeleteRecordCommand("note", parent.record_id, 1, "delete-parent-too-soon"),
    ).deleted_record_version == 2

    assert _create(
        store,
        locator,
        specification,
        "orphan",
        "orphan",
        parent_id=str(UUID(int=999)),
    ) == DataFailure("constraint_violation")


def test_busy_is_retryable_and_post_commit_loss_replays_completed_result(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store, locator, specification = _fixture(tmp_path)
    root = tmp_path / "data"
    database = root / "service-stores" / f"{locator.namespace}.sqlite"
    fast_store = SQLiteMaterialisedDataStore(
        SQLiteLocationPolicy(root),
        busy_timeout_ms=1,
        clock=_Clock(),
        record_ids=_Ids(),
    )

    with sqlite3.connect(database, isolation_level=None) as blocker:
        blocker.execute("PRAGMA journal_mode=WAL")
        blocker.execute("BEGIN IMMEDIATE")
        assert _create(
            fast_store, locator, specification, "blocked", "blocked"
        ) == DataFailure("storage_busy")
        blocker.rollback()

    original_commit = data_store_module._commit

    def lose_observation(connection, location):  # type: ignore[no-untyped-def]
        original_commit(connection, location)
        raise data_store_module._OperationUncertain

    monkeypatch.setattr(data_store_module, "_commit", lose_observation)
    observed = _create(store, locator, specification, "uncertain", "uncertain")
    assert observed.replayed is True
    assert observed.record is not None and observed.record.fields.to_dict()["title"] == "uncertain"

    monkeypatch.setattr(data_store_module, "_commit", original_commit)
    replay = _create(store, locator, specification, "uncertain", "uncertain")
    assert replay.replayed is True
    assert replay.record is not None
    assert replay.record.fields.to_dict()["title"] == "uncertain"


def test_branch_adapter_composition_replay_cas_isolation_and_record_history(
    tmp_path: Path,
) -> None:
    store, locator, specification = _fixture(tmp_path)
    first_command = CreateBranchCommand(
        "first",
        "First",
        "First retrieval branch.",
        "topic",
        None,
        None,
        "branch-first",
    )
    first = store.create_branch(
        locator, PRINCIPAL, CREATED_VIA, specification, first_command
    )
    assert first.branch.branch_id == UUID(int=100)
    assert first.branch.branch_revision == 1
    replay = store.create_branch(
        locator, PRINCIPAL, CREATED_VIA, specification, first_command
    )
    assert replay.branch == first.branch and replay.replayed is True
    assert store.create_branch(
        locator, PRINCIPAL, "admin-cli", specification, first_command
    ) == DataFailure("changed_fingerprint")

    second = store.create_branch(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        CreateBranchCommand(
            "second",
            "Second",
            "Second retrieval branch.",
            None,
            first.branch.branch_id,
            None,
            "branch-second",
        ),
    )
    page = store.list_branches(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        ListBranchesQuery(page=PageRequest(limit=1)),
    )
    assert [branch.slug for branch in page.branches] == ["first"]
    assert page.next_cursor is not None
    following = store.list_branches(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        ListBranchesQuery(page=PageRequest(limit=1, cursor=page.next_cursor)),
    )
    assert [branch.slug for branch in following.branches] == ["second"]
    assert store.list_branches(
        locator,
        OTHER,
        CREATED_VIA,
        specification,
        ListBranchesQuery(page=PageRequest(limit=1, cursor=page.next_cursor)),
    ) == DataFailure("validation_failed")
    assert store.list_branches(
        locator, OTHER, CREATED_VIA, specification, ListBranchesQuery()
    ).branches == ()

    no_op_command = UpdateBranchCommand(
        first.branch.branch_id,
        FrozenJsonObject.from_mapping({"branch_type": "topic"}),
        1,
        "branch-no-op",
    )
    no_op = store.update_branch(
        locator, PRINCIPAL, CREATED_VIA, specification, no_op_command
    )
    assert no_op.branch.branch_revision == 1
    no_op_replay = store.update_branch(
        locator, PRINCIPAL, CREATED_VIA, specification, no_op_command
    )
    assert no_op_replay.replayed is True and no_op_replay.branch == no_op.branch
    updated = store.update_branch(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        UpdateBranchCommand(
            first.branch.branch_id,
            FrozenJsonObject.from_mapping({"title": "First updated"}),
            1,
            "branch-update",
        ),
    )
    assert updated.branch.branch_revision == 2
    assert store.update_branch(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        UpdateBranchCommand(
            first.branch.branch_id,
            FrozenJsonObject.from_mapping({"title": "Stale"}),
            1,
            "branch-stale",
        ),
    ) == DataFailure("stale_branch_revision")

    record = _create(
        store,
        locator,
        specification,
        "assigned",
        "assigned-record",
        tags=["branch"],
    ).record
    assert record is not None
    assignment = AssignBranchRecordsCommand(
        first.branch.branch_id,
        "primary",
        "assign",
        (BranchAssignmentTarget("note", record.record_id, 1),),
        "assign-record",
    )
    assigned = store.assign_branch_records(
        locator, PRINCIPAL, CREATED_VIA, specification, assignment
    )
    assert assigned.records[0].record_version == 2
    assignment_replay = store.assign_branch_records(
        locator, PRINCIPAL, CREATED_VIA, specification, assignment
    )
    assert assignment_replay.replayed is True
    current = store.get_record(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        GetRecordQuery("note", record.record_id),
    )
    assert current.record_version == 2
    history = store.record_history(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        RecordHistoryQuery("note", record.record_id),
    )
    assert [(event.record_version, event.kind) for event in history.events] == [
        (1, "create"),
        (2, "primary_assign"),
    ]
    assert history.events[1].before == history.events[1].after

    no_effect = store.assign_branch_records(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        AssignBranchRecordsCommand(
            first.branch.branch_id,
            "primary",
            "assign",
            (BranchAssignmentTarget("note", record.record_id, 2),),
            "assign-record-no-op",
        ),
    )
    assert no_effect.records[0].record_version == 2
    assert store.record_history(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        RecordHistoryQuery("note", record.record_id),
    ).events == history.events
    assert store.assign_branch_records(
        locator,
        OTHER,
        CREATED_VIA,
        specification,
        AssignBranchRecordsCommand(
            second.branch.branch_id,
            "related",
            "assign",
            (BranchAssignmentTarget("note", record.record_id, 2),),
            "cross-principal",
        ),
    ) == DataFailure("not_found")


def test_first_mutation_upgrades_exact_r2_but_reads_remain_pure(tmp_path: Path) -> None:
    store, locator, specification = _fixture(tmp_path)
    database = _downgrade_exact_r3_to_r2(tmp_path, locator)
    before = database.read_bytes()

    assert store.get_record(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        GetRecordQuery("note", UUID(int=10)),
    ) == DataFailure("storage_migration_required")
    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            f'SELECT 1 FROM sqlite_schema WHERE name="{R3_HISTORY_TABLE}"'
        ).fetchone() is None

    created = _create(
        store,
        locator,
        specification,
        "upgraded",
        "upgrade-create",
        tags=["r2"],
    )
    assert created.record is not None and created.record.record_version == 1
    assert store._service_store_catalog.inspect_migration(locator).status == "ready"
    history = store.record_history(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        RecordHistoryQuery("note", created.record.record_id),
    )
    assert [(event.record_version, event.kind) for event in history.events] == [(1, "create")]


def test_ready_r3_mutation_skips_catalog_migration(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store, locator, specification = _fixture(tmp_path)
    monkeypatch.setattr(
        store._service_store_catalog,
        "migrate",
        lambda _locator: (_ for _ in ()).throw(AssertionError("ready R3 migrated")),
    )

    created = _create(store, locator, specification, "ready", "ready-create")

    assert created.record is not None


def test_commit_uncertainty_recovers_exact_record_and_branch_replays(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store, locator, specification = _fixture(tmp_path)
    original_commit = data_store_module._commit

    def applied_then_unknown(connection, location):  # type: ignore[no-untyped-def]
        original_commit(connection, location)
        raise data_store_module._OperationUncertain

    monkeypatch.setattr(data_store_module, "_commit", applied_then_unknown)
    record = _create(store, locator, specification, "replayed", "replayed")
    assert record.replayed is True
    assert record.record is not None and record.record.record_version == 1
    branch = store.create_branch(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        CreateBranchCommand("replayed", "Replayed", "Replay branch.", None, None, None, "replayed-branch"),
    )
    assert branch.replayed is True and branch.branch.branch_revision == 1


def test_absent_post_fault_retries_once_and_corrupt_or_changed_evidence_is_uncertain(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store, locator, specification = _fixture(tmp_path)
    original_commit = data_store_module._commit
    calls = 0

    def precommit_once(connection, location):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise data_store_module._OperationUncertain
        original_commit(connection, location)

    monkeypatch.setattr(data_store_module, "_commit", precommit_once)
    retried = _create(store, locator, specification, "retried", "retried")
    assert retried.replayed is False and retried.record is not None
    assert calls == 2

    database = _database(tmp_path, locator)

    def apply_then_change(connection, location):  # type: ignore[no-untyped-def]
        original_commit(connection, location)
        with sqlite3.connect(database) as external:
            external.execute(
                f'UPDATE "{R3_MUTATION_TABLE}" SET "fingerprint"=? '
                'WHERE "principal_id"=? AND "idempotency_key"=?',
                ("0" * 64, PRINCIPAL, "changed-evidence"),
            )
            external.commit()
        raise data_store_module._OperationUncertain

    monkeypatch.setattr(data_store_module, "_commit", apply_then_change)
    assert _create(store, locator, specification, "changed", "changed-evidence") == DataFailure(
        "operation_uncertain"
    )

    def apply_then_corrupt(connection, location):  # type: ignore[no-untyped-def]
        original_commit(connection, location)
        with sqlite3.connect(database) as external:
            external.execute(
                f'UPDATE "{R3_MUTATION_TABLE}" SET "result_json"=? '
                'WHERE "principal_id"=? AND "idempotency_key"=?',
                ("{}", PRINCIPAL, "corrupt-evidence"),
            )
            external.commit()
        raise data_store_module._OperationUncertain

    monkeypatch.setattr(data_store_module, "_commit", apply_then_corrupt)
    assert store.create_branch(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        CreateBranchCommand(
            "corrupt", "Corrupt", "Corrupt evidence branch.", None, None, None, "corrupt-evidence"
        ),
    ) == DataFailure("operation_uncertain")


def test_uncertain_commit_with_unavailable_reopen_never_guesses_outcome(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store, locator, specification = _fixture(tmp_path)
    original_commit = data_store_module._commit
    original_acquire = data_store_module._acquire_existing
    acquired = 0

    def applied_then_unknown(connection, location):  # type: ignore[no-untyped-def]
        original_commit(connection, location)
        raise data_store_module._OperationUncertain

    def unavailable_after_first(location, namespace):  # type: ignore[no-untyped-def]
        nonlocal acquired
        acquired += 1
        if acquired > 1:
            raise data_store_module.StorageUnavailable()
        return original_acquire(location, namespace)

    monkeypatch.setattr(data_store_module, "_commit", applied_then_unknown)
    monkeypatch.setattr(data_store_module, "_acquire_existing", unavailable_after_first)
    assert _create(store, locator, specification, "unavailable", "unavailable") == DataFailure(
        "operation_uncertain"
    )


def test_ready_summary_and_maintenance_are_principal_scoped_and_read_only(tmp_path: Path) -> None:
    store, locator, specification = _fixture(tmp_path)
    first = _create(
        store, locator, specification, "summary-first", "summary-first", tags=["shared"]
    ).record
    second = _create(
        store, locator, specification, "summary-second", "summary-second", tags=["shared"]
    ).record
    assert first is not None and second is not None
    branch = store.create_branch(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        CreateBranchCommand("summary", "Summary", "Summary branch.", None, None, None, "summary-branch"),
    )
    assigned = store.assign_branch_records(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        AssignBranchRecordsCommand(
            branch.branch.branch_id,
            "primary",
            "assign",
            (BranchAssignmentTarget("note", first.record_id, 1),),
            "summary-assign",
        ),
    )
    assert assigned.records[0].record_version == 2
    database = _database(tmp_path, locator)
    before = database.read_bytes()

    summary = store.service_summary(
        locator, PRINCIPAL, CREATED_VIA, specification, SummaryQuery((("note", 1),))
    )
    assert summary.data_status == "ready"
    assert summary.record_counts == (("note", 2),)
    assert summary.unbranched_record_count == 1
    assert [(facet.field_name, [(item.value, item.count) for item in facet.values]) for facet in summary.facets] == [
        ("tags", [("shared", 2)]),
        ("title", [("summary-first", 1), ("summary-second", 1)]),
    ]
    assert summary.branches[0].primary_record_count == 1
    assert len(summary.samples[0][1]) == 1
    assert "principal" not in repr(summary).lower()
    assert database.read_bytes() == before

    maintenance = store.maintenance_scan(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        MaintenanceQuery(("unbranched",), max_candidates=10),
    )
    assert [(item.scan_type, item.record_id) for item in maintenance.candidates] == [
        ("unbranched", second.record_id)
    ]
    assert store.service_summary(
        locator, OTHER, CREATED_VIA, specification, SummaryQuery((("note", 1),))
    ).record_counts == (("note", 0),)
    assert store.maintenance_scan(
        locator, OTHER, CREATED_VIA, specification, MaintenanceQuery(("unbranched",))
    ).candidates == ()


def test_r2_summary_is_truthful_and_all_reads_remain_non_migrating(tmp_path: Path) -> None:
    store, locator, specification = _fixture(tmp_path)
    database = _downgrade_exact_r3_to_r2(tmp_path, locator)
    before = database.read_bytes()
    with sqlite3.connect(database) as connection:
        schema_before = connection.execute(
            "SELECT name,sql FROM sqlite_schema ORDER BY type,name"
        ).fetchall()

    summary = store.service_summary(
        locator, PRINCIPAL, CREATED_VIA, specification, SummaryQuery((("note", 1),))
    )
    assert summary.data_status == "migration_required"
    assert summary.record_counts == ()
    assert summary.facets == () and summary.branches == () and summary.samples == ()
    assert summary.unbranched_record_count == 0
    assert store.maintenance_scan(
        locator, PRINCIPAL, CREATED_VIA, specification, MaintenanceQuery(("unbranched",))
    ) == DataFailure("storage_migration_required")
    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT name,sql FROM sqlite_schema ORDER BY type,name").fetchall() == schema_before


def test_dirty_and_extra_reserved_r2_states_are_not_repaired(
    tmp_path: Path,
) -> None:
    for index, corruption in enumerate(("dirty", "extra_reserved"), start=1):
        case = tmp_path / corruption
        case.mkdir(mode=0o700)
        store, locator, specification = _fixture(case)
        database = _downgrade_exact_r3_to_r2(case, locator)
        with sqlite3.connect(database) as connection:
            if corruption == "dirty":
                connection.execute(f'UPDATE "{META}" SET "dirty"=1')
            else:
                connection.execute('CREATE TABLE "__aipcs_hostile" ("id" INTEGER) STRICT')
            connection.commit()

        assert _create(
            store,
            locator,
            specification,
            corruption,
            f"corrupt-{index}",
        ) == DataFailure("storage_migration_required")
        state = store._service_store_catalog.inspect_migration(locator)
        assert state.status == "incompatible"
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                f'SELECT "dirty","applied_revision" FROM "{META}"'
            ).fetchone() == ((1, 2) if corruption == "dirty" else (0, 2))
            if corruption == "extra_reserved":
                assert connection.execute(
                    'SELECT 1 FROM sqlite_schema WHERE name="__aipcs_hostile"'
                ).fetchone() == (1,)


def test_mutation_never_allocates_an_absent_service_store(tmp_path: Path) -> None:
    root = tmp_path / "absent"
    location = SQLiteLocationPolicy(root)
    locator = SQLiteServiceStoreCatalog(location).allocate(UUID(int=77))
    specification = compile_record_specification(_manifest())
    store = SQLiteMaterialisedDataStore(
        location,
        clock=_Clock(),
        record_ids=_Ids(),
        branch_ids=_BranchIds(),
    )

    result = _create(store, locator, specification, "absent", "absent-create")

    assert result == DataFailure("storage_migration_required")
    assert not root.exists()


def test_concurrent_first_mutations_converge_on_one_exact_r3_upgrade(
    tmp_path: Path,
) -> None:
    _store, locator, specification = _fixture(tmp_path)
    _downgrade_exact_r3_to_r2(tmp_path, locator)
    root = tmp_path / "data"

    def create_one(index: int):  # type: ignore[no-untyped-def]
        adapter = SQLiteMaterialisedDataStore(
            SQLiteLocationPolicy(root),
            busy_timeout_ms=10_000,
            clock=lambda: START + timedelta(seconds=index),
            record_ids=lambda: UUID(int=1_000 + index),
        )
        return _create(
            adapter,
            locator,
            specification,
            f"concurrent-{index}",
            f"concurrent-{index}",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(create_one, (1, 2)))

    assert all(result.record is not None for result in results)
    catalog = SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root), busy_timeout_ms=10_000)
    assert catalog.inspect_migration(locator).status == "ready"
    reader = SQLiteMaterialisedDataStore(SQLiteLocationPolicy(root), busy_timeout_ms=10_000)
    page = reader.list_records(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        ListRecordsQuery("note"),
    )
    assert {record.fields.to_dict()["title"] for record in page.records} == {
        "concurrent-1",
        "concurrent-2",
    }


def test_first_mutation_resumes_exact_prepared_r3(tmp_path: Path) -> None:
    store, locator, specification = _fixture(tmp_path)
    database = _downgrade_exact_r3_to_r2(tmp_path, locator)
    _prepare_r3(database)
    assert store._service_store_catalog.inspect_migration(locator).status == "dirty"

    created = _create(store, locator, specification, "resumed", "prepared-r3-create")

    assert created.record is not None
    assert created.record.fields.to_dict()["title"] == "resumed"
    assert store._service_store_catalog.inspect_migration(locator).status == "ready"


def test_first_mutation_never_repairs_altered_prepared_r3(tmp_path: Path) -> None:
    store, locator, specification = _fixture(tmp_path)
    database = _downgrade_exact_r3_to_r2(tmp_path, locator)
    _prepare_r3(database, alter=True)
    before = database.read_bytes()

    result = _create(store, locator, specification, "rejected", "altered-prepared-create")

    assert result == DataFailure("storage_migration_required")
    assert database.read_bytes() == before
    assert store._service_store_catalog.inspect_migration(locator).status == "incompatible"


def test_numeric_storage_round_trips_endpoints_and_exact_search(tmp_path: Path) -> None:
    store, locator, specification = _fixture(tmp_path)
    exact_number = IEEE_754_EXACT_INTEGER_MAX
    largest_real = float.fromhex("0x1.fffffffffffffp+1023")

    low = _create(
        store,
        locator,
        specification,
        "low",
        "numeric-low",
        rank=SIGNED_64_MIN,
        score=exact_number,
    ).record
    high = _create(
        store,
        locator,
        specification,
        "high",
        "numeric-high",
        rank=SIGNED_64_MAX,
        score=largest_real,
    ).record

    assert low is not None and high is not None
    assert low.fields.to_dict()["rank"] == SIGNED_64_MIN
    assert type(low.fields.to_dict()["score"]) is float
    assert low.fields.to_dict()["score"] == exact_number
    assert high.fields.to_dict()["rank"] == SIGNED_64_MAX
    assert high.fields.to_dict()["score"] == largest_real

    exact = store.search_records(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        SearchRecordsQuery(
            "note", (specification.validate_filter("note", "score", exact_number),)
        ),
    )
    assert [record.fields.to_dict()["title"] for record in exact.records] == ["low"]

    rejected = _create(
        store,
        locator,
        specification,
        "rounded",
        "numeric-rounded",
        score=IEEE_754_EXACT_INTEGER_MAX + 1,
    )
    assert rejected == DataFailure("validation_failed")
    assert store.update_record(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        UpdateRecordCommand(
            "note",
            low.record_id,
            FrozenJsonObject.from_mapping(
                {"score": IEEE_754_EXACT_INTEGER_MAX + 1}
            ),
            1,
            "numeric-rounded-update",
        ),
    ) == DataFailure("validation_failed")
    assert store.list_records(
        locator, PRINCIPAL, CREATED_VIA, specification, ListRecordsQuery("note")
    ).records == (low, high)


def test_string_list_enumerations_validate_each_member_before_storage(tmp_path: Path) -> None:
    payload = _manifest().model_dump(mode="json", by_alias=True)
    tags = next(
        attribute
        for attribute in payload["entities"][0]["attributes"]
        if attribute["name"] == "tags"
    )
    tags["allowed_values"] = ["bug", "feature"]
    store, locator, specification = _fixture(
        tmp_path, ManifestV2.model_validate(payload)
    )

    created = _create(
        store,
        locator,
        specification,
        "enumerated",
        "enumerated-list-create",
        tags=["bug", "feature"],
    ).record
    assert created is not None
    assert created.fields.to_dict()["tags"] == ["bug", "feature"]
    assert [
        record.record_id
        for record in store.search_records(
            locator,
            PRINCIPAL,
            CREATED_VIA,
            specification,
            SearchRecordsQuery(
                "note", (specification.validate_filter("note", "tags", "bug"),)
            ),
        ).records
    ] == [created.record_id]

    assert _create(
        store,
        locator,
        specification,
        "invalid",
        "enumerated-list-invalid-create",
        tags=["bug", "private"],
    ) == DataFailure("validation_failed")
    assert store.update_record(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        UpdateRecordCommand(
            "note",
            created.record_id,
            FrozenJsonObject.from_mapping({"tags": ["private"]}),
            1,
            "enumerated-list-invalid-update",
        ),
    ) == DataFailure("validation_failed")
    current = store.get_record(
        locator,
        PRINCIPAL,
        CREATED_VIA,
        specification,
        GetRecordQuery("note", created.record_id),
    )
    assert current == created
