from __future__ import annotations

import pytest
from fixtures import entity, valid_manifest
from pydantic import ValidationError

from aipcs_mcp.manifest_v2 import ManifestV2


def test_manifest_accepts_explicit_facets_indices_and_fk_declarations() -> None:
    manifest = valid_manifest()
    manifest["entities"].append(
        entity("task", [{"name": "project_id", "type": "uuid", "required": True}])
    )
    manifest["relationships"] = [
        {
            "name": "task_project_fk",
            "from": {"entity": "task", "field": "project_id"},
            "to": {"entity": "project", "field": "id"},
            "on_delete": "restrict",
        }
    ]
    manifest["indices"].append(
        {"name": "task_project_idx", "entity": "task", "fields": ["owner_id", "project_id"]}
    )
    parsed = ManifestV2.model_validate(manifest)
    assert parsed.relationships[0].to.field == "id"
    assert parsed.to_public_dict()["relationships"][0]["from"] == {
        "entity": "task",
        "field": "project_id",
    }
    assert parsed.indices[1].fields == ["owner_id", "project_id"]
    default_serialized = parsed.model_dump(mode="json")
    assert "from" in default_serialized["relationships"][0]
    assert "from_" not in default_serialized["relationships"][0]


def test_manifest_requires_exact_server_managed_declarations() -> None:
    manifest = valid_manifest()
    manifest["entities"][0]["attributes"] = [
        item for item in manifest["entities"][0]["attributes"] if item["name"] != "record_version"
    ]
    with pytest.raises(ValidationError, match="record_version"):
        ManifestV2.model_validate(manifest)


@pytest.mark.parametrize(
    "relationship",
    [
        {
            "name": "bad_target",
            "from": {"entity": "project", "field": "id"},
            "to": {"entity": "project", "field": "owner_id"},
            "on_delete": "restrict",
        },
        {
            "name": "bad_delete",
            "from": {"entity": "project", "field": "id"},
            "to": {"entity": "project", "field": "id"},
            "on_delete": "cascade",
        },
    ],
)
def test_manifest_rejects_unsupported_relationship_shapes(
    relationship: dict[str, object],
) -> None:
    manifest = valid_manifest()
    manifest["relationships"].append(relationship)
    with pytest.raises(ValidationError):
        ManifestV2.model_validate(manifest)


def test_manifest_rejects_string_list_index() -> None:
    manifest = valid_manifest()
    manifest["entities"][0]["attributes"].append(
        {"name": "tags", "type": "string_list", "retrieval_mode": "membership"}
    )
    manifest["indices"].append({"name": "bad_list_index", "entity": "project", "fields": ["tags"]})
    with pytest.raises(ValidationError, match="string_list"):
        ManifestV2.model_validate(manifest)


@pytest.mark.parametrize(
    ("attribute_type", "allowed_values"),
    [
        ("string", ["open", "closed"]),
        ("integer", [1, 2]),
        ("number", [1, 2.5]),
        ("boolean", [True, False]),
        ("string_list", ["red", "blue"]),
    ],
)
def test_manifest_accepts_type_compatible_allowed_values(
    attribute_type: str, allowed_values: list[object]
) -> None:
    manifest = valid_manifest()
    manifest["entities"][0]["attributes"].append(
        {"name": "state", "type": attribute_type, "allowed_values": allowed_values}
    )
    ManifestV2.model_validate(manifest)


def test_manifest_rejects_incompatible_allowed_values_and_retrieval_modes() -> None:
    manifest = valid_manifest()
    manifest["entities"][0]["attributes"].append(
        {"name": "score", "type": "integer", "allowed_values": ["high"]}
    )
    with pytest.raises(ValidationError, match="compatible"):
        ManifestV2.model_validate(manifest)

    manifest = valid_manifest()
    manifest["entities"][0]["attributes"].append(
        {"name": "score", "type": "integer", "retrieval_mode": "annotation"}
    )
    with pytest.raises(ValidationError, match="annotation"):
        ManifestV2.model_validate(manifest)


def test_manifest_allows_only_server_id_as_primary_key() -> None:
    manifest = valid_manifest()
    manifest["entities"][0]["attributes"].append(
        {"name": "external_id", "type": "uuid", "required": True, "primary_key": True}
    )
    with pytest.raises(ValidationError, match="only supported primary key"):
        ManifestV2.model_validate(manifest)


def test_manifest_rejects_string_facets_and_unknown_fields() -> None:
    manifest = valid_manifest()
    manifest["discovery_facets"] = ["project.owner_id"]
    with pytest.raises(ValidationError):
        ManifestV2.model_validate(manifest)
    manifest = valid_manifest()
    manifest["tool_definitions"] = []
    with pytest.raises(ValidationError):
        ManifestV2.model_validate(manifest)


def test_manifest_bounds_query_pattern_lengths() -> None:
    manifest = valid_manifest()
    manifest["query_patterns"] = ["x" * 241]
    with pytest.raises(ValidationError):
        ManifestV2.model_validate(manifest)
