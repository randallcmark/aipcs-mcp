# AIPCS MCP

`aipcs-mcp` is a pre-release, local-first persistence foundation for AI agents.
It provides a small, stable MCP surface over an agent-defined relational-memory
schema. The current implementation supports durable service registration and
initial design storage through a local SQLite registry. It also packages a
private SQLite catalog, relational-schema adapter, and transport-neutral
lifecycle coordinator for distribution-level fidelity and restart proof. No
public operation invokes these seams, creates agent-authored tables, or stores
records.

The public runtime provides:

- manifest v2 validation and an explicit, one-way manifest-v1 library converter;
- a strict, redacted configuration model;
- a stateless stdio server for capability discovery; and
- a SQLite stdio profile for principal-scoped service seed, list, inspect, and
  initial-design lifecycle operations.

SQLite support is deliberately narrow: local POSIX filesystems on Linux and
macOS, one host, Python 3.12+, and SQLite 3.51.3+. WAL permits concurrent local
readers and serialises writers through SQLite's one-writer slot with a bounded
configurable wait. This is a same-effective-user trust boundary: busy results
surface as `StorageBusy` and the adapter does not retry them. PostgreSQL,
Windows SQLite, remote MCP, public service-store allocation or materialisation,
record operations, administration, and hosted deployment are not available.

## Public v1 direction

Public v1 is local-first and stdio-only. The server owns generic primitives;
schemas describe records, relationships, indexes, and retrieval intent rather
than generating bespoke MCP tools or services.

## Contract documentation

- [Configuration](docs/configuration.md) documents precedence, profiles, and
  redacted inspection.
- [Manifest v2](docs/manifest-v2.md) describes the current schema boundary.
- [Application boundary](docs/application-boundary.md) describes the separation
  between transport, lifecycle use cases, and storage.
- [Storage contracts](docs/storage-contracts.md) defines the backend-neutral
  vocabulary, the supported SQLite registry boundary, and the private
  relational/coordinator contract.
- [Private relational boundary](docs/private-relational-boundary.md) explains
  the packaged internal proof seam and its public exclusions.
- [Security and trust boundary](docs/security.md) describes safe inputs,
  errors, capability information, and transport restrictions.
- [Compatibility](docs/compatibility.md) records what is and is not a public
  compatibility promise.
- [Design evolution](docs/design-evolution.md) explains decisions retained from
  earlier design work and concepts intentionally retired.

## Development status

There is not yet a supported release installation or production deployment.
From a checkout, run the stdio server with the project environment:

    uv run aipcs serve

To run the durable SQLite registry, supply a process-local principal and either
an explicit root or a platform default:

    uv run aipcs serve --profile sqlite --principal-id local-agent \
      --sqlite-data-root /absolute/operator-owned/aipcs-data \
      --sqlite-busy-timeout-ms 5000

On Linux an omitted root resolves to `$XDG_DATA_HOME/aipcs-mcp` (or
`~/.local/share/aipcs-mcp`); on macOS it resolves below
`~/Library/Application Support/aipcs-mcp`. Resolution does not touch storage.
Each `serve` startup performs the one explicit migration operation, then starts
MCP only when the registry is ready.
`config show` and `config validate` never create directories, open a database,
or migrate storage:

    uv run aipcs config show --profile sqlite --principal-id local-agent
    uv run aipcs config validate --profile sqlite --principal-id local-agent

These are checkout/development invocations, not released installation
instructions. An explicit SQLite root must be an absolute operator-owned location.
The service validates the live path only when it starts; configuration output
reports structural support, not whether a store is ready.

The SQLite adapter requires an operator-owned `0700` root and `0600` database
and WAL/SHM files. It uses persistent WAL, `synchronous=FULL`, one
`1..30000` ms busy timeout (default `5000`), and SQLite's native
`BEGIN IMMEDIATE` writer serialisation. It fails closed on Windows and on
SQLite older than 3.51.3. Do not place the data root on a network filesystem
or share one SQLite database between hosts.

WAL is part of durable database state. Do not copy, move, delete, or repair a
live database's `-wal` or `-shm` files, and do not treat copying only the main
`.sqlite` file as a backup. A supported online backup/export command is not
available yet; stop every process cleanly before an operator-managed offline
copy.

## MCP tools

The stateless profile exposes exactly one tool:

- `aipcs_server_info`

A ready SQLite profile exposes exactly five tools:

- `aipcs_server_info`
- `aipcs_service_seed`
- `aipcs_service_list`
- `aipcs_service_inspect`
- `aipcs_service_design`

`aipcs_server_info` reports `features.registry_lifecycle: true` only in the
ready SQLite profile. `tools/list` is the source of truth for the live surface.
All tool arguments are flat JSON objects. Successful calls return
`{"ok": true, "result": ..., "error": null}`; safe failures return
`{"ok": false, "result": null, "error": ...}`. A normal MCP client invokes
the tools; the examples below show their JSON arguments and result shape.

```json
// aipcs_service_seed
{
  "domain_name": "project_context",
  "domain_class": "project",
  "intent_description": "Persist compact project context for future agent sessions.",
  "idempotency_key": "seed-project-context-001"
}
```

```json
// successful result (abbreviated)
{
  "service_id": "11111111-2222-4333-8444-555555555555",
  "domain_name": "project_context",
  "domain_class": "project",
  "intent_description": "Persist compact project context for future agent sessions.",
  "design_state": "seeded",
  "operational_status": "active",
  "schema": null,
  "schema_version": null,
  "materialised_at": null,
  "storage": null
}
```

Use `aipcs_service_list` with an optional strict integer `limit` from 1 through
100 (default 100). It returns `{"services": [ ... ]}` in creation order. Use
`aipcs_service_inspect` with the lowercase canonical non-zero UUID returned by
seed. Both mutations require an idempotency key (nonempty, at most 128
characters): retrying the same request returns the original outcome; reusing a
key with a different request returns `conflict`.

`aipcs_service_design` accepts the service UUID, a manifest-v2 `schema`, and an
idempotency key. It validates and stores the manifest, but leaves the service
seeded: `materialised_at` and `storage` stay `null`, and it creates no service
database, tables, records, or generated tools.

The installed package contains a private SQLite service-store catalog,
relational-schema adapter, and transport-neutral lifecycle coordinator for
internal fidelity and restart proof. The coordinator admits relationally
supported work to the registry before service-store I/O, closes the registry
transaction, and reconciles exact physical state through the pure recovery
planner. None of these private seams is wired to configuration, `serve`, MCP,
CLI, or the registry-only application package. Calling the catalog or domain
adapter directly can still create an orphan foundation or physical schema, so
the private modules are development and release-verification seams rather than
a user workflow or compatibility API. V1-08E owns public composition. See the
[private relational boundary](docs/private-relational-boundary.md).

AIPCS remains stdio-only. Listener transport settings are rejected before
configuration resolution or server construction.

Tests and example data in this repository are synthetic contract fixtures.
They must not contain operational records, credentials, or personal context.

## Current exclusions

- dynamically generated domain-specific MCP tools or per-domain web services
  are out of scope for the core public-v1 runtime;
- public service-store allocation/materialisation, records, branches, search,
  and cross-service retrieval are planned but not available yet;
- PostgreSQL, remote MCP, hosted tenancy, or authentication; and
- standalone lifecycle, storage, or administration CLI commands; and
- automatic deletion, archival, merging, or rewriting of memory.
