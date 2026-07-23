"""Focused tests for the pure generic-record and discovery vocabulary."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import get_type_hints
from uuid import UUID

import pytest
from fixtures import valid_manifest

from aipcs_mcp.data_store import MaterialisedDataStore
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.numeric_domain import (
    IEEE_754_EXACT_INTEGER_MAX,
    SIGNED_64_MAX,
    SIGNED_64_MIN,
)
from aipcs_mcp.records import (
    MAX_SEARCH_FILTERS,
    BranchAssignmentTarget,
    CreateBranchCommand,
    CreateRecordCommand,
    FrozenJsonObject,
    MaintenanceQuery,
    PageRequest,
    RecordContractError,
    RecordFilter,
    SearchRecordsQuery,
    compile_record_specification,
)
from aipcs_mcp.storage import ServiceStoreLocator


def _manifest() -> ManifestV2:
    payload = valid_manifest()
    payload["entities"][0]["attributes"].extend(
        [
            {
                "name": "state",
                "type": "string",
                "allowed_values": ["open", "closed"],
            },
            {
                "name": "labels",
                "type": "string_list",
                "allowed_values": ["v1", "public"],
                "retrieval_mode": "membership",
            },
            {"name": "note", "type": "string", "retrieval_mode": "annotation"},
            {"name": "count", "type": "integer"},
            {"name": "score", "type": "number"},
        ]
    )
    payload["relationships"] = []
    payload["discovery_facets"] = [
        {"entity": "project", "field": "state"},
        {"entity": "project", "field": "labels"},
    ]
    return ManifestV2.model_validate(payload)


def test_record_specification_detaches_manifest_metadata_and_validates_domain_payloads() -> None:
    manifest = _manifest()
    specification = compile_record_specification(manifest)

    project = specification.entity("project")
    assert project is not None
    assert project.field("state").allowed_values == ("open", "closed")  # type: ignore[union-attr]
    assert project.field("labels").filter_mode == "membership"  # type: ignore[union-attr]
    assert project.field("labels").allowed_values == ("v1", "public")  # type: ignore[union-attr]
    assert project.field("note").filter_mode == "none"  # type: ignore[union-attr]
    assert specification.query_patterns == ("Find projects by owner.",)
    assert specification.retrieval_guidance == "Use exact owner filters before listing."
    assert specification.discovery_facets[0].filter_mode == "membership"
    assert specification.discovery_facets[1].filter_mode == "exact"

    created = specification.validate_create(
        "project", {"title": "A project", "state": "open", "labels": ["v1", "public"]}
    )
    assert created.to_dict() == {
        "labels": ["v1", "public"],
        "state": "open",
        "title": "A project",
    }
    with pytest.raises(RecordContractError):
        specification.validate_create("project", {"title": "A project", "state": "invalid"})
    assert specification.validate_create("project", {"title": "A project", "note": None}).to_dict()[
        "note"
    ] is None
    with pytest.raises(RecordContractError):
        specification.validate_updates("project", {"record_version": 2})
    assert specification.validate_updates("project", {"labels": ["public"]}).to_dict() == {
        "labels": ["public"]
    }
    for labels in (["private"], ["v1", "private"]):
        with pytest.raises(RecordContractError):
            specification.validate_create("project", {"title": "A project", "labels": labels})
        with pytest.raises(RecordContractError):
            specification.validate_updates("project", {"labels": labels})
    with pytest.raises(RecordContractError):
        specification.validate_filter("project", "note", "private")


def test_values_and_query_bounds_are_immutable_and_fail_closed() -> None:
    payload = FrozenJsonObject.from_mapping({"nested": {"values": ["a"]}})
    assert payload.to_dict() == {"nested": {"values": ["a"]}}
    direct_payload = FrozenJsonObject((("values", ["a"]),))
    assert type(direct_payload["values"]) is tuple
    with pytest.raises(FrozenInstanceError):
        payload.entries = ()  # type: ignore[misc]

    identifier = UUID("00000000-0000-0000-0000-000000000001")
    command = CreateRecordCommand("project", FrozenJsonObject.from_mapping({"title": "A"}), "key")
    assert command.idempotency_key == "key"
    filters = tuple(RecordFilter("project", f"field_{index}", "exact", index) for index in range(17))
    with pytest.raises(RecordContractError):
        SearchRecordsQuery("project", filters)
    assert MAX_SEARCH_FILTERS == 16
    with pytest.raises(RecordContractError):
        BranchAssignmentTarget("project", identifier, 0)
    with pytest.raises(RecordContractError):
        PageRequest(limit=101)
    assert MaintenanceQuery(("stale",)).stale_after_days == 90
    with pytest.raises(RecordContractError):
        MaintenanceQuery(("stale_age",))


def test_branch_slug_contract_is_one_consistent_three_to_sixty_four_rule() -> None:
    valid = "a" + "b" * 63
    assert CreateBranchCommand(
        valid, "Boundary", "Intent", None, None, None, "valid-slug"
    ).slug == valid
    for invalid in ("ab", "a" + "b" * 64, "starts-valid-", "Uppercase"):
        with pytest.raises(RecordContractError):
            CreateBranchCommand(
                invalid, "Boundary", "Intent", None, None, None, "invalid-slug"
            )


def test_numeric_domains_are_bounded_and_preserve_exact_typed_filters() -> None:
    specification = compile_record_specification(_manifest())

    for value in (SIGNED_64_MIN, SIGNED_64_MAX):
        assert specification.validate_create(
            "project", {"title": "bounded", "count": value}
        ).to_dict()["count"] == value
        assert specification.validate_updates("project", {"count": value}).to_dict()[
            "count"
        ] == value
        assert specification.validate_filter("project", "count", value).value == value
    for value in (SIGNED_64_MIN - 1, SIGNED_64_MAX + 1):
        with pytest.raises(RecordContractError):
            specification.validate_create("project", {"title": "overflow", "count": value})
        with pytest.raises(RecordContractError):
            specification.validate_updates("project", {"count": value})
        with pytest.raises(RecordContractError):
            specification.validate_filter("project", "count", value)

    for value in (-IEEE_754_EXACT_INTEGER_MAX, IEEE_754_EXACT_INTEGER_MAX):
        assert specification.validate_create(
            "project", {"title": "exact", "score": value}
        ).to_dict()["score"] == value
        assert specification.validate_updates("project", {"score": value}).to_dict()[
            "score"
        ] == value
        assert specification.validate_filter("project", "score", value).value == value
    for value in (
        -IEEE_754_EXACT_INTEGER_MAX - 1,
        IEEE_754_EXACT_INTEGER_MAX + 1,
    ):
        with pytest.raises(RecordContractError):
            specification.validate_create("project", {"title": "rounded", "score": value})
        with pytest.raises(RecordContractError):
            specification.validate_updates("project", {"score": value})
        with pytest.raises(RecordContractError):
            specification.validate_filter("project", "score", value)

    largest_real = float.fromhex("0x1.fffffffffffffp+1023")
    assert specification.validate_create(
        "project", {"title": "real", "score": largest_real}
    ).to_dict()["score"] == largest_real


def test_data_store_port_is_locator_scoped_without_location_types() -> None:
    hints = get_type_hints(MaterialisedDataStore.create_record)
    assert hints["locator"] is ServiceStoreLocator
