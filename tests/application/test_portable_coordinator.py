"""Finite portable orchestration and restart recovery over durable fake boundaries."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID

import pytest
from fixtures import entity

from aipcs_mcp.application.models import MaterialisationStorage, Service
from aipcs_mcp.application.operational_lifecycle import (
    ArchiveCommand,
    RestoreCommand,
    ResumeCommand,
    SuspendCommand,
)
from aipcs_mcp.application.portable import ImportBundleRequest, PortableResultCategory
from aipcs_mcp.application.portable_codec import PortableBundleEncoder
from aipcs_mcp.application.portable_models import PortableHeader
from aipcs_mcp.application.portable_store import (
    BranchMembership,
    FenceTransitionResult,
    PortableStoreMember,
    PurgeStoreResult,
    StageResult,
    WriteAdmissionFence,
)
from aipcs_mcp.application.registry_authority import (
    ExportCommand,
    PurgeAuthority,
    PurgeAuthorityKind,
    PurgeCommand,
    StorageBackend,
)
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.portable_coordinator import PortableCoordinator
from aipcs_mcp.records import (
    BranchValue,
    FrozenJsonObject,
    RecordHistoryEvent,
    RecordValue,
)

from .fakes import FakeRegistry, FixedClock

AT = datetime(2026, 7, 24, 12, tzinfo=UTC)
LATER = datetime(2026, 7, 24, 12, 1, tzinfo=UTC)
SERVICE_ID = UUID("10000000-0000-4000-8000-000000000001")
RECORD_ID = UUID("20000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("30000000-0000-4000-8000-000000000001")
EXPORT_RECEIPT = UUID("40000000-0000-4000-8000-000000000001")
IMPORT_RECEIPT = UUID("40000000-0000-4000-8000-000000000002")
PRINCIPAL = "portable-test"
INITIAL_CLOSED_FENCE = WriteAdmissionFence(1, "closed")


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


def _service(
    *,
    principal: str = PRINCIPAL,
    backend: str = "sqlite",
    design_state: str = "materialised",
) -> Service:
    materialised = design_state == "materialised"
    return Service(
        SERVICE_ID,
        principal,
        "notes",
        "project",
        "portable notes",
        AT,
        AT,
        AT,
        _manifest(),
        1,
        design_state,  # type: ignore[arg-type]
        "suspended",
        3,
        AT if materialised else None,
        MaterialisationStorage(backend, f"svc_{SERVICE_ID.hex}") if materialised else None,  # type: ignore[arg-type]
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


class FakePortableStore:
    def __init__(
        self,
        *,
        members: tuple[PortableStoreMember, ...] = (),
        fence: WriteAdmissionFence = INITIAL_CLOSED_FENCE,
    ) -> None:
        self.members = members
        self.fence = fence
        self.staged = bool(members)
        self.calls: list[str] = []
        self.fail_after_stage = False
        self.fail_before_stage_once = False
        self.fail_after_transition = False
        self.change_after_snapshot = False
        self.registry: FakeRegistry | None = None
        self.purge_calls = 0
        self.absent = False

    def open_snapshot(self, service, expected_fence):  # type: ignore[no-untyped-def]
        self.calls.append("open_snapshot")
        if expected_fence != self.fence:
            raise RuntimeError("fence")
        return _Snapshot(self)

    def stage(self, service, members, initial_fence):  # type: ignore[no-untyped-def]
        self.calls.append("stage")
        if self.fail_before_stage_once:
            self.fail_before_stage_once = False
            raise RuntimeError("pre-stage fault")
        values = tuple(members)
        if self.staged:
            status = "already_staged" if values == self.members else "different"
            return StageResult(status, self.fence)
        self.members = values
        self.fence = initial_fence
        self.staged = True
        if self.registry is not None:
            self.registry.fail_at = "commit"
        if self.fail_after_stage:
            raise RuntimeError("post-stage fault")
        return StageResult("staged", self.fence)

    def observe(self, service, expected_fence, members):  # type: ignore[no-untyped-def]
        self.calls.append("observe")
        return (
            self.staged
            and self.fence == expected_fence
            and self.members == tuple(members)
        )

    def read_fence(self, service):  # type: ignore[no-untyped-def]
        self.calls.append("read_fence")
        return self.fence

    def transition_fence(self, service, expected, target_state):  # type: ignore[no-untyped-def]
        self.calls.append("transition_fence")
        if expected != self.fence:
            return FenceTransitionResult("stale", self.fence)
        if self.fence.state == target_state:
            return FenceTransitionResult("current", self.fence)
        self.fence = WriteAdmissionFence(self.fence.generation + 1, target_state)
        if self.fail_after_transition:
            raise RuntimeError("post-transition fault")
        return FenceTransitionResult("applied", self.fence)

    def purge(self, service):  # type: ignore[no-untyped-def]
        self.calls.append("purge")
        self.purge_calls += 1
        if self.absent:
            return PurgeStoreResult("already_absent")
        self.absent = True
        self.staged = False
        self.members = ()
        return PurgeStoreResult("purged")

    def is_absent(self, service):  # type: ignore[no-untyped-def]
        self.calls.append("is_absent")
        return self.absent


class _Snapshot:
    def __init__(self, store: FakePortableStore) -> None:
        self.store = store

    @property
    def fence(self) -> WriteAdmissionFence:
        return self.store.fence

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.store.members)

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *_):  # type: ignore[no-untyped-def]
        self.close()

    def close(self) -> None:
        if self.store.change_after_snapshot:
            self.store.fence = WriteAdmissionFence(
                self.store.fence.generation + 1, "open"
            )


def _coordinator(
    registry: FakeRegistry,
    store: FakePortableStore,
    *,
    backend: StorageBackend = StorageBackend.SQLITE,
    receipt: UUID = EXPORT_RECEIPT,
    at: datetime = AT,
) -> PortableCoordinator:
    return PortableCoordinator(
        registry.uow,
        FixedClock(at),
        lambda: receipt,
        backend,
        store,
    )


def _export_bundle() -> bytes:
    registry = FakeRegistry()
    registry.items[SERVICE_ID] = _service()
    destination = BytesIO()
    result = _coordinator(registry, FakePortableStore(members=_members())).export(
        ExportCommand(PRINCIPAL, "test", SERVICE_ID, 3, "export-1"),
        destination,
    )
    assert result.category is PortableResultCategory.COMPLETED
    assert result.artifact_written
    artifact = destination.getvalue()
    assert b'"principal' not in artifact
    assert b"svc_" not in artifact
    assert b"postgresql://" not in artifact
    return artifact


def test_export_then_dry_run_validates_without_destination_writes() -> None:
    artifact = _export_bundle()
    destination = FakeRegistry()
    store = FakePortableStore()
    result = _coordinator(
        destination, store, receipt=IMPORT_RECEIPT, at=LATER
    ).import_bundle(
        ImportBundleRequest(
            "destination",
            "test",
            StorageBackend.SQLITE,
            "import-1",
            dry_run=True,
        ),
        BytesIO(artifact),
    )
    assert result.category is PortableResultCategory.VALIDATED
    assert result.service is not None
    assert result.service.principal_id == "destination"
    assert destination.uows == []
    assert store.calls == []


def test_completed_export_replay_reemits_only_the_exact_unchanged_snapshot() -> None:
    registry = FakeRegistry()
    registry.items[SERVICE_ID] = _service()
    store = FakePortableStore(members=_members())
    coordinator = _coordinator(registry, store)
    command = ExportCommand(PRINCIPAL, "test", SERVICE_ID, 3, "export-replay")
    first = BytesIO()
    assert coordinator.export(command, first).artifact_written

    replay = BytesIO()
    replayed = coordinator.export(command, replay)
    assert replayed.category is PortableResultCategory.COMPLETED
    assert replayed.artifact_written
    assert replay.getvalue() == first.getvalue()
    assert replayed.receipt is not None
    assert replayed.receipt.receipt_id == EXPORT_RECEIPT

    registry.items[SERVICE_ID] = replace(
        registry.items[SERVICE_ID],
        operational_status="archived",
        service_revision=4,
    )
    changed = coordinator.export(command, BytesIO())
    assert changed.category is PortableResultCategory.STALE_REVISION


def test_seeded_export_and_import_never_open_a_service_store() -> None:
    source_registry = FakeRegistry()
    source_registry.items[SERVICE_ID] = replace(
        _service(design_state="seeded"), operational_status="active"
    )
    source_store = FakePortableStore()
    artifact = BytesIO()
    exported = _coordinator(source_registry, source_store).export(
        ExportCommand(PRINCIPAL, "test", SERVICE_ID, 3, "export-seeded"),
        artifact,
    )
    assert exported.category is PortableResultCategory.COMPLETED
    assert source_store.calls == []

    destination_registry = FakeRegistry()
    destination_store = FakePortableStore()
    imported = _coordinator(
        destination_registry,
        destination_store,
        receipt=IMPORT_RECEIPT,
        at=LATER,
    ).import_bundle(
        ImportBundleRequest(
            "destination", "test", StorageBackend.SQLITE, "import-seeded"
        ),
        BytesIO(artifact.getvalue()),
    )
    assert imported.category is PortableResultCategory.COMPLETED
    assert imported.service is not None
    assert imported.service.design_state == "seeded"
    assert imported.service.storage is None
    assert destination_store.calls == []


def test_import_reobserves_post_stage_fault_and_publishes_only_after_exact_match() -> None:
    artifact = _export_bundle()
    registry = FakeRegistry()
    store = FakePortableStore()
    store.fail_after_stage = True
    request = ImportBundleRequest(
        "destination", "test", StorageBackend.SQLITE, "import-1"
    )
    first = _coordinator(
        registry, store, receipt=IMPORT_RECEIPT, at=LATER
    ).import_bundle(request, BytesIO(artifact))
    assert first.category is PortableResultCategory.COMPLETED
    assert registry.items[SERVICE_ID].principal_id == "destination"
    assert SERVICE_ID not in registry.import_reservations
    assert store.staged


def test_import_final_commit_uncertainty_remains_invisible_and_replays() -> None:
    artifact = _export_bundle()
    registry = FakeRegistry()
    store = FakePortableStore()
    store.registry = registry
    request = ImportBundleRequest(
        "destination", "test", StorageBackend.SQLITE, "import-commit"
    )
    first = _coordinator(
        registry, store, receipt=IMPORT_RECEIPT, at=LATER
    ).import_bundle(request, BytesIO(artifact))
    assert first.category is PortableResultCategory.OPERATION_UNCERTAIN
    assert SERVICE_ID not in registry.items
    assert SERVICE_ID in registry.import_reservations

    registry.fail_at = None
    store.registry = None
    second = _coordinator(
        registry, store, receipt=IMPORT_RECEIPT, at=LATER
    ).import_bundle(request, BytesIO(artifact))
    assert second.category is PortableResultCategory.COMPLETED
    assert registry.items[SERVICE_ID].principal_id == "destination"


def test_import_restart_after_prepared_boundary_stages_and_publishes() -> None:
    artifact = _export_bundle()
    registry = FakeRegistry()
    store = FakePortableStore()
    store.fail_before_stage_once = True
    request = ImportBundleRequest(
        "destination", "test", StorageBackend.SQLITE, "import-prepared"
    )
    first = _coordinator(
        registry, store, receipt=IMPORT_RECEIPT, at=LATER
    ).import_bundle(request, BytesIO(artifact))
    assert first.category is PortableResultCategory.OPERATION_UNCERTAIN
    assert SERVICE_ID in registry.import_reservations
    assert SERVICE_ID not in registry.items

    second = _coordinator(
        registry, store, receipt=IMPORT_RECEIPT, at=LATER
    ).import_bundle(request, BytesIO(artifact))
    assert second.category is PortableResultCategory.COMPLETED
    assert SERVICE_ID in registry.items


def test_import_restart_after_publish_call_fault_keeps_service_invisible() -> None:
    artifact = _export_bundle()
    registry = FakeRegistry()
    registry.fail_at = "publish_import"
    store = FakePortableStore()
    request = ImportBundleRequest(
        "destination", "test", StorageBackend.SQLITE, "import-publish"
    )
    first = _coordinator(
        registry, store, receipt=IMPORT_RECEIPT, at=LATER
    ).import_bundle(request, BytesIO(artifact))
    assert first.category is PortableResultCategory.OPERATION_UNCERTAIN
    assert SERVICE_ID not in registry.items
    assert store.staged

    registry.fail_at = None
    second = _coordinator(
        registry, store, receipt=IMPORT_RECEIPT, at=LATER
    ).import_bundle(request, BytesIO(artifact))
    assert second.category is PortableResultCategory.COMPLETED
    assert SERVICE_ID in registry.items


def test_changed_destination_stage_becomes_durable_recovery_required() -> None:
    artifact = _export_bundle()
    registry = FakeRegistry()
    store = FakePortableStore(members=_members()[:-1])
    result = _coordinator(
        registry, store, receipt=IMPORT_RECEIPT, at=LATER
    ).import_bundle(
        ImportBundleRequest(
            "destination", "test", StorageBackend.SQLITE, "import-different"
        ),
        BytesIO(artifact),
    )
    assert result.category is PortableResultCategory.RECOVERY_REQUIRED
    assert SERVICE_ID not in registry.items


def test_operational_transition_reobserves_post_commit_fence_fault() -> None:
    registry = FakeRegistry()
    registry.items[SERVICE_ID] = replace(
        _service(), operational_status="active"
    )
    store = FakePortableStore(fence=WriteAdmissionFence(1, "open"))
    store.fail_after_transition = True
    result = _coordinator(registry, store).transition(
        SuspendCommand(PRINCIPAL, "test", SERVICE_ID, 3, "suspend-1")
    )
    assert result.category is PortableResultCategory.COMPLETED
    assert registry.items[SERVICE_ID].operational_status == "suspended"
    assert store.fence == WriteAdmissionFence(2, "closed")


@pytest.mark.parametrize(
    ("command_type", "source_status", "source_fence", "target_status", "target_fence"),
    [
        (SuspendCommand, "active", "open", "suspended", "closed"),
        (ResumeCommand, "suspended", "closed", "active", "open"),
        (ArchiveCommand, "active", "open", "archived", "closed"),
        (ArchiveCommand, "suspended", "closed", "archived", "closed"),
        (RestoreCommand, "archived", "closed", "suspended", "closed"),
    ],
)
def test_materialised_operational_matrix_coordinates_registry_and_fence(
    command_type,  # type: ignore[no-untyped-def]
    source_status: str,
    source_fence: str,
    target_status: str,
    target_fence: str,
) -> None:
    registry = FakeRegistry()
    registry.items[SERVICE_ID] = replace(
        _service(), operational_status=source_status
    )
    store = FakePortableStore(fence=WriteAdmissionFence(1, source_fence))  # type: ignore[arg-type]
    result = _coordinator(registry, store).transition(
        command_type(PRINCIPAL, "test", SERVICE_ID, 3, f"{target_status}-1")
    )
    assert result.category is PortableResultCategory.COMPLETED
    assert registry.items[SERVICE_ID].operational_status == target_status
    assert store.fence.state == target_fence


def test_export_fence_change_never_emits_and_marks_recovery_required() -> None:
    registry = FakeRegistry()
    registry.items[SERVICE_ID] = _service()
    store = FakePortableStore(members=_members())
    store.change_after_snapshot = True
    destination = BytesIO()
    result = _coordinator(registry, store).export(
        ExportCommand(PRINCIPAL, "test", SERVICE_ID, 3, "export-race"),
        destination,
    )
    assert result.category is PortableResultCategory.RECOVERY_REQUIRED
    assert destination.getvalue() == b""


@pytest.mark.parametrize("use_receipt", [False, True])
def test_purge_override_or_verified_receipt_removes_store_and_leaves_tombstone(
    use_receipt: bool,
) -> None:
    registry = FakeRegistry()
    archived = replace(_service(), operational_status="archived")
    registry.items[SERVICE_ID] = archived
    store = FakePortableStore(members=_members())
    if use_receipt:
        exported = _coordinator(registry, store).export(
            ExportCommand(PRINCIPAL, "test", SERVICE_ID, 3, "export-for-purge"),
            BytesIO(),
        )
        assert exported.receipt is not None
        authority = PurgeAuthority(
            PurgeAuthorityKind.VERIFIED_RECEIPT, exported.receipt.receipt_id
        )
    else:
        authority = PurgeAuthority(PurgeAuthorityKind.EXPLICIT_OVERRIDE)
    command = PurgeCommand(
        PRINCIPAL, "test", SERVICE_ID, 3, "purge-1", authority
    )
    purged = _coordinator(registry, store, at=LATER).purge(command)
    assert purged.category is PortableResultCategory.COMPLETED
    assert purged.tombstone is not None
    assert SERVICE_ID not in registry.items
    assert SERVICE_ID in registry.tombstones
    assert store.absent

    replay = _coordinator(registry, store, at=LATER).purge(command)
    assert replay.category is PortableResultCategory.COMPLETED
    assert replay.tombstone == purged.tombstone
    assert store.purge_calls == 1


def test_semantically_unknown_service_field_is_rejected_before_registry_access() -> None:
    encoder = PortableBundleEncoder()
    lines = [
        encoder.add(
            "header",
            PortableHeader(
                1,
                "aipcs-json-c14n-1",
                "aipcs-bundle-limits-1",
                "aipcs-mcp",
                "0.0.0",
                "1.2.0",
                "2026-07-24T12:00:00.000000Z",
                "sqlite",
            ).payload(),
        ),
        encoder.add("service", {"unexpected": True}),
    ]
    trailer, _summary = encoder.finish()
    lines.append(trailer)
    registry = FakeRegistry()
    result = _coordinator(registry, FakePortableStore()).import_bundle(
        ImportBundleRequest(
            "destination", "test", StorageBackend.SQLITE, "invalid"
        ),
        BytesIO(b"".join(lines)),
    )
    assert result.category is PortableResultCategory.MALFORMED_INPUT
    assert registry.uows == []
