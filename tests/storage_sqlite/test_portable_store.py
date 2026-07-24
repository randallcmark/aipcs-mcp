"""SQLite logical transfer, observation, and write-admission proof."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fixtures import entity

from aipcs_mcp.application.models import MaterialisationStorage, Service
from aipcs_mcp.application.portable_store import (
    BranchMembership,
    PortableStoreMember,
    WriteAdmissionFence,
)
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.records import (
    BranchValue,
    CreateRecordCommand,
    DataFailure,
    FrozenJsonObject,
    RecordHistoryEvent,
    RecordValue,
    compile_record_specification,
)
from aipcs_mcp.storage.errors import StorageContractError, StorageMigrationError
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy
from aipcs_mcp.storage.sqlite import portable as portable_module
from aipcs_mcp.storage.sqlite.data_store import SQLiteMaterialisedDataStore
from aipcs_mcp.storage.sqlite.portable import SQLitePortableServiceStore

AT = datetime(2026, 7, 24, 12, tzinfo=UTC)
SERVICE_ID = UUID("10000000-0000-4000-8000-000000000001")
RECORD_ID = UUID("20000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("30000000-0000-4000-8000-000000000001")
PRINCIPAL = "portable-sqlite"


def _manifest() -> ManifestV2:
    return ManifestV2.model_validate(
        {
            "manifest_version": 2,
            "schema_version": 1,
            "entities": [
                entity(
                    "note",
                    [{"name": "body", "type": "string", "required": True}],
                )
            ],
            "relationships": [],
            "indices": [],
            "query_patterns": [],
            "discovery_facets": [],
            "migration_history": [],
        }
    )


def _service() -> Service:
    manifest = _manifest()
    return Service(
        SERVICE_ID,
        PRINCIPAL,
        "notes",
        "project",
        "portable notes",
        AT,
        AT,
        AT,
        manifest,
        1,
        "materialised",
        "suspended",
        3,
        AT,
        MaterialisationStorage("sqlite", f"svc_{SERVICE_ID.hex}"),
    )


def _members() -> tuple[PortableStoreMember, ...]:
    fields = FrozenJsonObject.from_mapping({"body": "remember"})
    return (
        PortableStoreMember(
            "record", RecordValue("note", RECORD_ID, 1, AT, AT, "import", fields)
        ),
        PortableStoreMember(
            "history",
            RecordHistoryEvent("note", RECORD_ID, 1, "create", AT, None, fields),
        ),
        PortableStoreMember(
            "branch",
            BranchValue(
                BRANCH_ID,
                "main",
                "Main",
                "Primary memory",
                None,
                None,
                "active",
                None,
                AT,
                AT,
                1,
            ),
        ),
        PortableStoreMember(
            "branch_membership",
            BranchMembership(BRANCH_ID, "note", RECORD_ID, "primary"),
        ),
    )


def test_stage_snapshot_observe_and_exact_replay(tmp_path) -> None:  # type: ignore[no-untyped-def]
    adapter = SQLitePortableServiceStore(SQLiteLocationPolicy(tmp_path / "data"), clock=lambda: AT)
    fence = WriteAdmissionFence(1, "closed")
    assert adapter.stage(_service(), _members(), fence).status == "staged"
    with adapter.open_snapshot(_service(), fence) as reader:
        assert reader.fence == fence
        assert tuple(reader) == _members()
    assert adapter.observe(_service(), fence, _members())
    assert adapter.stage(_service(), _members(), fence).status == "already_staged"
    changed = _members()[:-1]
    assert adapter.stage(_service(), changed, fence).status == "different"


def test_fence_is_monotonic_and_blocks_every_data_mutation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    location = SQLiteLocationPolicy(tmp_path / "data")
    adapter = SQLitePortableServiceStore(location, clock=lambda: AT)
    closed = WriteAdmissionFence(1, "closed")
    assert adapter.stage(_service(), (), closed).status == "staged"
    store = SQLiteMaterialisedDataStore(location, clock=lambda: AT)
    specification = compile_record_specification(_manifest())
    command = CreateRecordCommand(
        "note", FrozenJsonObject.from_mapping({"body": "blocked"}), "blocked"
    )
    locator = adapter._catalog.allocate(SERVICE_ID)
    assert store.create_record(locator, PRINCIPAL, "test", specification, command) == DataFailure(
        "write_admission_closed"
    )
    opened = adapter.transition_fence(_service(), closed, "open")
    assert opened.status == "applied"
    assert opened.fence == WriteAdmissionFence(2, "open")
    assert adapter.transition_fence(_service(), closed, "open").status == "stale"
    assert adapter.transition_fence(_service(), opened.fence, "open").status == "current"
    assert not isinstance(
        store.create_record(locator, PRINCIPAL, "test", specification, command), DataFailure
    )
    with pytest.raises(StorageMigrationError):
        adapter.open_snapshot(_service(), closed)


def test_materialised_active_snapshot_is_rejected_before_store_access(tmp_path) -> None:  # type: ignore[no-untyped-def]
    adapter = SQLitePortableServiceStore(SQLiteLocationPolicy(tmp_path / "data"))
    with pytest.raises(StorageContractError):
        adapter.open_snapshot(
            replace(_service(), operational_status="active"),
            WriteAdmissionFence(1, "closed"),
        )


def test_stage_fault_rolls_back_data_and_retry_reobserves_exactly(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    location = SQLiteLocationPolicy(tmp_path / "data")
    adapter = SQLitePortableServiceStore(location, clock=lambda: AT)
    fence = WriteAdmissionFence(1, "closed")
    original = portable_module._write_fence
    monkeypatch.setattr(
        portable_module,
        "_write_fence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fault")),
    )
    with pytest.raises(RuntimeError, match="fault"):
        adapter.stage(_service(), _members(), fence)
    monkeypatch.setattr(portable_module, "_write_fence", original)
    assert adapter.stage(_service(), _members(), fence).status == "staged"
    assert adapter.observe(_service(), fence, _members())


def test_snapshot_path_executes_no_ddl(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    location = SQLiteLocationPolicy(tmp_path / "data")
    adapter = SQLitePortableServiceStore(location, clock=lambda: AT)
    fence = WriteAdmissionFence(1, "closed")
    assert adapter.stage(_service(), _members(), fence).status == "staged"
    statements: list[str] = []
    original = portable_module.connect

    def traced(*args, **kwargs):  # type: ignore[no-untyped-def]
        connection = original(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(portable_module, "connect", traced)
    with adapter.open_snapshot(_service(), fence) as reader:
        assert tuple(reader) == _members()
    assert not any(
        statement.lstrip().upper().startswith(("CREATE ", "ALTER ", "DROP ", "VACUUM "))
        for statement in statements
    )
