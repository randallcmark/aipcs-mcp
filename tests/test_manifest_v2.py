from __future__ import annotations

import pytest
from fixtures import entity, valid_manifest
from pydantic import ValidationError

from aipcs_mcp.manifest_v2 import ManifestV2


def _relationship(
    name: str,
    source_entity: str,
    source_field: str,
    target_entity: str,
) -> dict[str, object]:
    return {
        "name": name,
        "from": {"entity": source_entity, "field": source_field},
        "to": {"entity": target_entity, "field": "id"},
        "on_delete": "restrict",
    }


def _history(schema_version: int, operations: list[object] | None = None) -> list[dict[str, object]]:
    return [
        {
            "from_schema_version": version,
            "to_schema_version": version + 1,
            "operations": operations if operations is not None else [f"upgrade to {version + 1}"],
        }
        for version in range(1, schema_version)
    ]


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


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("entity", "sqlite_project"),
        ("index", "sqlite_project_owner_idx"),
    ],
)
def test_manifest_rejects_sqlite_reserved_entity_and_index_names(kind: str, name: str) -> None:
    manifest = valid_manifest()
    if kind == "entity":
        manifest["entities"][0]["name"] = name
    else:
        manifest["indices"][0]["name"] = name
    with pytest.raises(ValidationError, match="SQLite-reserved"):
        ManifestV2.model_validate(manifest)


def test_manifest_rejects_entity_index_name_collision() -> None:
    manifest = valid_manifest()
    manifest["indices"][0]["name"] = "project"
    with pytest.raises(ValidationError, match="must not collide"):
        ManifestV2.model_validate(manifest)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_manifest_rejects_non_finite_numeric_allowed_values(value: float) -> None:
    manifest = valid_manifest()
    manifest["entities"][0]["attributes"].append(
        {"name": "score", "type": "number", "allowed_values": [value]}
    )
    with pytest.raises(ValidationError, match="must be finite"):
        ManifestV2.model_validate(manifest)


@pytest.mark.parametrize("attribute_type", ["string", "string_list"])
def test_manifest_rejects_nul_in_string_allowed_values(attribute_type: str) -> None:
    manifest = valid_manifest()
    manifest["entities"][0]["attributes"].append(
        {"name": "state", "type": attribute_type, "allowed_values": ["open\x00closed"]}
    )
    with pytest.raises(ValidationError, match="NUL"):
        ManifestV2.model_validate(manifest)


def test_manifest_accepts_complete_migration_history_at_public_bound() -> None:
    manifest = valid_manifest()
    manifest["schema_version"] = 65
    manifest["migration_history"] = _history(65, ["x" * 240] * 32)
    parsed = ManifestV2.model_validate(manifest)
    assert parsed.schema_version == 65
    assert len(parsed.migration_history) == 64


def test_manifest_rejects_schema_version_above_history_capacity() -> None:
    manifest = valid_manifest()
    manifest["schema_version"] = 66
    manifest["migration_history"] = _history(65)
    with pytest.raises(ValidationError):
        ManifestV2.model_validate(manifest)


@pytest.mark.parametrize(
    ("schema_version", "history"),
    [
        (1, _history(2)),
        (2, []),
        (
            2,
            [
                {
                    "from_schema_version": 2,
                    "to_schema_version": 3,
                    "operations": ["wrong origin"],
                }
            ],
        ),
        (
            3,
            [
                {
                    "from_schema_version": 1,
                    "to_schema_version": 2,
                    "operations": ["first"],
                },
                {
                    "from_schema_version": 2,
                    "to_schema_version": 4,
                    "operations": ["skips a version"],
                },
            ],
        ),
    ],
)
def test_manifest_rejects_incomplete_or_discontinuous_history(
    schema_version: int, history: list[dict[str, object]]
) -> None:
    manifest = valid_manifest()
    manifest["schema_version"] = schema_version
    manifest["migration_history"] = history
    with pytest.raises(ValidationError, match="migration_history"):
        ManifestV2.model_validate(manifest)


@pytest.mark.parametrize(
    "operations",
    [
        [],
        [""],
        ["   "],
        ["x" * 241],
        ["operation"] * 33,
        [1],
    ],
)
def test_manifest_rejects_invalid_migration_operation_annotations(
    operations: list[object],
) -> None:
    manifest = valid_manifest()
    manifest["schema_version"] = 2
    manifest["migration_history"] = _history(2, operations)
    with pytest.raises(ValidationError):
        ManifestV2.model_validate(manifest)


def test_manifest_rejects_server_managed_relationship_source() -> None:
    manifest = valid_manifest()
    manifest["entities"].append(entity("task"))
    manifest["relationships"] = [_relationship("task_project_fk", "task", "id", "project")]
    with pytest.raises(ValidationError, match="agent-declared non-primary-key"):
        ManifestV2.model_validate(manifest)


def test_manifest_rejects_more_than_one_relationship_from_same_source_endpoint() -> None:
    manifest = valid_manifest()
    manifest["entities"].extend(
        [
            entity("task", [{"name": "target_id", "type": "uuid"}]),
            entity("team"),
        ]
    )
    manifest["relationships"] = [
        _relationship("task_project_fk", "task", "target_id", "project"),
        _relationship("task_team_fk", "task", "target_id", "team"),
    ]
    with pytest.raises(ValidationError, match="source endpoints must be unique"):
        ManifestV2.model_validate(manifest)


def test_manifest_rejects_required_self_reference() -> None:
    manifest = valid_manifest()
    manifest["entities"][0]["attributes"].append(
        {"name": "parent_id", "type": "uuid", "required": True}
    )
    manifest["relationships"] = [
        _relationship("project_parent_fk", "project", "parent_id", "project")
    ]
    with pytest.raises(ValidationError, match="directed cycle"):
        ManifestV2.model_validate(manifest)


def test_manifest_rejects_required_subcycle_inside_larger_nullable_graph() -> None:
    manifest = valid_manifest()
    manifest["entities"][0]["attributes"].append(
        {"name": "task_id", "type": "uuid", "required": True}
    )
    manifest["entities"].extend(
        [
            entity(
                "task",
                [
                    {"name": "project_id", "type": "uuid", "required": True},
                    {"name": "note_id", "type": "uuid"},
                ],
            ),
            entity("note", [{"name": "project_id", "type": "uuid", "required": True}]),
        ]
    )
    manifest["relationships"] = [
        _relationship("project_task_fk", "project", "task_id", "task"),
        _relationship("task_project_fk", "task", "project_id", "project"),
        _relationship("task_note_fk", "task", "note_id", "note"),
        _relationship("note_project_fk", "note", "project_id", "project"),
    ]
    with pytest.raises(ValidationError, match="directed cycle"):
        ManifestV2.model_validate(manifest)


def test_manifest_accepts_nullable_cycles_and_multiple_distinct_sources() -> None:
    manifest = valid_manifest()
    manifest["entities"][0]["attributes"].extend(
        [
            {"name": "parent_id", "type": "uuid"},
            {"name": "task_id", "type": "uuid", "required": True},
        ]
    )
    manifest["entities"].append(
        entity(
            "task",
            [
                {"name": "project_id", "type": "uuid"},
                {"name": "backup_project_id", "type": "uuid"},
            ],
        )
    )
    manifest["relationships"] = [
        _relationship("project_parent_fk", "project", "parent_id", "project"),
        _relationship("project_task_fk", "project", "task_id", "task"),
        _relationship("task_project_fk", "task", "project_id", "project"),
        _relationship("task_backup_project_fk", "task", "backup_project_id", "project"),
    ]
    parsed = ManifestV2.model_validate(manifest)
    assert [relationship.name for relationship in parsed.relationships] == [
        "project_parent_fk",
        "project_task_fk",
        "task_project_fk",
        "task_backup_project_fk",
    ]
