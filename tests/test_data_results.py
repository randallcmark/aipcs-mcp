"""Focused public projections for record, topology, discovery, and maintenance values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fixtures import valid_manifest

from aipcs_mcp.data_results import (
    PublicResultProjectionError,
    project_bootstrap,
    project_branch_assignment,
    project_branch_mutation,
    project_branch_page,
    project_maintenance,
    project_record_delete,
    project_record_document,
    project_record_history,
    project_record_mutation,
    project_record_page,
    project_service_summary,
)
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.records import (
    AssignedRecordRef,
    BranchAssignmentOutcome,
    BranchCard,
    BranchMutationOutcome,
    BranchPage,
    BranchValue,
    DeleteRecordOutcome,
    FacetObservation,
    FacetValue,
    FrozenJsonObject,
    HistoryPage,
    MaintenanceCandidate,
    MaintenanceResult,
    PageCursor,
    RecordHistoryEvent,
    RecordMutationOutcome,
    RecordPage,
    RecordValue,
    ServiceSummary,
    compile_record_specification,
)

_RECORD_ID = UUID("00000000-0000-0000-0000-000000000001")
_BRANCH_ID = UUID("00000000-0000-0000-0000-000000000002")
_TIME = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _record() -> RecordValue:
    return RecordValue(
        "project",
        _RECORD_ID,
        2,
        _TIME,
        _TIME,
        "mcp",
        FrozenJsonObject.from_mapping({"title": "Public project", "nested": {"labels": ["v1"]}}),
    )


def _branch() -> BranchValue:
    return BranchValue(
        _BRANCH_ID,
        "release-notes",
        "Release notes",
        "Track public release work.",
        "release_notes",
        None,
        "active",
        "Use when retrieving release work.",
        _TIME,
        _TIME,
        3,
    )


def test_record_branch_and_history_projections_are_explicit_and_detached() -> None:
    record = _record()
    document = project_record_document(record)
    assert document.model_dump(mode="json") == {
        "entity_name": "project",
        "id": str(_RECORD_ID),
        "record_version": 2,
        "created_at": "2026-07-23T12:00:00Z",
        "updated_at": "2026-07-23T12:00:00Z",
        "created_via": "mcp",
        "fields": {"nested": {"labels": ["v1"]}, "title": "Public project"},
    }
    document.fields["nested"]["labels"].append("changed")  # type: ignore[index,union-attr]
    assert record.fields.to_dict()["nested"] == {"labels": ["v1"]}
    assert "owner_id" not in document.model_dump()
    assert "principal_id" not in document.model_dump()

    assert project_record_mutation(RecordMutationOutcome(record, True)).replayed is True
    assert project_record_delete(DeleteRecordOutcome(_RECORD_ID, 3, False)).model_dump(mode="json") == {
        "id": str(_RECORD_ID),
        "record_version": 3,
        "replayed": False,
    }
    assert project_record_page(RecordPage((record,), PageCursor("opaque"))).next_cursor == "opaque"
    history = project_record_history(
        HistoryPage((RecordHistoryEvent("project", _RECORD_ID, 2, "update", _TIME, record.fields, record.fields),), None)
    )
    assert history.events[0].before is not None
    assert history.events[0].before.fields == {"nested": {"labels": ["v1"]}, "title": "Public project"}

    assert project_branch_mutation(BranchMutationOutcome(_branch(), False)).branch.id == _BRANCH_ID
    assert project_branch_page(BranchPage((_branch(),), PageCursor("branch-cursor"))).next_cursor == "branch-cursor"
    assignment = project_branch_assignment(
        BranchAssignmentOutcome((AssignedRecordRef("project", _RECORD_ID, 3),), True)
    )
    assert assignment.model_dump(mode="json") == {
        "records": [{"entity_name": "project", "id": str(_RECORD_ID), "record_version": 3}],
        "replayed": True,
    }


@dataclass(frozen=True)
class _BootstrapCard:
    service_id: UUID
    domain_name: str
    domain_class: str
    intent_description: str
    design_state: str
    operational_status: str
    schema_version: int | None
    recovery_state: str
    entity_names: tuple[str, ...]


def test_discovery_and_maintenance_projections_bound_known_public_shapes() -> None:
    bootstrap = project_bootstrap(
        (
            _BootstrapCard(
                _RECORD_ID,
                "public_release",
                "project",
                "Release memory.",
                "materialised",
                "active",
                1,
                "clear",
                ("project",),
            ),
        ),
        False,
    )
    assert bootstrap.model_dump(mode="json")["services"][0] == {
        "service_id": str(_RECORD_ID),
        "domain_name": "public_release",
        "domain_class": "project",
        "intent": "Release memory.",
        "state": "materialised",
        "operational_status": "active",
        "schema_version": 1,
        "recovery_state": "clear",
        "entities": ["project"],
    }

    manifest = valid_manifest()
    manifest["discovery_facets"] = [{"entity": "project", "field": "title"}]
    specification = compile_record_specification(ManifestV2.model_validate(manifest))
    summary = ServiceSummary(
        "ready",
        (("project", 4),),
        1,
        (FacetObservation("project", "title", (FacetValue("Public project", 4),)),),
        (BranchCard(_branch(), 2, 1),),
        (("project", (_record(),)),),
    )
    result = project_service_summary(summary, specification).model_dump(mode="json")
    assert result["affordances"] == [
        {
            "entity_name": "project",
            "filter_modes": [
                {"field_name": "title", "mode": "exact", "value_shape": "scalar", "facet": True}
            ],
        }
    ]
    assert result["authority_field_availability"] == [
        {
            "entity_name": "project",
            "provenance_fields": [],
            "source_fields": [],
            "scope_fields": [],
            "confidence_field": None,
            "status_field": None,
            "validity_fields": [],
            "supersession_fields": [],
        }
    ]
    assert result["record_counts"] == [{"entity_name": "project", "count": 4}]
    assert result["facets"] == [
        {
            "entity_name": "project",
            "field_name": "title",
            "values": [{"value": "Public project", "count": 4}],
        }
    ]
    assert result["branches"][0]["branch"]["id"] == str(_BRANCH_ID)
    assert result["samples"][0]["records"][0]["fields"]["title"] == "Public project"

    migration = project_service_summary(
        ServiceSummary("migration_required", (("project", 0),), 0, (), (), ()), specification
    ).model_dump(mode="json")
    assert migration["record_counts"] == [{"entity_name": "project", "count": None}]
    assert migration["facets"] is None
    assert migration["unbranched_record_count"] is None

    maintenance = project_maintenance(
        MaintenanceResult(
            (MaintenanceCandidate("stale", "project", _RECORD_ID, FrozenJsonObject.from_mapping({"age_days": 91})),),
            ("expired", "missing_authority"),
        )
    ).model_dump(mode="json")
    assert maintenance == {
        "candidates": [
            {
                "scan_type": "stale",
                "record": {"entity_name": "project", "id": str(_RECORD_ID)},
                "detail": {"age_days": 91},
            }
        ],
        "unavailable_scan_types": ["expired", "missing_authority"],
    }


def test_projections_fail_closed_instead_of_passing_internal_or_hostile_values() -> None:
    with pytest.raises(PublicResultProjectionError):
        project_record_document(object())  # type: ignore[arg-type]
    with pytest.raises(PublicResultProjectionError):
        project_bootstrap((object(),), False)
    with pytest.raises(PublicResultProjectionError):
        project_bootstrap((), "false")


def test_authority_availability_reports_only_declared_conventional_fields() -> None:
    manifest = valid_manifest()
    attributes = manifest["entities"][0]["attributes"]  # type: ignore[index]
    attributes.extend(  # type: ignore[union-attr]
        [
            {"name": "provenance_source", "type": "string"},
            {"name": "confidence", "type": "number"},
            {"name": "status", "type": "string"},
            {"name": "valid_until", "type": "datetime"},
            {"name": "superseded_by", "type": "uuid"},
            {"name": "unrelated", "type": "string"},
        ]
    )
    manifest["discovery_facets"] = [{"entity": "project", "field": "title"}]
    specification = compile_record_specification(ManifestV2.model_validate(manifest))
    result = project_service_summary(
        ServiceSummary("ready", (("project", 0),), 0, (), (), ()), specification
    ).model_dump(mode="json")
    assert result["authority_field_availability"] == [
        {
            "entity_name": "project",
            "provenance_fields": ["provenance_source"],
            "source_fields": [],
            "scope_fields": [],
            "confidence_field": "confidence",
            "status_field": "status",
            "validity_fields": ["valid_until"],
            "supersession_fields": ["superseded_by"],
        }
    ]
