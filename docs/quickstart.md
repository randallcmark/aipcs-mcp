# Quickstart

This walkthrough starts a local SQLite-backed AIPCS MCP server and reaches a
first persisted memory without cloning the repository.

## Requirements

- Python 3.12 or newer;
- `uv` with `uvx`;
- Linux or macOS; and
- an MCP client that can launch a local stdio server.

The package is pre-release and is not yet published to a package index. The
examples show the intended post-publication name. Until publication, replace
the `aipcs-mcp` value after `--from` with a wheel, source archive, or Git URL
supplied by the project. Do the same for `aipcs-mcp[postgresql]`, retaining the
`postgresql` extra on the selected source form where the installer supports it.

## Configure the MCP process

Choose one stable principal and one private local data root. Reuse both on
every launch that should see the same memory.

```json
{
  "command": "uvx",
  "args": [
    "--from",
    "aipcs-mcp",
    "aipcs",
    "serve",
    "--profile",
    "sqlite",
    "--principal-id",
    "my-agent",
    "--sqlite-data-root",
    "/absolute/private/path/aipcs-data"
  ]
}
```

The data-root parent must be operator-owned and private. AIPCS creates and
checks the data root when `serve` starts; it never searches the working
directory for configuration or data. See [storage](storage.md) before choosing
a shared or unusual location.

After the client starts the process, call `aipcs_server_info`. A ready
persistent server reports the public contract and enabled lifecycle, record,
and discovery features.

## Seed and design a memory service

Call `aipcs_service_seed`:

```json
{
  "domain_name": "project_memory",
  "domain_class": "project",
  "intent_description": "Keep durable project decisions, tasks, and context.",
  "idempotency_key": "seed-project-memory-v1"
}
```

Keep the returned `service_id`. Then call `aipcs_service_design` with that ID
and this minimal manifest:

```json
{
  "service_id": "REPLACE_WITH_SERVICE_ID",
  "idempotency_key": "design-project-memory-v1",
  "schema": {
    "manifest_version": 2,
    "schema_version": 1,
    "entities": [
      {
        "name": "note",
        "attributes": [
          {"name": "id", "type": "uuid", "required": true, "primary_key": true},
          {"name": "owner_id", "type": "string", "required": true},
          {"name": "created_at", "type": "datetime", "required": true},
          {"name": "updated_at", "type": "datetime", "required": true},
          {"name": "created_via", "type": "string", "required": true},
          {"name": "record_version", "type": "integer", "required": true},
          {"name": "title", "type": "string", "required": true},
          {"name": "status", "type": "string", "required": true},
          {
            "name": "tags",
            "type": "string_list",
            "retrieval_mode": "membership"
          },
          {"name": "detail", "type": "string", "retrieval_mode": "annotation"}
        ]
      }
    ],
    "relationships": [],
    "indices": [
      {"name": "note_status_idx", "entity": "note", "fields": ["status"]}
    ],
    "query_patterns": ["Find notes by exact status or tag membership."],
    "discovery_facets": [{"entity": "note", "field": "status"}],
    "retrieval_guidance": "Search exact status or one tag before broad listing.",
    "migration_history": []
  }
}
```

Design validates and stores the schema but does not create domain tables.

## Materialise and persist the first record

The design response contains the current `service_revision` and
`schema_version`. For a newly seeded and designed service they are normally 2
and 1. Use the exact returned values with `aipcs_service_materialise`:

```json
{
  "service_id": "REPLACE_WITH_SERVICE_ID",
  "expected_service_revision": 2,
  "expected_schema_version": 1,
  "idempotency_key": "materialise-project-memory-v1"
}
```

Now call `aipcs_record_create`:

```json
{
  "service_id": "REPLACE_WITH_SERVICE_ID",
  "entity_name": "note",
  "record": {
    "title": "First durable memory",
    "status": "active",
    "tags": ["quickstart"],
    "detail": "AIPCS is connected and persistence is working."
  },
  "idempotency_key": "create-first-note"
}
```

Restart the MCP process with the same principal and data root, then call
`aipcs_record_list`:

```json
{
  "service_id": "REPLACE_WITH_SERVICE_ID",
  "entity_name": "note",
  "limit": 50
}
```

The record should still be present. If a mutation response is lost, retry the
exact same request with the same idempotency key; do not invent a new key until
you know the original request did not commit.

## Next steps

- Learn the [service and record lifecycle](concepts.md).
- Put reusable guidance in your [agent instructions](agent-integration.md).
- Run read-only checks with the [administration CLI](administration.md).
- Plan transfer and recovery using [export and import](backup-and-migration.md).
