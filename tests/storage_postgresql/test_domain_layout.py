"""SYNTHETIC_FIXTURE. PostgreSQL domain-layout compiler coverage without a server."""

from __future__ import annotations

import pytest
from storage_contracts.parity_fixtures import evolved_parity_manifest, parity_manifest

from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.relational import classify_transition, compile_manifest
from aipcs_mcp.storage.errors import StorageContractError
from aipcs_mcp.storage.postgresql.domain_schema_layout import (
    build_postgresql_domain_layout,
    build_postgresql_domain_transition_layout,
)

NAMESPACE = "svc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_native_postgresql_layout_is_quoted_deterministic_and_uses_d040_types() -> None:
    layout = build_postgresql_domain_layout(compile_manifest(parity_manifest()), NAMESPACE)

    table, index = layout.ddl
    assert table.startswith(f'CREATE TABLE "{NAMESPACE}"."note"')
    assert f'REFERENCES "{NAMESPACE}"."note" ("id")' in table
    assert 'CONSTRAINT "note_parent_fk"' in table
    assert index == f'CREATE INDEX "note_title_idx" ON "{NAMESPACE}"."note" ("title")'


def test_adjacent_additive_transition_contains_only_create_add_column_and_index_ddl() -> None:
    current = parity_manifest()
    transition = classify_transition(current, evolved_parity_manifest())

    plan = build_postgresql_domain_transition_layout(transition, NAMESPACE)

    assert plan.ddl == (
        f'ALTER TABLE "{NAMESPACE}"."note" ADD COLUMN "summary" text COLLATE "C"',
    )
    assert all("DROP" not in statement and "UPDATE" not in statement for statement in plan.ddl)


def test_d040_native_type_mapping_covers_every_logical_record_type() -> None:
    payload = parity_manifest().model_dump(mode="json", by_alias=True)
    payload["entities"][0]["attributes"].extend(
        [
            {"name": "active", "type": "boolean"},
            {"name": "score", "type": "number"},
        ]
    )
    layout = build_postgresql_domain_layout(
        compile_manifest(ManifestV2.model_validate(payload)), NAMESPACE
    )

    assert dict((name, type_name) for name, type_name, _required, _collation in layout.tables[0].columns) == {
        "id": "uuid",
        "owner_id": "text",
        "created_at": "timestamp with time zone",
        "updated_at": "timestamp with time zone",
        "created_via": "text",
        "record_version": "bigint",
        "title": "text",
        "rank": "bigint",
        "tags": "jsonb",
        "parent_id": "uuid",
        "active": "boolean",
        "score": "double precision",
    }
    assert {name: collation for name, _type_name, _required, collation in layout.tables[0].columns}[
        "title"
    ] == "C"


def test_layout_rejects_forged_or_subclassed_relational_input() -> None:
    specification = compile_manifest(parity_manifest())

    class Subclass(type(specification)):
        pass

    with pytest.raises(StorageContractError):
        build_postgresql_domain_layout(Subclass(  # type: ignore[call-arg]
            specification.schema_version,
            specification.entities,
            specification.relationships,
            specification.indices,
        ), NAMESPACE)
