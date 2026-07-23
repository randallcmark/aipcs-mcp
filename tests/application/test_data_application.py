from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from fixtures import valid_manifest

from aipcs_mcp.application.data import BootstrapResult, DataApplication, SummaryView
from aipcs_mcp.application.models import (
    ApplicationContext,
    DesignCommand,
    MaterialisationStorage,
    SeedCommand,
)
from aipcs_mcp.application.services import ServiceApplication
from aipcs_mcp.lifecycle import RecoveryState
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.records import (
    CreateRecordCommand,
    DataFailure,
    FrozenJsonObject,
    ServiceSummary,
    SummaryQuery,
)
from application.fakes import FakeRegistry, FixedClock, SequentialIds

PRINCIPAL = "principal-secret"
CONTEXT = ApplicationContext(PRINCIPAL, "mcp")
START = datetime(2026, 1, 1, tzinfo=UTC)


class RecordingGateway:
    def __init__(self, registry: FakeRegistry) -> None:
        self.registry = registry
        self.calls: list[tuple[str, object, object, object]] = []

    def execute(
        self,
        operation: str,
        storage: object,
        principal_id: str,
        created_via: str,
        specification: object,
        value: object,
    ) -> DataFailure:
        assert principal_id == PRINCIPAL
        assert created_via == "mcp"
        assert self.registry.uows[-1].closed
        self.calls.append((operation, storage, specification, value))
        return DataFailure("storage_migration_required")


def _materialised_application(
    manifest: ManifestV2 | None = None,
) -> tuple[DataApplication, FakeRegistry, RecordingGateway, UUID]:
    registry = FakeRegistry()
    lifecycle = ServiceApplication(registry.uow, FixedClock(START), SequentialIds(UUID(int=1)))
    seeded = lifecycle.seed(CONTEXT, SeedCommand("notes", "project", "An intent", "seed"))
    lifecycle.design(
        CONTEXT,
        DesignCommand(
            seeded.service_id,
            manifest or ManifestV2.model_validate(valid_manifest()),
            "design",
        ),
    )
    designed = registry.items[seeded.service_id]
    registry.items[seeded.service_id] = replace(
        designed,
        design_state="materialised",
        materialised_at=designed.updated_at,
        storage=MaterialisationStorage("sqlite", f"svc_{seeded.service_id.hex}"),
    )
    gateway = RecordingGateway(registry)
    return DataApplication(registry.uow, gateway), registry, gateway, seeded.service_id


def test_summary_admission_closes_registry_uow_before_gateway_and_detaches_values() -> None:
    application, registry, gateway, service_id = _materialised_application()
    registry_service = registry.items[service_id]

    result = application.service_summary(CONTEXT, service_id, SummaryQuery())

    assert result == DataFailure("storage_migration_required")
    assert registry.uows[-1].calls == ["get", "recovery_state", "close"]
    assert len(gateway.calls) == 1
    operation, storage, specification, value = gateway.calls[0]
    assert operation == "service_summary" and value == SummaryQuery()
    assert storage == registry_service.storage and storage is not registry_service.storage
    assert specification is not registry_service.manifest
    assert registry.commits == 2 and len(registry.events) == 2


def test_application_admits_string_list_enumerations_as_scalar_member_values() -> None:
    payload = valid_manifest()
    payload["entities"][0]["attributes"].append(
        {
            "name": "labels",
            "type": "string_list",
            "allowed_values": ["public", "release"],
            "retrieval_mode": "membership",
        }
    )
    application, _, gateway, service_id = _materialised_application(
        ManifestV2.model_validate(payload)
    )

    result = application.create_record(
        CONTEXT,
        service_id,
        CreateRecordCommand(
            "project",
            FrozenJsonObject.from_mapping(
                {"title": "Release project", "labels": ["public", "release"]}
            ),
            "enumerated-list",
        ),
    )

    assert result == DataFailure("storage_migration_required")
    assert gateway.calls[-1][0] == "create_record"
    specification = gateway.calls[-1][2]
    assert specification.field("project", "labels").allowed_values == (  # type: ignore[union-attr]
        "public",
        "release",
    )


def test_data_admission_hides_cross_principal_lifecycle_and_unmaterialised_states() -> None:
    application, registry, gateway, service_id = _materialised_application()

    assert application.service_summary(ApplicationContext("other", "mcp"), service_id, SummaryQuery()) == DataFailure(
        "not_found"
    )
    registry.recovery_states[(PRINCIPAL, service_id)] = RecoveryState.PENDING
    assert application.service_summary(CONTEXT, service_id, SummaryQuery()) == DataFailure("storage_busy")
    registry.recovery_states[(PRINCIPAL, service_id)] = RecoveryState.RECOVERY_REQUIRED
    assert application.service_summary(CONTEXT, service_id, SummaryQuery()) == DataFailure(
        "storage_unavailable"
    )
    registry.recovery_states[(PRINCIPAL, service_id)] = RecoveryState.CLEAR
    registry.items[service_id] = replace(
        registry.items[service_id],
        design_state="seeded",
        materialised_at=None,
        storage=None,
    )
    assert application.service_summary(CONTEXT, service_id, SummaryQuery()) == DataFailure(
        "storage_unavailable"
    )
    assert gateway.calls == []


def test_bootstrap_is_registry_only_bounded_and_contains_no_data_gateway_call() -> None:
    application, registry, gateway, service_id = _materialised_application()

    result = application.bootstrap(CONTEXT)

    assert isinstance(result, BootstrapResult)
    assert [card.service_id for card in result.services] == [service_id]
    assert result.services[0].entity_names
    assert registry.uows[-1].calls == ["count", "list", "recovery_state", "close"]
    assert gateway.calls == []


def test_bootstrap_uses_a_count_sentinel_without_exceeding_its_requested_limit() -> None:
    application, registry, _, _ = _materialised_application()
    second_id = UUID(int=2)
    first = next(iter(registry.items.values()))
    registry.items[second_id] = replace(
        first,
        service_id=second_id,
        storage=MaterialisationStorage("sqlite", f"svc_{second_id.hex}"),
    )

    result = application.bootstrap(CONTEXT, limit=1)

    assert isinstance(result, BootstrapResult)
    assert len(result.services) == 1 and result.truncated is True
    assert registry.uows[-1].calls == ["count", "list", "recovery_state", "close"]


def test_summary_view_retains_only_detached_specification_needed_by_public_projection() -> None:
    application, _, gateway, service_id = _materialised_application()

    def summary(
        operation: str, storage: object, principal_id: str, created_via: str, specification: object, value: object
    ) -> ServiceSummary:
        gateway.calls.append((operation, storage, specification, value))
        return ServiceSummary("migration_required", (), 0, (), (), ())

    gateway.execute = summary  # type: ignore[method-assign]
    result = application.service_summary(CONTEXT, service_id, SummaryQuery())

    assert isinstance(result, SummaryView)
    assert result.summary.data_status == "migration_required"
    assert result.specification is gateway.calls[-1][2]
