"""SYNTHETIC_FIXTURE. Synthetic fixture provenance: public contract-only data."""

from __future__ import annotations


def server_fields() -> list[dict[str, object]]:
    return [
        {"name": "id", "type": "uuid", "required": True, "primary_key": True},
        {"name": "owner_id", "type": "string", "required": True},
        {"name": "created_at", "type": "datetime", "required": True},
        {"name": "updated_at", "type": "datetime", "required": True},
        {"name": "created_via", "type": "string", "required": True},
        {"name": "record_version", "type": "integer", "required": True},
    ]


def entity(name: str, attributes: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {"name": name, "attributes": [*server_fields(), *(attributes or [])]}


def valid_manifest() -> dict[str, object]:
    return {
        "manifest_version": 2,
        "schema_version": 1,
        "entities": [entity("project", [{"name": "title", "type": "string", "required": True}])],
        "relationships": [],
        "indices": [{"name": "project_owner_idx", "entity": "project", "fields": ["owner_id"]}],
        "query_patterns": ["Find projects by owner."],
        "discovery_facets": [{"entity": "project", "field": "owner_id"}],
        "retrieval_guidance": "Use exact owner filters before listing.",
        "migration_history": [],
    }


def valid_design_request() -> dict[str, object]:
    return {"service_id": "5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23", "schema": valid_manifest()}


def synthetic_legacy_manifest() -> dict[str, object]:
    return {
        "manifest_version": 1,
        "schema_version": 3,
        "entities": [
            {
                "name": "note",
                "fields": [
                    {"name": "id", "data_type": "uuid", "primary_key": True},
                    {"name": "score", "data_type": "float", "required": False},
                    {"name": "category", "data_type": "string", "required": True},
                ],
            }
        ],
        "relationships": [],
        "indices": [],
        "query_patterns": ["Find notes by category."],
        "discovery_facets": ["note.category"],
        "tool_definitions": [{"name": "private_tool"}],
        "endpoint": "sqlite:////private/path.db",
        "aliases": ["notes"],
        "migration_history": [{"unknown": "private"}],
    }
