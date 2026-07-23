"""SYNTHETIC_FIXTURE. Backend-neutral manifests for materialised-store parity."""

# Synthetic fixture provenance: public contract-only manifest data.

from __future__ import annotations

from copy import deepcopy

from fixtures import entity

from aipcs_mcp.manifest_v2 import ManifestV2


def parity_manifest() -> ManifestV2:
    """Return a compact manifest exercising records, discovery, and restrict FKs."""

    return ManifestV2.model_validate(
        {
            "manifest_version": 2,
            "schema_version": 1,
            "entities": [
                entity(
                    "note",
                    [
                        {"name": "title", "type": "string", "required": True},
                        {"name": "rank", "type": "integer"},
                        {"name": "tags", "type": "string_list", "retrieval_mode": "membership"},
                        {"name": "parent_id", "type": "uuid"},
                    ],
                )
            ],
            "relationships": [
                {
                    "name": "note_parent_fk",
                    "from": {"entity": "note", "field": "parent_id"},
                    "to": {"entity": "note", "field": "id"},
                    "on_delete": "restrict",
                }
            ],
            "indices": [{"name": "note_title_idx", "entity": "note", "fields": ["title"]}],
            "query_patterns": ["Find notes by title or tag."],
            "discovery_facets": [
                {"entity": "note", "field": "title"},
                {"entity": "note", "field": "tags"},
            ],
            "retrieval_guidance": "Use exact title and membership tag filters.",
            "migration_history": [],
        }
    )


def evolved_parity_manifest() -> ManifestV2:
    """Return one valid additive evolution from :func:`parity_manifest`."""

    payload = deepcopy(parity_manifest().model_dump(mode="json", by_alias=True))
    payload["schema_version"] = 2
    payload["migration_history"] = [
        {
            "from_schema_version": 1,
            "to_schema_version": 2,
            "operations": ["add optional note summary"],
        }
    ]
    payload["entities"][0]["attributes"].append({"name": "summary", "type": "string"})
    return ManifestV2.model_validate(payload)
