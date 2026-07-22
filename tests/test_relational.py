from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest
from fixtures import entity, valid_manifest

from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.relational import (
    FieldAddition,
    MetadataChange,
    RelationalAdditions,
    RelationalContractError,
    RelationalEntity,
    RelationalField,
    RelationalIndex,
    RelationalRelationship,
    RelationalSpecification,
    RelationalTransition,
    classify_transition,
    compile_manifest,
)


def _manifest(payload: dict[str, object]) -> ManifestV2:
    return ManifestV2.model_validate(payload)


def _evolved(payload: dict[str, object]) -> dict[str, object]:
    result = deepcopy(payload)
    result["schema_version"] = 2
    result["migration_history"] = [
        {"from_schema_version": 1, "to_schema_version": 2, "operations": ["Additive change."]}
    ]
    return result


def test_compile_is_detached_sorted_and_keeps_physical_order() -> None:
    payload = valid_manifest()
    payload["entities"].append(
        entity("task", [{"name": "project_id", "type": "uuid", "required": True}])
    )
    payload["relationships"] = [
        {
            "name": "task_project_fk",
            "from": {"entity": "task", "field": "project_id"},
            "to": {"entity": "project", "field": "id"},
            "on_delete": "restrict",
        }
    ]
    payload["indices"].append(
        {"name": "task_project_idx", "entity": "task", "fields": ["project_id", "owner_id"]}
    )
    payload["entities"].reverse()
    payload["indices"].reverse()
    source = _manifest(payload)

    specification = compile_manifest(source)

    assert [item.name for item in specification.entities] == ["project", "task"]
    assert specification.entities[1].fields[-1].name == "project_id"
    assert specification.indices[1].fields == ("project_id", "owner_id")
    relationship = specification.relationships[0]
    assert (relationship.on_delete, relationship.on_update, relationship.constraint_timing) == (
        "restrict",
        "restrict",
        "immediate",
    )
    source.entities[0].attributes[0].name = "mutated"
    assert specification.entities[0].fields[0].name == "id"


def test_compiled_specification_is_deeply_immutable_and_detached() -> None:
    payload = valid_manifest()
    payload["entities"][0]["attributes"].append(
        {"name": "state", "type": "string", "allowed_values": ["open", "closed"]}
    )
    source = _manifest(payload)

    specification = compile_manifest(source)

    source.entities[0].attributes[-1].name = "changed"
    source.indices[0].fields.append("title")
    with pytest.raises(FrozenInstanceError):
        specification.entities[0].fields[0].name = "changed"
    with pytest.raises(FrozenInstanceError):
        specification.indices[0].fields += (RelationalField("title", "string", True, False),)
    with pytest.raises(FrozenInstanceError):
        specification.entities += ()

    assert [field.name for field in specification.entities[0].fields] == [
        "id",
        "owner_id",
        "created_at",
        "updated_at",
        "created_via",
        "record_version",
        "title",
        "state",
    ]
    assert specification.indices[0].fields == ("owner_id",)


def test_compile_rejects_subclass_forged_and_mutated_models() -> None:
    class ManifestSubclass(ManifestV2):
        pass

    with pytest.raises(RelationalContractError):
        compile_manifest(ManifestSubclass.model_validate(valid_manifest()))

    forged = ManifestV2.model_construct(manifest_version=2, schema_version="bad")
    with pytest.raises(RelationalContractError):
        compile_manifest(forged)

    mutated = _manifest(valid_manifest())
    mutated.schema_version = 0
    with pytest.raises(RelationalContractError):
        compile_manifest(mutated)


def test_specification_equality_includes_version_but_structure_does_not() -> None:
    first = _manifest(valid_manifest())
    second_payload = _evolved(valid_manifest())
    second = _manifest(second_payload)

    first_specification = compile_manifest(first)
    second_specification = compile_manifest(second)

    assert first_specification != second_specification
    assert first_specification.same_structure_as(second_specification)


def test_transition_classifies_new_entity_optional_field_relationship_and_index() -> None:
    current_payload = valid_manifest()
    target_payload = _evolved(current_payload)
    target_payload["entities"][0]["attributes"].append(
        {"name": "summary", "type": "string"}
    )
    target_payload["entities"].append(
        entity("task", [{"name": "project_id", "type": "uuid", "required": True}])
    )
    target_payload["relationships"] = [
        {
            "name": "task_project_fk",
            "from": {"entity": "task", "field": "project_id"},
            "to": {"entity": "project", "field": "id"},
            "on_delete": "restrict",
        }
    ]
    target_payload["indices"].append(
        {"name": "task_project_idx", "entity": "task", "fields": ["project_id"]}
    )

    transition = classify_transition(_manifest(current_payload), _manifest(target_payload))

    assert [item.name for item in transition.additions.entities] == ["task"]
    assert [(item.entity, item.field.name) for item in transition.additions.fields] == [
        ("project", "summary")
    ]
    assert [item.name for item in transition.additions.relationships] == ["task_project_fk"]
    assert [item.name for item in transition.additions.indices] == ["task_project_idx"]
    with pytest.raises(RelationalContractError):
        RelationalTransition()


@pytest.mark.parametrize(
    "change",
    [
        "required_field",
        "relationship_from_existing",
        "index_change",
        "history_not_extension",
        "allowed_values_narrowing",
    ],
)
def test_transition_rejects_non_additive_changes(change: str) -> None:
    current_payload = valid_manifest()
    current_payload["entities"][0]["attributes"].append(
        {"name": "state", "type": "string", "allowed_values": ["open", "closed"]}
    )
    target_payload = _evolved(current_payload)
    if change == "required_field":
        target_payload["entities"][0]["attributes"].append(
            {"name": "title_2", "type": "string", "required": True}
        )
    elif change == "relationship_from_existing":
        target_payload["entities"].append(entity("task"))
        target_payload["entities"][0]["attributes"].append(
            {"name": "task_id", "type": "uuid"}
        )
        target_payload["relationships"] = [
            {
                "name": "project_task_fk",
                "from": {"entity": "project", "field": "task_id"},
                "to": {"entity": "task", "field": "id"},
                "on_delete": "restrict",
            }
        ]
    elif change == "index_change":
        target_payload["indices"][0]["unique"] = True
    elif change == "history_not_extension":
        target_payload["migration_history"][0]["operations"] = ["Different annotation."]
        current_payload["migration_history"] = [
            {"from_schema_version": 1, "to_schema_version": 2, "operations": ["Old."]}
        ]
        current_payload["schema_version"] = 2
        target_payload["schema_version"] = 3
        target_payload["migration_history"].append(
            {"from_schema_version": 2, "to_schema_version": 3, "operations": ["Next."]}
        )
    else:
        target_payload["entities"][0]["attributes"][-1]["allowed_values"] = ["open"]

    with pytest.raises(RelationalContractError):
        classify_transition(_manifest(current_payload), _manifest(target_payload))


def test_metadata_change_and_collection_reordering_are_explicit_and_safe() -> None:
    current_payload = valid_manifest()
    current_payload["entities"][0]["attributes"].append(
        {"name": "state", "type": "string", "allowed_values": ["open", "closed"]}
    )
    target_payload = _evolved(current_payload)
    target_payload["entities"][0]["description"] = "A changed description."
    target_payload["entities"][0]["attributes"][-1]["allowed_values"] = ["closed", "open", "paused"]
    target_payload["query_patterns"].reverse()

    transition = classify_transition(_manifest(current_payload), _manifest(target_payload))

    assert not transition.additions.entities
    assert [change.kind for change in transition.metadata_changes] == [
        "allowed_values_expansion",
        "entity_description",
    ]


def test_transition_classifies_every_metadata_kind_and_ordered_query_facet_changes() -> None:
    current_payload = valid_manifest()
    current_payload["entities"][0]["attributes"].extend(
        [
            {"name": "state", "type": "string", "allowed_values": ["open", "closed"]},
            {"name": "tags", "type": "string_list", "allowed_values": ["bug"]},
        ]
    )
    current_payload["query_patterns"].append("Find projects by state.")
    current_payload["discovery_facets"].append({"entity": "project", "field": "title"})
    target_payload = _evolved(current_payload)
    target_entity = target_payload["entities"][0]
    target_entity["description"] = "Projects with changed metadata."
    target_entity["attributes"][-3]["description"] = "A project title."
    target_entity["attributes"][-3]["retrieval_mode"] = "annotation"
    target_entity["attributes"][-2]["allowed_values"] = ["closed", "open", "paused"]
    target_entity["attributes"][-1]["allowed_values"] = ["bug", "feature"]
    target_payload["retrieval_guidance"] = "Use title annotations before listing."
    target_payload["query_patterns"].reverse()
    target_payload["discovery_facets"].reverse()

    transition = classify_transition(_manifest(current_payload), _manifest(target_payload))

    assert [(change.kind, change.entity, change.field) for change in transition.metadata_changes] == [
        ("allowed_values_expansion", "project", "state"),
        ("allowed_values_expansion", "project", "tags"),
        ("attribute_description", "project", "title"),
        ("discovery_facets", None, None),
        ("entity_description", "project", None),
        ("query_patterns", None, None),
        ("retrieval_guidance", None, None),
        ("retrieval_mode", "project", "title"),
    ]


def test_transition_treats_entity_relationship_and_index_collection_reordering_as_a_noop() -> None:
    current_payload = valid_manifest()
    current_payload["entities"].append(
        entity(
            "task",
            [
                {"name": "project_id", "type": "uuid"},
                {"name": "parent_id", "type": "uuid"},
            ],
        )
    )
    current_payload["relationships"] = [
        {
            "name": "task_project_fk",
            "from": {"entity": "task", "field": "project_id"},
            "to": {"entity": "project", "field": "id"},
            "on_delete": "restrict",
        },
        {
            "name": "task_parent_fk",
            "from": {"entity": "task", "field": "parent_id"},
            "to": {"entity": "task", "field": "id"},
            "on_delete": "restrict",
        },
    ]
    current_payload["indices"].append(
        {"name": "task_owner_idx", "entity": "task", "fields": ["owner_id"]}
    )
    target_payload = _evolved(current_payload)
    target_payload["entities"].reverse()
    target_payload["relationships"].reverse()
    target_payload["indices"].reverse()
    target_payload["entities"][1]["attributes"].append(
        {"name": "summary", "type": "string"}
    )

    transition = classify_transition(_manifest(current_payload), _manifest(target_payload))

    assert [(item.entity, item.field.name) for item in transition.additions.fields] == [
        ("project", "summary")
    ]
    assert not transition.metadata_changes


def test_transition_accepts_new_entity_relationships_to_existing_new_and_self() -> None:
    current_payload = valid_manifest()
    target_payload = _evolved(current_payload)
    target_payload["entities"].extend(
        [
            entity("task", [{"name": "project_id", "type": "uuid", "required": True}]),
            entity("comment", [{"name": "task_id", "type": "uuid"}]),
            entity("node", [{"name": "parent_id", "type": "uuid"}]),
        ]
    )
    target_payload["relationships"] = [
        {
            "name": "task_project_fk",
            "from": {"entity": "task", "field": "project_id"},
            "to": {"entity": "project", "field": "id"},
            "on_delete": "restrict",
        },
        {
            "name": "comment_task_fk",
            "from": {"entity": "comment", "field": "task_id"},
            "to": {"entity": "task", "field": "id"},
            "on_delete": "restrict",
        },
        {
            "name": "node_parent_fk",
            "from": {"entity": "node", "field": "parent_id"},
            "to": {"entity": "node", "field": "id"},
            "on_delete": "restrict",
        },
    ]

    transition = classify_transition(_manifest(current_payload), _manifest(target_payload))

    assert [item.name for item in transition.additions.entities] == ["comment", "node", "task"]
    assert [item.name for item in transition.additions.relationships] == [
        "comment_task_fk",
        "node_parent_fk",
        "task_project_fk",
    ]


@pytest.mark.parametrize(
    "change",
    [
        "none_to_set",
        "set_to_none",
        "narrowing",
        "typed_substitution",
        "entity_removal",
        "entity_rename",
        "field_removal",
        "field_rename",
        "field_type",
        "field_nullability",
        "field_primary_key",
        "field_reorder",
        "relationship_removal",
        "relationship_rename",
        "relationship_source",
        "relationship_target",
        "relationship_delete_policy",
        "index_removal",
        "index_rename",
        "index_fields",
        "index_unique",
    ],
)
def test_transition_rejects_all_remaining_non_additive_changes(change: str) -> None:
    current_payload = valid_manifest()
    current_payload["entities"][0]["attributes"].extend(
        [
            {"name": "state", "type": "string", "allowed_values": ["open", "closed"]},
            {"name": "score", "type": "number", "allowed_values": [1, 1.0]},
            {"name": "task_id", "type": "uuid"},
            {"name": "detail", "type": "string"},
            {"name": "other_task_id", "type": "uuid"},
        ]
    )
    current_payload["entities"].append(entity("task"))
    current_payload["relationships"] = [
        {
            "name": "project_task_fk",
            "from": {"entity": "project", "field": "task_id"},
            "to": {"entity": "task", "field": "id"},
            "on_delete": "restrict",
        }
    ]
    current_payload["indices"].append(
        {"name": "project_state_idx", "entity": "project", "fields": ["state", "title"]}
    )
    target_payload = _evolved(current_payload)
    project = target_payload["entities"][0]
    attributes = project["attributes"]
    by_name = {attribute["name"]: attribute for attribute in attributes}
    if change == "none_to_set":
        by_name["detail"]["allowed_values"] = ["detail"]
    elif change == "set_to_none":
        by_name["state"].pop("allowed_values")
    elif change == "narrowing":
        by_name["state"]["allowed_values"] = ["open"]
    elif change == "typed_substitution":
        by_name["score"]["allowed_values"] = [1.0, 2]
    elif change == "entity_removal":
        target_payload["entities"].pop()
    elif change == "entity_rename":
        target_payload["entities"][1]["name"] = "work"
        target_payload["relationships"][0]["to"]["entity"] = "work"
    elif change == "field_removal":
        attributes.remove(by_name["detail"])
    elif change == "field_rename":
        by_name["detail"]["name"] = "details"
    elif change == "field_type":
        by_name["detail"]["type"] = "integer"
    elif change == "field_nullability":
        by_name["detail"]["required"] = True
    elif change == "field_primary_key":
        by_name["detail"]["primary_key"] = True
        by_name["detail"]["required"] = True
    elif change == "field_reorder":
        detail_index = attributes.index(by_name["detail"])
        attributes[detail_index], attributes[detail_index - 1] = (
            attributes[detail_index - 1],
            attributes[detail_index],
        )
    elif change == "relationship_removal":
        target_payload["relationships"] = []
    elif change == "relationship_rename":
        target_payload["relationships"][0]["name"] = "project_task_relation"
    elif change == "relationship_source":
        target_payload["relationships"][0]["from"]["field"] = "other_task_id"
    elif change == "relationship_target":
        target_payload["relationships"][0]["to"]["entity"] = "project"
    elif change == "relationship_delete_policy":
        target_payload["relationships"][0]["on_delete"] = "cascade"
    elif change == "index_removal":
        target_payload["indices"].pop()
    elif change == "index_rename":
        target_payload["indices"][-1]["name"] = "project_state_other_idx"
    elif change == "index_fields":
        target_payload["indices"][-1]["fields"].reverse()
    else:
        target_payload["indices"][-1]["unique"] = True

    with pytest.raises((RelationalContractError, ValueError)):
        classify_transition(_manifest(current_payload), _manifest(target_payload))


def test_transition_accepts_typed_numeric_and_string_list_allowed_value_expansion() -> None:
    current_payload = valid_manifest()
    current_payload["entities"][0]["attributes"].extend(
        [
            {"name": "score", "type": "number", "allowed_values": [1]},
            {"name": "tags", "type": "string_list", "allowed_values": ["bug"]},
        ]
    )
    target_payload = _evolved(current_payload)
    target_payload["entities"][0]["attributes"][-2]["allowed_values"] = [1, 1.0]
    target_payload["entities"][0]["attributes"][-1]["allowed_values"] = ["bug", "feature"]

    transition = classify_transition(_manifest(current_payload), _manifest(target_payload))

    assert [(change.kind, change.field) for change in transition.metadata_changes] == [
        ("allowed_values_expansion", "score"),
        ("allowed_values_expansion", "tags"),
    ]


def test_relational_value_objects_reject_invalid_shapes_and_bounds() -> None:
    class StringSubclass(str):
        pass

    valid_field = RelationalField("title", "string", True, False)
    valid_entity = RelationalEntity(
        "project",
        tuple(
            RelationalField(item["name"], item["type"], item.get("required", False), item.get("primary_key", False))
            for item in entity("project")["attributes"]
        )
        + (valid_field,),
    )
    invalid_values = [
        lambda: RelationalField("title", "not_a_type", True, False),
        lambda: RelationalField("title", StringSubclass("string"), True, False),
        lambda: RelationalField("title", "string", "true", False),
        lambda: RelationalEntity("project", [valid_field]),
        lambda: RelationalRelationship("x" * 49, "project", "title", "project", "id", "restrict"),
        lambda: RelationalRelationship("title_fk", "project", "title", "project", "id", "cascade"),
        lambda: RelationalRelationship(
            "title_fk", "project", "title", "project", "id", StringSubclass("restrict")
        ),
        lambda: RelationalRelationship(
            "title_fk",
            "project",
            "title",
            "project",
            "id",
            "restrict",
            on_update=StringSubclass("restrict"),
        ),
        lambda: RelationalRelationship(
            "title_fk",
            "project",
            "title",
            "project",
            "id",
            "restrict",
            constraint_timing=StringSubclass("immediate"),
        ),
        lambda: RelationalIndex("project_title_idx", "project", (), False),
        lambda: RelationalIndex("project_title_idx", "project", ("title",) * 9, False),
        lambda: RelationalSpecification(0, (valid_entity,), (), ()),
        lambda: RelationalSpecification(66, (valid_entity,), (), ()),
        lambda: MetadataChange("entity_description", "project", "title"),
    ]

    for make_value in invalid_values:
        with pytest.raises(RelationalContractError):
            make_value()


def test_transition_factory_rejects_inconsistent_delta_authorities() -> None:
    current = compile_manifest(_manifest(valid_manifest()))
    target_payload = _evolved(valid_manifest())
    target_payload["entities"][0]["attributes"].append({"name": "summary", "type": "string"})
    target = compile_manifest(_manifest(target_payload))
    expected_field = target.entities[0].fields[-1]
    cases = [
        RelationalAdditions((), (), (), ()),
        RelationalAdditions((), (FieldAddition("task", expected_field),), (), ()),
    ]

    for additions in cases:
        with pytest.raises(RelationalContractError):
            RelationalTransition._build(
                current=current,
                target=target,
                additions=additions,
                metadata_changes=(),
            )

    with pytest.raises(RelationalContractError):
        RelationalTransition._build(
            current=current,
            target=target,
            additions=RelationalAdditions((), (FieldAddition("project", expected_field),), (), ()),
            metadata_changes=(MetadataChange("query_patterns"), MetadataChange("discovery_facets")),
        )


def test_transition_factory_rejects_unjustified_metadata_authority() -> None:
    current = compile_manifest(_manifest(valid_manifest()))
    target_payload = _evolved(valid_manifest())
    target_payload["entities"][0]["attributes"].append({"name": "summary", "type": "string"})
    target = compile_manifest(_manifest(target_payload))
    additions = RelationalAdditions(
        (),
        (FieldAddition("project", target.entities[0].fields[-1]),),
        (),
        (),
    )

    with pytest.raises(RelationalContractError):
        RelationalTransition._build(
            current=current,
            target=target,
            additions=additions,
            metadata_changes=(MetadataChange("query_patterns"),),
        )


def test_relational_errors_are_bounded_and_do_not_echo_hostile_values() -> None:
    hostile = "hostile-secret-value" * 100
    forged = ManifestV2.model_construct(manifest_version=2, schema_version=hostile)

    with pytest.raises(RelationalContractError) as compile_error:
        compile_manifest(forged)
    assert str(compile_error.value) == "The relational contract value is invalid."
    assert hostile not in str(compile_error.value)

    current = _manifest(valid_manifest())
    target_payload = _evolved(valid_manifest())
    target_payload["entities"][0]["attributes"].append(
        {"name": "required_hostile", "type": "string", "required": True}
    )
    with pytest.raises(RelationalContractError) as transition_error:
        classify_transition(current, _manifest(target_payload))
    assert str(transition_error.value) == "The relational contract value is invalid."
