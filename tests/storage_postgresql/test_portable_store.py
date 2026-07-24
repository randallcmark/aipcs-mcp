"""PostgreSQL logical transfer parity and write-admission proof."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

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
from aipcs_mcp.storage.postgresql import portable as portable_module
from aipcs_mcp.storage.postgresql.connection import PostgreSQLDsn
from aipcs_mcp.storage.postgresql.data_store import PostgreSQLMaterialisedDataStore
from aipcs_mcp.storage.postgresql.portable import PostgreSQLPortableServiceStore

from .container import PostgresTestTarget

AT = datetime(2026, 7, 24, 12, tzinfo=UTC)
SERVICE_ID = UUID("40000000-0000-4000-8000-000000000001")
RECORD_ID = UUID("50000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("60000000-0000-4000-8000-000000000001")
PRINCIPAL = "portable-postgresql"


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
        MaterialisationStorage("postgresql", f"svc_{SERVICE_ID.hex}"),
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


def test_postgresql_matches_sqlite_portable_fixture_and_fence(
    postgres_test_target: PostgresTestTarget,
    monkeypatch,
) -> None:
    dsn = PostgreSQLDsn(postgres_test_target.dsn)
    adapter = PostgreSQLPortableServiceStore(dsn, clock=lambda: AT)
    closed = WriteAdmissionFence(1, "closed")
    assert adapter.stage(_service(), _members(), closed).status == "staged"
    with adapter.open_snapshot(_service(), closed) as reader:
        assert reader.fence == closed
        assert tuple(reader) == _members()
    assert adapter.observe(_service(), closed, _members())
    assert adapter.stage(_service(), _members(), closed).status == "already_staged"

    store = PostgreSQLMaterialisedDataStore(dsn, clock=lambda: AT)
    specification = compile_record_specification(_manifest())
    locator = adapter._catalog.allocate(SERVICE_ID)
    command = CreateRecordCommand(
        "note", FrozenJsonObject.from_mapping({"body": "blocked"}), "blocked"
    )
    assert store.create_record(locator, PRINCIPAL, "test", specification, command) == DataFailure(
        "write_admission_closed"
    )
    opened = adapter.transition_fence(_service(), closed, "open")
    assert opened.fence == WriteAdmissionFence(2, "open")
    assert not isinstance(
        store.create_record(locator, PRINCIPAL, "test", specification, command), DataFailure
    )

    fault_service_id = UUID("40000000-0000-4000-8000-000000000002")
    fault_service = replace(
        _service(),
        service_id=fault_service_id,
        storage=MaterialisationStorage(
            "postgresql", f"svc_{fault_service_id.hex}"
        ),
    )
    original = portable_module._write_fence
    monkeypatch.setattr(
        portable_module,
        "_write_fence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fault")),
    )
    try:
        try:
            adapter.stage(fault_service, _members(), closed)
        except RuntimeError as error:
            assert str(error) == "fault"
        else:
            raise AssertionError("fault was not injected")
    finally:
        monkeypatch.setattr(portable_module, "_write_fence", original)
    assert adapter.stage(fault_service, _members(), closed).status == "staged"
    assert adapter.observe(fault_service, closed, _members())
