"""Backend-neutral logical fixture and assertions for V1-10 transfer."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID, uuid5

from fixtures import entity

from aipcs_mcp.application.models import MaterialisationStorage, Service
from aipcs_mcp.application.portable import ImportBundleRequest, PortableResultCategory
from aipcs_mcp.application.portable_codec import (
    OrderedBundleDigests,
    decode_canonical_json_object,
    decode_non_trailer_frame,
    encode_non_trailer_frame,
    encode_trailer,
)
from aipcs_mcp.application.portable_models import PortableFrame, PortableJsonObject
from aipcs_mcp.application.portable_store import (
    BranchMembership,
    PortableStoreMember,
    WriteAdmissionFence,
    portable_member_sort_key,
)
from aipcs_mcp.application.ports import PortableServiceStore, UowFactory
from aipcs_mcp.application.registry_authority import (
    ExportCommand,
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

AT = datetime(2026, 7, 24, 12, tzinfo=UTC)
LATER = datetime(2026, 7, 24, 12, 1, tzinfo=UTC)
FENCE = WriteAdmissionFence(7, "closed")


class FixedClock:
    def now(self) -> datetime:
        return LATER


@dataclass(frozen=True, slots=True)
class PortableInstallation:
    backend: StorageBackend
    principal: str
    uows: UowFactory
    store: PortableServiceStore
    coordinator: PortableCoordinator


@dataclass(frozen=True, slots=True)
class LogicalFixture:
    service: Service
    members: tuple[PortableStoreMember, ...]
    fence: WriteAdmissionFence = FENCE

    def bind(self, installation: PortableInstallation) -> LogicalFixture:
        return LogicalFixture(
            replace(
                self.service,
                principal_id=installation.principal,
                storage=MaterialisationStorage(
                    installation.backend.value,
                    f"svc_{self.service.service_id.hex}",
                ),
            ),
            self.members,
            self.fence,
        )


def logical_fixture(service_id: UUID, domain_name: str) -> LogicalFixture:
    manifest = ManifestV2.model_validate(
        {
            "manifest_version": 2,
            "schema_version": 1,
            "entities": [
                entity(
                    "memory",
                    [
                        {"name": "title", "type": "string", "required": True},
                        {"name": "rank", "type": "integer", "required": True},
                        {"name": "weight", "type": "number", "required": True},
                        {"name": "confirmed", "type": "boolean", "required": True},
                        {"name": "observed_at", "type": "datetime", "required": True},
                        {"name": "authority_id", "type": "uuid", "required": True},
                        {"name": "tags", "type": "string_list", "required": True},
                    ],
                )
            ],
            "relationships": [],
            "indices": [],
            "query_patterns": ["Find memory by title and tags."],
            "discovery_facets": [{"entity": "memory", "field": "title"}],
            "migration_history": [],
            "retrieval_guidance": "Prefer confirmed memories with explicit authority.",
        }
    )
    current_id = uuid5(service_id, "current")
    deleted_id = uuid5(service_id, "deleted")
    authority_id = uuid5(service_id, "authority")
    parent_id = uuid5(service_id, "parent-branch")
    child_id = uuid5(service_id, "child-branch")
    created_fields = FrozenJsonObject.from_mapping(
        {
            "title": "Initial memory",
            "rank": 1,
            "weight": 1.25,
            "confirmed": False,
            "observed_at": "2026-07-24T12:00:00Z",
            "authority_id": str(authority_id),
            "tags": ["portable", "initial"],
        }
    )
    current_fields = FrozenJsonObject.from_mapping(
        {
            "title": "Portable memory",
            "rank": 2,
            "weight": 2.5,
            "confirmed": True,
            "observed_at": "2026-07-24T12:01:00Z",
            "authority_id": str(authority_id),
            "tags": ["portable", "verified"],
        }
    )
    deleted_fields = FrozenJsonObject.from_mapping(
        {
            "title": "Retired memory",
            "rank": 3,
            "weight": 3.75,
            "confirmed": True,
            "observed_at": "2026-07-24T11:00:00Z",
            "authority_id": str(authority_id),
            "tags": ["retired"],
        }
    )
    members = (
        PortableStoreMember(
            "record",
            RecordValue(
                "memory", current_id, 2, AT, LATER, "portable-fixture", current_fields
            ),
        ),
        PortableStoreMember(
            "history",
            RecordHistoryEvent(
                "memory", current_id, 1, "create", AT, None, created_fields
            ),
        ),
        PortableStoreMember(
            "history",
            RecordHistoryEvent(
                "memory", current_id, 2, "update", LATER, created_fields, current_fields
            ),
        ),
        PortableStoreMember(
            "history",
            RecordHistoryEvent(
                "memory", deleted_id, 1, "create", AT, None, deleted_fields
            ),
        ),
        PortableStoreMember(
            "history",
            RecordHistoryEvent(
                "memory", deleted_id, 2, "delete", LATER, deleted_fields, None
            ),
        ),
        PortableStoreMember(
            "branch",
            BranchValue(
                parent_id,
                "research",
                "Research",
                "Durable research memory",
                "topic",
                None,
                "active",
                "Portable research",
                AT,
                LATER,
                2,
            ),
        ),
        PortableStoreMember(
            "branch",
            BranchValue(
                child_id,
                "portable",
                "Portable",
                "Cross-backend memory",
                "topic",
                parent_id,
                "active",
                "Backend-neutral state",
                AT,
                LATER,
                1,
            ),
        ),
        PortableStoreMember(
            "branch_membership",
            BranchMembership(child_id, "memory", current_id, "primary"),
        ),
        PortableStoreMember(
            "branch_membership",
            BranchMembership(parent_id, "memory", current_id, "related"),
        ),
    )
    ordered = tuple(sorted(members, key=portable_member_sort_key))
    service = Service(
        service_id,
        "fixture-owner",
        domain_name,
        "project",
        "Cross-backend portable fixture",
        AT,
        LATER,
        LATER,
        manifest,
        1,
        "materialised",
        "suspended",
        5,
        AT,
        MaterialisationStorage("sqlite", f"svc_{service_id.hex}"),
    )
    return LogicalFixture(service, ordered)


def install(
    installation: PortableInstallation, fixture: LogicalFixture
) -> LogicalFixture:
    bound = fixture.bind(installation)
    uow = installation.uows()
    try:
        uow.services.add(bound.service)
        uow.commit()
    finally:
        uow.close()
    assert installation.store.stage(
        bound.service, bound.members, bound.fence
    ).status == "staged"
    return bound


def export_bundle(
    installation: PortableInstallation,
    fixture: LogicalFixture,
    *,
    key: str,
) -> bytes:
    destination = BytesIO()
    result = installation.coordinator.export(
        ExportCommand(
            installation.principal,
            "portable-conformance",
            fixture.service.service_id,
            fixture.service.service_revision,
            key,
        ),
        destination,
    )
    assert result.category is PortableResultCategory.COMPLETED
    assert result.artifact_written
    artifact = destination.getvalue()
    assert installation.principal.encode() not in artifact
    assert b'"principal' not in artifact
    assert b"svc_" not in artifact
    return artifact


def transfer(
    source: PortableInstallation,
    destination: PortableInstallation,
    fixture: LogicalFixture,
    *,
    key: str,
) -> tuple[LogicalFixture, bytes]:
    source_bound = fixture.bind(source)
    before_service = service(source, fixture.service.service_id)
    assert before_service == source_bound.service
    assert source.store.observe(
        source_bound.service, source_bound.fence, source_bound.members
    )
    artifact = export_bundle(source, source_bound, key=f"export-{key}")
    imported = destination.coordinator.import_bundle(
        ImportBundleRequest(
            destination.principal,
            "portable-conformance",
            destination.backend,
            f"import-{key}",
        ),
        BytesIO(artifact),
    )
    assert imported.category is PortableResultCategory.COMPLETED
    destination_bound = fixture.bind(destination)
    assert imported.service == destination_bound.service
    assert service(destination, fixture.service.service_id) == destination_bound.service
    assert destination.store.observe(
        destination_bound.service,
        destination_bound.fence,
        destination_bound.members,
    )
    assert service(source, fixture.service.service_id) == before_service
    assert source.store.observe(
        source_bound.service, source_bound.fence, source_bound.members
    )
    return destination_bound, artifact


def service(installation: PortableInstallation, service_id: UUID) -> Service | None:
    uow = installation.uows()
    try:
        result = uow.services.get(installation.principal, service_id)
        uow.rollback()
        return result
    finally:
        uow.close()


def rewrite_payload(
    artifact: bytes,
    kind: str,
    update: dict[str, object],
) -> bytes:
    """Rebuild a canonical/digest-valid artifact with one hostile payload change."""

    digests = OrderedBundleDigests()
    lines: list[bytes] = []
    changed = False
    for exact in artifact.splitlines(keepends=True):
        decoded = decode_canonical_json_object(exact[:-1])
        if decoded.get("kind") == "trailer":
            break
        frame = decode_non_trailer_frame(decoded)
        payload = frame.payload.to_dict()
        if not changed and frame.kind == kind:
            payload.update(update)
            changed = True
        line = encode_non_trailer_frame(
            frame.kind, frame.ordinal, PortableJsonObject.from_mapping(payload)
        )
        encoded = decode_canonical_json_object(line[:-1])
        rebuilt = PortableFrame(
            frame.kind,
            frame.ordinal,
            PortableJsonObject.from_mapping(payload),
            encoded["sha256"],  # type: ignore[arg-type]
        )
        digests.add(rebuilt, line)
        lines.append(line)
    assert changed
    lines.append(encode_trailer(digests.trailer()))
    return b"".join(lines)
