# Agent integration

This guide is vendor-adaptable. Translate the command and argument array into
your MCP client's local stdio configuration format.

The JSON examples use the intended post-publication distribution name. Before
publication, replace the `aipcs` argument following `--from` with a
supplied wheel, source archive, or Git URL as described in the
[quickstart](quickstart.md).

## MCP process

SQLite:

```json
{
  "command": "uvx",
  "args": [
    "--from",
    "aipcs",
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

PostgreSQL uses `aipcs[postgresql]`, `--profile postgresql`, and
`--postgres-dsn-env NAME`. Put the DSN itself in the process environment, not
in agent instructions or tool arguments.

## Streamable HTTP service

The service transport exposes the identical tool catalogue and envelopes as
the local process. Configure an MCP client that supports Streamable HTTP with
the single endpoint URL selected by the operator (for example,
`https://memory.example.test/mcp`); do not add per-tool URLs or embed storage
credentials in agent instructions. The endpoint must be served through the
trusted TLS/authentication gateway described in
[storage and deployment](storage.md). AIPCS's configured principal and MCP
session identifier are not client credentials.

## Recommended agent protocol

1. Call `aipcs_bootstrap` at the beginning of a session. If it shows a
   guidance service, retrieve only the help needed for the current task.
2. Select a relevant service, then call `aipcs_service_summary`.
3. Follow the returned filter modes and retrieval guidance.
4. Persist durable information when it becomes useful; do not wait for
   context exhaustion.
5. Before compaction or handoff, persist decisions, state, evidence, and the
   next action.
6. Retry uncertain mutations with the exact same request and idempotency key.
7. Evolve a schema only when the current model cannot represent durable
   information cleanly.
8. Treat maintenance candidates as prompts for judgment, never automatic
   deletion instructions.

## Bootstrap and retrieval

```json
{"limit": 100}
```

Then:

```json
{
  "service_id": "11111111-2222-4333-8444-555555555555",
  "sample": 0
}
```

Use the summary's declared filter modes. This example combines exact scalar
equality with one membership value:

```json
{
  "service_id": "11111111-2222-4333-8444-555555555555",
  "entity_name": "note",
  "filters": {"status": "active", "tags": "release"},
  "limit": 50
}
```

Do not invent filters beyond the summary's declared modes.

## Persistence and replay

```json
{
  "service_id": "11111111-2222-4333-8444-555555555555",
  "entity_name": "note",
  "record": {
    "title": "Release decision",
    "status": "active",
    "tags": ["release"],
    "detail": "Ship only after the exact release gate passes."
  },
  "idempotency_key": "create-release-decision-001"
}
```

If the response is lost, replay this exact call. For an update, use the latest
returned record version:

```json
{
  "service_id": "11111111-2222-4333-8444-555555555555",
  "entity_name": "note",
  "record_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
  "updates": {"status": "complete"},
  "expected_record_version": 1,
  "idempotency_key": "complete-release-decision-001"
}
```

## Schema evolution

Inspect the current complete manifest and revisions. Construct one complete
target manifest with `schema_version` advanced by exactly one, a continuous
`migration_history`, and only supported additive changes. Then call
`aipcs_service_evolve` with the exact current service and schema revisions.

Do not send SQL, a partial delta, rename/remove instructions, or a schema
inferred without inspecting the current service. See
[Manifest v2](manifest-v2.md).

## Maintenance

Call `aipcs_maintenance_scan` with bounded scan types appropriate to fields the
service actually declares. Review returned candidates, retrieve necessary
records, and decide through ordinary update/delete/branch operations. Never
describe the scan as truth ranking or garbage collection.

## Pre-compaction persistence

Before a context reset, persist only durable information:

- decisions and rationale;
- completed work and evidence;
- unresolved risks or questions;
- current revisions or stable external references when useful; and
- one explicit resume point.

Avoid dumping transcripts, hidden prompts, credentials, temporary logs, or
large working notes into memory. A compact handoff record is more useful than
an unstructured session archive.

See the copyable [AGENTS.md example](../examples/agent-instructions/AGENTS.md)
and the optional [seeded guide service](../examples/agent-instructions/seeded-guide-service.md).
