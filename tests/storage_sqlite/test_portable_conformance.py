"""SQLite-backed C7 transfer, lifecycle, collision, and purge conformance."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from io import BytesIO
from uuid import UUID, uuid4

from storage_contracts.portable_conformance import (
    FixedClock,
    PortableInstallation,
    export_bundle,
    install,
    logical_fixture,
    rewrite_payload,
    service,
    transfer,
)

from aipcs_mcp.application.operational_lifecycle import (
    ArchiveCommand,
    RestoreCommand,
    ResumeCommand,
    SuspendCommand,
)
from aipcs_mcp.application.portable import ImportBundleRequest, PortableResultCategory
from aipcs_mcp.application.registry_authority import (
    ExportCommand,
    PurgeAuthority,
    PurgeAuthorityKind,
    PurgeCommand,
    StorageBackend,
)
from aipcs_mcp.portable_coordinator import PortableCoordinator
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter
from aipcs_mcp.storage.sqlite.portable import SQLitePortableServiceStore


def _installation(root, principal: str) -> PortableInstallation:  # type: ignore[no-untyped-def]
    location = SQLiteLocationPolicy(root)
    registry = SQLiteRegistryAdapter(location)
    assert registry.migrate().status == "ready"
    store = SQLitePortableServiceStore(location, clock=FixedClock().now)
    coordinator = PortableCoordinator(
        registry.open_uow,
        FixedClock(),
        uuid4,
        StorageBackend.SQLITE,
        store,
    )
    return PortableInstallation(
        StorageBackend.SQLITE,
        principal,
        registry.open_uow,
        store,
        coordinator,
    )


def test_sqlite_to_sqlite_round_trip_preserves_complete_logical_state(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    source = _installation(tmp_path / "source", "source-owner")
    destination = _installation(tmp_path / "destination", "destination-owner")
    fixture = logical_fixture(
        UUID("71000000-0000-4000-8000-000000000001"), "sqlite_round_trip"
    )
    install(source, fixture)
    imported, _artifact = transfer(
        source, destination, fixture, key="sqlite-sqlite"
    )
    assert imported.service.principal_id == "destination-owner"
    uow = destination.uows()
    try:
        assert uow.services.get("source-owner", fixture.service.service_id) is None
        uow.rollback()
    finally:
        uow.close()


def test_dry_run_and_hostile_closed_payloads_write_nothing(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    source = _installation(tmp_path / "source", "source-owner")
    fixture = logical_fixture(
        UUID("72000000-0000-4000-8000-000000000001"), "validation"
    )
    installed = install(source, fixture)
    artifact = export_bundle(source, installed, key="validation-export")

    for name, candidate, expected in (
        ("dry-run", artifact, PortableResultCategory.VALIDATED),
        (
            "excluded-principal",
            rewrite_payload(artifact, "service", {"principal_id": "hostile"}),
            PortableResultCategory.MALFORMED_INPUT,
        ),
        (
            "newer-format",
            rewrite_payload(artifact, "header", {"export_format_version": 2}),
            PortableResultCategory.MALFORMED_INPUT,
        ),
    ):
        destination = _installation(tmp_path / name, "destination-owner")
        result = destination.coordinator.import_bundle(
            ImportBundleRequest(
                destination.principal,
                "portable-conformance",
                destination.backend,
                f"import-{name}",
                dry_run=name == "dry-run",
            ),
            BytesIO(candidate),
        )
        assert result.category is expected
        assert service(destination, fixture.service.service_id) is None
        assert destination.store.is_absent(
            replace(
                fixture.bind(destination).service,
                operational_status="archived",
            )
        )


def test_domain_and_service_collisions_are_bounded_before_staging(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    source = _installation(tmp_path / "source", "source-owner")
    fixture = logical_fixture(
        UUID("73000000-0000-4000-8000-000000000001"), "collision"
    )
    installed = install(source, fixture)
    artifact = export_bundle(source, installed, key="collision-export")

    domain_destination = _installation(tmp_path / "domain", "destination-owner")
    install(
        domain_destination,
        logical_fixture(
            UUID("73000000-0000-4000-8000-000000000002"), "collision"
        ),
    )
    domain = domain_destination.coordinator.import_bundle(
        ImportBundleRequest(
            domain_destination.principal,
            "portable-conformance",
            domain_destination.backend,
            "import-domain-collision",
        ),
        BytesIO(artifact),
    )
    assert domain.category is PortableResultCategory.IDENTITY_COLLISION

    service_destination = _installation(tmp_path / "service", "destination-owner")
    install(
        service_destination,
        logical_fixture(fixture.service.service_id, "other_domain"),
    )
    identity = service_destination.coordinator.import_bundle(
        ImportBundleRequest(
            service_destination.principal,
            "portable-conformance",
            service_destination.backend,
            "import-service-collision",
        ),
        BytesIO(artifact),
    )
    assert identity.category is PortableResultCategory.IDENTITY_COLLISION


def test_operational_matrix_then_receipt_purge_is_terminal_and_tombstoned(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    installation = _installation(tmp_path / "lifecycle", "owner")
    fixture = logical_fixture(
        UUID("74000000-0000-4000-8000-000000000001"), "lifecycle"
    )
    bound = install(installation, fixture)
    commands = (
        ResumeCommand("owner", "test", bound.service.service_id, 5, "resume"),
        SuspendCommand("owner", "test", bound.service.service_id, 6, "suspend"),
        ArchiveCommand("owner", "test", bound.service.service_id, 7, "archive"),
        RestoreCommand("owner", "test", bound.service.service_id, 8, "restore"),
        ArchiveCommand("owner", "test", bound.service.service_id, 9, "rearchive"),
    )
    for command in commands:
        assert (
            installation.coordinator.transition(command).category
            is PortableResultCategory.COMPLETED
        )
    archived = service(installation, bound.service.service_id)
    assert archived is not None
    assert archived.operational_status == "archived"
    assert archived.service_revision == 10

    output = BytesIO()
    exported = installation.coordinator.export(
        ExportCommand(
            "owner", "test", archived.service_id, 10, "export-before-purge"
        ),
        output,
    )
    assert exported.receipt is not None
    purged = installation.coordinator.purge(
        PurgeCommand(
            "owner",
            "test",
            archived.service_id,
            10,
            "purge",
            PurgeAuthority(
                PurgeAuthorityKind.VERIFIED_RECEIPT,
                exported.receipt.receipt_id,
            ),
        )
    )
    assert purged.category is PortableResultCategory.COMPLETED
    assert purged.tombstone is not None
    assert service(installation, archived.service_id) is None
    assert installation.store.is_absent(archived)
    restarted = SQLiteRegistryAdapter(SQLiteLocationPolicy(tmp_path / "lifecycle"))
    assert restarted.inspect_migration().status == "ready"
    with sqlite3.connect(tmp_path / "lifecycle" / "registry.sqlite") as connection:
        assert connection.execute(
            'SELECT count(*) FROM "aipcs_registry_receipt" WHERE "service_id"=?',
            (str(archived.service_id),),
        ).fetchone() == (0,)
    collision = installation.coordinator.import_bundle(
        ImportBundleRequest(
            "owner", "test", StorageBackend.SQLITE, "reimport-purged"
        ),
        BytesIO(output.getvalue()),
    )
    assert collision.category is PortableResultCategory.IDENTITY_COLLISION


def test_explicit_override_purge_does_not_require_an_export_receipt(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    installation = _installation(tmp_path / "override", "owner")
    fixture = logical_fixture(
        UUID("75000000-0000-4000-8000-000000000001"), "override_purge"
    )
    archived_fixture = replace(
        fixture,
        service=replace(fixture.service, operational_status="archived"),
    )
    bound = install(installation, archived_fixture)
    result = installation.coordinator.purge(
        PurgeCommand(
            "owner",
            "test",
            bound.service.service_id,
            bound.service.service_revision,
            "purge-override",
            PurgeAuthority(PurgeAuthorityKind.EXPLICIT_OVERRIDE),
        )
    )
    assert result.category is PortableResultCategory.COMPLETED
    assert result.tombstone is not None
    assert result.tombstone.receipt_id is None
    assert installation.store.is_absent(bound.service)
