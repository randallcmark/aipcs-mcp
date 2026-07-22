"""SYNTHETIC_FIXTURE. Pure SQLite domain-schema layout compilation tests."""

from __future__ import annotations

import sqlite3
from copy import deepcopy

import pytest
from fixtures import entity, valid_manifest

from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.relational import (
    RelationalEntity,
    RelationalField,
    RelationalSpecification,
    compile_manifest,
)
from aipcs_mcp.storage.errors import StorageContractError
from aipcs_mcp.storage.sqlite.domain_schema_layout import build_domain_schema_layout


def _specification() -> RelationalSpecification:
    payload = valid_manifest()
    payload["entities"] = [
        entity(
            "account",
            [
                {"name": "flag", "type": "boolean", "required": True},
                {"name": "rating", "type": "number", "required": False},
                {"name": "labels", "type": "string_list", "required": False},
            ],
        ),
        entity(
            "project",
            [
                {"name": "account_id", "type": "uuid", "required": False},
                {"name": "manager_id", "type": "uuid", "required": False},
                {"name": "title", "type": "string", "required": True},
            ],
        ),
    ]
    payload["relationships"] = [
        {
            "name": "project_account_fk",
            "from": {"entity": "project", "field": "account_id"},
            "to": {"entity": "account", "field": "id"},
            "on_delete": "restrict",
        },
        {
            "name": "project_manager_fk",
            "from": {"entity": "project", "field": "manager_id"},
            "to": {"entity": "account", "field": "id"},
            "on_delete": "restrict",
        },
    ]
    payload["indices"] = [
        {"name": "account_flag_idx", "entity": "account", "fields": ["flag"]},
        {
            "name": "project_account_title_idx",
            "entity": "project",
            "fields": ["account_id", "title"],
            "unique": True,
        },
    ]
    return compile_manifest(ManifestV2.model_validate(payload))


def test_layout_is_immutable_deterministic_and_matches_fresh_sqlite_signatures() -> None:
    layout = build_domain_schema_layout(_specification())

    assert tuple(table.name for table in layout.tables) == ("account", "project")
    assert tuple(index.name for index in layout.indices) == (
        "account_flag_idx",
        "project_account_title_idx",
    )
    assert layout.ddl == tuple(table.sql for table in layout.tables) + tuple(
        index.sql for index in layout.indices
    )
    assert all("STRICT" in table.sql for table in layout.tables)
    assert all("DEFERRABLE" not in statement for statement in layout.ddl)
    assert all(
        " CHECK " not in statement and " DEFAULT " not in statement for statement in layout.ddl
    )

    with sqlite3.connect(":memory:") as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for statement in layout.ddl:
            connection.execute(statement)
        for table in layout.tables:
            assert connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table.name,)
            ).fetchone() == (table.sql,)
            assert (
                tuple(map(tuple, connection.execute(f'PRAGMA table_xinfo("{table.name}")')))
                == table.xinfo
            )
            assert (
                tuple(map(tuple, connection.execute(f'PRAGMA foreign_key_list("{table.name}")')))
                == table.foreign_keys
            )
            actual_indexes = tuple(
                sorted(
                    (row[1], row[2], row[3], row[4])
                    for row in connection.execute(f'PRAGMA index_list("{table.name}")')
                )
            )
            expected_indexes = tuple(
                sorted(
                    (index.name, index.unique, index.origin, index.partial)
                    for index in (*layout.indices, table.implicit_primary_key_index)
                    if index.table == table.name
                )
            )
            assert actual_indexes == expected_indexes
        for index in layout.all_indices:
            assert (
                tuple(map(tuple, connection.execute(f'PRAGMA index_xinfo("{index.name}")')))
                == index.xinfo
            )


def test_layout_uses_exact_field_mapping_nullability_and_immediate_named_foreign_keys() -> None:
    layout = build_domain_schema_layout(_specification())
    account, project = layout.tables

    assert account.xinfo[-3:] == (
        (6, "flag", "INTEGER", 1, None, 0, 0),
        (7, "rating", "REAL", 0, None, 0, 0),
        (8, "labels", "TEXT", 0, None, 0, 0),
    )
    assert project.sql.endswith(
        'CONSTRAINT "project_account_fk" FOREIGN KEY ("account_id") '
        'REFERENCES "account" ("id") ON DELETE RESTRICT ON UPDATE RESTRICT,\n'
        '    CONSTRAINT "project_manager_fk" FOREIGN KEY ("manager_id") '
        'REFERENCES "account" ("id") ON DELETE RESTRICT ON UPDATE RESTRICT\n) STRICT'
    )
    assert project.foreign_keys == (
        (0, 0, "account", "manager_id", "id", "RESTRICT", "RESTRICT", "NONE"),
        (1, 0, "account", "account_id", "id", "RESTRICT", "RESTRICT", "NONE"),
    )


@pytest.mark.parametrize(
    "forged",
    [
        object(),
        type("SpecificationSubclass", (RelationalSpecification,), {}),
        "not-a-specification",
    ],
)
def test_layout_rejects_nonexact_outer_values(forged: object) -> None:
    value = forged
    if isinstance(forged, type):
        value = forged.__new__(forged)
    with pytest.raises(StorageContractError) as captured:
        build_domain_schema_layout(value)  # type: ignore[arg-type]
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_layout_rejects_forged_nested_values_without_trusting_dataclass_immutability() -> None:
    specification = _specification()
    forged = deepcopy(specification)
    bad_field = object.__new__(RelationalField)
    object.__setattr__(bad_field, "name", "bad")
    object.__setattr__(bad_field, "logical_type", "not-a-logical-type")
    object.__setattr__(bad_field, "required", True)
    object.__setattr__(bad_field, "primary_key", False)
    bad_entity = object.__new__(RelationalEntity)
    object.__setattr__(bad_entity, "name", forged.entities[0].name)
    object.__setattr__(bad_entity, "fields", (bad_field,))
    object.__setattr__(forged, "entities", (bad_entity, *forged.entities[1:]))

    with pytest.raises(StorageContractError) as captured:
        build_domain_schema_layout(forged)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize("collection", ["entities", "relationships", "indices"])
def test_layout_rejects_forged_specification_collection_shapes(collection: str) -> None:
    specification = deepcopy(_specification())
    object.__setattr__(specification, collection, list(getattr(specification, collection)))

    with pytest.raises(StorageContractError):
        build_domain_schema_layout(specification)


def test_layout_rejects_forged_entity_and_index_field_collection_shapes() -> None:
    specification = deepcopy(_specification())
    object.__setattr__(specification.entities[0], "fields", list(specification.entities[0].fields))
    with pytest.raises(StorageContractError):
        build_domain_schema_layout(specification)

    specification = deepcopy(_specification())
    object.__setattr__(specification.indices[0], "fields", list(specification.indices[0].fields))
    with pytest.raises(StorageContractError):
        build_domain_schema_layout(specification)


def test_layout_has_no_semantic_checks_or_defaults_for_allowed_values() -> None:
    payload = valid_manifest()
    payload["entities"][0]["attributes"].append(
        {
            "name": "state",
            "type": "string",
            "required": True,
            "allowed_values": ["open", "closed"],
        }
    )
    layout = build_domain_schema_layout(compile_manifest(ManifestV2.model_validate(payload)))

    assert "CHECK" not in layout.tables[0].sql
    assert "DEFAULT" not in layout.tables[0].sql
