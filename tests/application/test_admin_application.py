"""SYNTHETIC_FIXTURE. Read-only administration application contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from aipcs_mcp.application.admin import (
    AdminInspectionApplication,
    MigrationObservation,
    project_doctor,
    project_migration,
    project_status,
)
from aipcs_mcp.application.data import BootstrapResult, BootstrapService
from aipcs_mcp.application.models import ApplicationContext
from aipcs_mcp.contracts import ServiceMetadata
from aipcs_mcp.records import MaintenanceResult

SERVICE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
NOW = datetime(2026, 1, 1, tzinfo=UTC)
CONTEXT = ApplicationContext("principal", "cli")


class Storage:
    def __init__(self, registry_status: str = "ready") -> None:
        self.registry_status = registry_status
        self.calls: list[str] = []

    def inspect_registry(self) -> MigrationObservation:
        self.calls.append("registry")
        revision = 4 if self.registry_status == "ready" else 3
        return MigrationObservation("registry", self.registry_status, revision, 4)  # type: ignore[arg-type]

    def inspect_service(self, storage: object) -> MigrationObservation:
        self.calls.append("service")
        return MigrationObservation("service_store", "ready", 3, 3)

    def inspect_domain(self, storage: object, manifest: object) -> str:
        self.calls.append("domain")
        return "ready"


class Services:
    def __init__(self, service: ServiceMetadata) -> None:
        self.service = service

    def list(self, context: ApplicationContext, limit: int) -> list[ServiceMetadata]:
        assert context == CONTEXT
        assert limit == 25
        return [self.service]

    def inspect(
        self, context: ApplicationContext, service_id: UUID
    ) -> ServiceMetadata:
        assert context == CONTEXT
        assert service_id == SERVICE_ID
        return self.service


class Data:
    def __init__(self, topology: BootstrapResult) -> None:
        self.topology = topology
        self.query: object | None = None

    def bootstrap(self, context: ApplicationContext) -> BootstrapResult:
        assert context == CONTEXT
        return self.topology

    def maintenance_scan(
        self, context: ApplicationContext, service_id: UUID, query: object
    ) -> MaintenanceResult:
        assert context == CONTEXT
        assert service_id == SERVICE_ID
        self.query = query
        return MaintenanceResult((), ())


def _seeded_service() -> ServiceMetadata:
    return ServiceMetadata(
        service_id=SERVICE_ID,
        domain_name="notes",
        domain_class="knowledge",
        intent_description="Keep durable notes.",
        design_state="seeded",
        operational_status="active",
        service_revision=1,
        recovery_state="clear",
        created_at=NOW,
        updated_at=NOW,
        last_activity_at=NOW,
    )


def _topology(*, truncated: bool = False) -> BootstrapResult:
    return BootstrapResult(
        (
            BootstrapService(
                SERVICE_ID,
                "notes",
                "knowledge",
                "Keep durable notes.",
                "seeded",
                "active",
                None,
                "clear",
                (),
            ),
            BootstrapService(
                UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                "archive",
                "knowledge",
                "Keep archived notes.",
                "materialised",
                "archived",
                1,
                "recovery_required",
                ("note",),
            ),
        ),
        truncated,
    )


def test_stateless_status_and_doctor_never_require_storage() -> None:
    application = AdminInspectionApplication("stateless", None, None, None)

    status = project_status(application.status(ApplicationContext("stateless", "cli")))
    assert status == {
        "profile": "stateless",
        "backend": None,
        "registry": None,
        "services": {
            "design": {},
            "operational": {},
            "recovery": {},
            "truncated": False,
        },
        "overall": "unavailable",
    }
    doctor = project_doctor(
        application.doctor(ApplicationContext("stateless", "cli"), None)
    )
    assert doctor["overall"] == "fail"
    assert doctor["checks"][-1]["issue_code"] == "persistent_storage_not_configured"


def test_ready_status_returns_bounded_counts_and_recovery_attention() -> None:
    storage = Storage()
    service = _seeded_service()
    application = AdminInspectionApplication(
        "sqlite", storage, Services(service), Data(_topology(truncated=True))  # type: ignore[arg-type]
    )

    result = project_status(application.status(CONTEXT))

    assert result["services"] == {
        "design": {"materialised": 1, "seeded": 1},
        "operational": {"active": 1, "archived": 1},
        "recovery": {"clear": 1, "recovery_required": 1},
        "truncated": True,
    }
    assert result["overall"] == "attention_required"
    assert storage.calls == ["registry"]


def test_unready_registry_does_not_read_service_topology() -> None:
    class NoData(Data):
        def bootstrap(self, context: ApplicationContext) -> BootstrapResult:
            raise AssertionError("An unready registry must not be opened.")

    storage = Storage("outdated")
    application = AdminInspectionApplication(
        "sqlite",
        storage,
        Services(_seeded_service()),  # type: ignore[arg-type]
        NoData(_topology()),  # type: ignore[arg-type]
    )

    result = project_status(application.status(CONTEXT))

    assert result["overall"] == "attention_required"
    assert result["services"]["design"] == {}  # type: ignore[index]
    assert result["registry"]["issue_code"] == "storage_migration_required"  # type: ignore[index]


def test_service_reads_storage_status_doctor_and_maintenance_are_bounded() -> None:
    storage = Storage()
    service = _seeded_service()
    data = Data(_topology())
    application = AdminInspectionApplication(
        "sqlite", storage, Services(service), data  # type: ignore[arg-type]
    )

    assert application.service_list(CONTEXT, 25) == [service]
    assert application.service_inspect(CONTEXT, SERVICE_ID) == service
    migration = project_migration(application.storage_status(CONTEXT, SERVICE_ID))
    assert migration["status"] == "uninitialised"
    assert migration["issue_code"] == "service_not_materialised"

    doctor = project_doctor(application.doctor(CONTEXT, SERVICE_ID))
    assert doctor["overall"] == "warning"
    assert [check["check_id"] for check in doctor["checks"]] == [
        "configuration",
        "runtime",
        "registry_migration",
        "service_identity",
        "admission_fence",
        "service_store_migration",
        "domain_schema",
    ]

    assert application.maintenance_scan(CONTEXT, SERVICE_ID) == MaintenanceResult((), ())
    assert data.query is not None
    assert len(data.query.scan_types) == 8  # type: ignore[attr-defined]

