# AIPCS

<p align="center">
  <img src="assets/aipcs-mark.svg" width="112" alt="AIPCS logo">
</p>

**AIPCS** is a self-operated, local-first memory primitive server for AI
agents. An agent defines a relational memory schema; the server supplies a
stable generic MCP surface for service lifecycle, records, discovery, branches,
and advisory maintenance. The agent retains ownership of its memory architecture
as its knowledge grows.

The current source runtime supports:

- strict manifest-v2 validation and one-way legacy manifest-v1 conversion;
- principal-scoped service seed, design, materialise, and additive evolution;
- generic record create, get, list, search, update, delete, and history;
- memory branches with primary and related record membership;
- shape-only bootstrap plus bounded service summaries, facets, and samples;
- read-only mechanical maintenance candidate discovery;
- local administration, operational lifecycle, and logical transfer commands;
- a local SQLite implementation over stdio or Streamable HTTP; and
- a generic PostgreSQL reference implementation over either transport when
  installed with the `postgresql` optional dependency.

SQLite is the default local reference backend. PostgreSQL is a supported
generic public-v1 reference backend when the package is installed with its
`[postgresql]` extra and an operator provides a database. `stdio` remains the
local default. Streamable HTTP is available for a trusted service deployment;
it binds to loopback by default and is not an authentication or tenancy layer.
Remote public exposure must sit behind an authenticated TLS gateway. Physical
backup/restore and hosted tenancy are future work.
Any client that can reach an AIPCS HTTP listener has full read/write access to
every service for that listener's configured principal.

Copyright 2026 Mark Randall. Licensed under [Apache-2.0](LICENSE).

## Installation and isolated invocation

Run the packaged command in an isolated environment:

```text
uvx --from aipcs aipcs --help
```

The PostgreSQL adapter is an explicit optional dependency:

```text
uvx --from 'aipcs[postgresql]' aipcs config validate \
  --profile postgresql \
  --principal-id local-agent \
  --postgres-dsn-env AIPCS_POSTGRES_DSN
```

`uvx` isolates Python packages, not persistent AIPCS data. Supply the same
operator-owned SQLite data root or PostgreSQL DSN reference on subsequent
invocations. The package requires Python 3.12 or newer. Source checkouts use
the locked development environment described below.

## Documentation

- [Quickstart](docs/quickstart.md)
- [Concepts and lifecycle](docs/concepts.md)
- [Capabilities and limits](docs/capabilities.md)
- [Configuration](docs/configuration.md)
- [Storage and deployment](docs/storage.md)
- [Administration CLI](docs/administration.md)
- [Export, import, and migration](docs/backup-and-migration.md)
- [Agent integration](docs/agent-integration.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Architecture](docs/architecture.md)
- [Manifest v2](docs/manifest-v2.md)
- [Application boundary](docs/application-boundary.md)
- [Storage contracts](docs/storage-contracts.md)
- [Security and trust boundary](docs/security.md)
- [Compatibility](docs/compatibility.md)
- [Design evolution](docs/design-evolution.md)
- [Security reporting](SECURITY.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)
- [Release policy](RELEASING.md)
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)
- [AIPCS name and logo policy](TRADEMARKS.md)
- [Licence](LICENSE)

Start with the [quickstart](docs/quickstart.md) to reach a first persisted
memory using only the installed package and public MCP tools.

## Checkout development

Run the stateless capability server:

```text
uv run aipcs serve
```

Run the local SQLite profile with an operator-selected principal and local
data root:

```text
uv run aipcs serve --profile sqlite --principal-id local-agent \
  --sqlite-data-root /absolute/operator-owned/aipcs-data \
  --sqlite-busy-timeout-ms 5000
```

An omitted SQLite root uses the documented platform default. Configuration
resolution does not touch storage. `serve` performs the explicit registry
migration before constructing MCP and fails closed if storage is unsafe,
busy, dirty, incompatible, or unavailable.

SQLite support is intentionally bounded to Linux and macOS, SQLite 3.51.3 or
newer, one host, a local POSIX filesystem, and cooperating processes under the
same effective user. Persistent WAL allows concurrent readers and serialises
writers through SQLite's writer slot. Do not use a network filesystem or copy
only a live database's main `.sqlite` file as a backup. The internal logical
portable format described below is not an online SQLite backup procedure.

## MCP contract

The stateless profile exposes only `aipcs_server_info`. A ready SQLite profile
exposes exactly 21 tools:

- `aipcs_server_info`
- `aipcs_service_seed`
- `aipcs_service_list`
- `aipcs_service_inspect`
- `aipcs_service_design`
- `aipcs_service_materialise`
- `aipcs_service_evolve`
- `aipcs_record_create`
- `aipcs_record_get`
- `aipcs_record_list`
- `aipcs_record_search`
- `aipcs_record_update`
- `aipcs_record_delete`
- `aipcs_record_history`
- `aipcs_bootstrap`
- `aipcs_service_summary`
- `aipcs_branch_create`
- `aipcs_branch_list`
- `aipcs_branch_update`
- `aipcs_branch_assign_records`
- `aipcs_maintenance_scan`

`tools/list` is the source of truth for a live process. Server info reports
`aipcs_mcp_contract: "1.2.0"` and separately reports registry lifecycle,
materialisation lifecycle, record runtime, and discovery/topology features.

All arguments are strict JSON objects. Unknown fields are rejected. Successful
calls use `{"ok": true, "result": ..., "error": null}`; failures use a safe,
bounded error document and never expose SQL, paths, credentials, principal
identity, operation evidence, or driver errors.

## Service and schema lifecycle

The normal flow is:

1. seed a durable service cue;
2. design it with a complete initial manifest-v2 document;
3. materialise the exact schema with current service and schema revisions;
4. create and retrieve records; and
5. evolve with one complete adjacent additive manifest when the schema needs
   to change.

Design persists the manifest but creates no service database or domain table.
Materialise creates the initial relational layout. Evolve accepts a complete
target manifest, never SQL or a partial migration delta. `service_revision`,
`schema_version`, per-record `record_version`, per-branch `branch_revision`,
and adapter migration revisions are independent concurrency dimensions.

Lifecycle mutations use registry-held durable intent with
`prepared | completed | recovery_required` phases. Retrying the exact request
and idempotency key replays or resumes it. A changed request under the same key
fails safely. Concurrent same-key callers may initially receive retryable
`operation_uncertain` or `storage_busy`; retrying the unchanged request and key
converges on the one completed result. Repeated physical `dirty` observations
alone never make the shared registry intent recovery-required. An exact clean
predecessor observed while a peer migration is progressing is resumed through
the same bounded foundation migration path; only incompatible evidence is
terminal. See
[application boundary](docs/application-boundary.md).

## Records and retrieval

Every entity contains the exact server-managed fields `id`, `owner_id`,
`created_at`, `updated_at`, `created_via`, and `record_version`. Callers provide
only domain fields. The server supplies identity, principal ownership,
timestamps, provenance, and revision values and omits `owner_id` from public
record results.

Create, update, delete, branch create/update, and branch assignment require
idempotency keys. Update and delete also require the exact current
`expected_record_version`; branch update requires
`expected_branch_revision`. A successful record mutation increments
`record_version` once. Exact completed retries replay the stored local result
without writing again.

Search is structured:

- scalar fields use exact equality;
- a declared `string_list` membership field accepts one string member;
- annotation fields are not filterable; and
- undeclared, server-managed, malformed, or unsupported filters fail closed.

Search is deliberately limited to the structure declared by the manifest. This
keeps retrieval explicit, predictable, and owned by the agent's domain model.
List, search, branch list, and history use query-bound opaque cursors. Clients
must return the cursor unchanged with the same query rather than parsing or
reusing it for another query.

Current hard bounds include:

- record or history JSON: 64 KiB;
- one string: 16 KiB;
- one `string_list`: 256 distinct strings of at most 256 characters;
- search filters: 16;
- page size: 1–100, default 50; and
- one branch assignment request: 1–100 explicit record targets.

## Branch topology and history

A branch has a stable UUID, slug, intent, optional type and parent, status
`active | archived | superseded`, and a server-owned `branch_revision`.
Archiving is a status update, not deletion.

A record may have at most one primary branch and any number of related
branches. Assigning a primary branch replaces the prior primary; related
assignment adds membership. Assignment and unassignment are all-or-nothing,
idempotent, and require each target's expected record version. Every effective
mapping change advances the affected record revision and writes a record
history event such as `primary_assign`, `primary_move`, or `related_unassign`.
There is no separate public branch-history stream.

## Discovery and maintenance

`aipcs_bootstrap` reads registry projections only. It is shape-only and
value-free: it never opens, allocates, or migrates a service store. Use it to
select a service, then call `aipcs_service_summary`.

Summary returns manifest-derived retrieval affordances, declared authority
field availability, query guidance, truthful entity counts, up to 20 observed
values per declared domain facet, up to 100 branch cards, the count of records
without a primary branch, and optional samples of 0–3 records per entity.
Samples use the same principal-scoped public record projection.

Maintenance is read-only and advisory. It can report bounded candidates for
expired validity, age beyond a caller-supplied stale threshold, low declared
numeric confidence, declared supersession, missing declared authority,
unbranched records, exact duplicate authority references, and oversized
declared annotation fields. Signals whose required fields are not declared
are reported as unavailable. The server does not infer truth, rank authority,
merge records, archive, delete, or rewrite memory.

Bootstrap is bounded to 100 service cards. Summary is bounded as described
above. Maintenance returns at most 100 deterministic candidates.

## Administration and portable lifecycle

The `aipcs` administration CLI exposes read-only `status`, `doctor`,
`storage status`, `service list`, `service inspect`, and `maintenance scan`
commands. These inspect configured SQLite or PostgreSQL state without
migration, repair, DDL, or hidden lifecycle actions.

Revision-bound `service suspend|resume|archive|restore` commands operate
through the same portable application boundary as logical export, import, and
purge. Machine mode requires explicit recovery-critical values. Interactive
mode displays generated values before confirmation. Export publishes an
exclusive mode-`0600` file; import rejects symlinks and non-regular files and
supports a complete zero-write dry run. Purge is archived-only and requires a
verified receipt or an explicit override plus exact service identity
confirmation.

The administration command tree is:

```text
aipcs status
aipcs doctor [--service SERVICE_ID]
aipcs storage status [--service SERVICE_ID]
aipcs service list [--limit N]
aipcs service inspect SERVICE_ID
aipcs service suspend|resume|archive|restore SERVICE_ID
aipcs export SERVICE_ID --output FILE
aipcs import --input FILE [--dry-run]
aipcs service purge SERVICE_ID
aipcs maintenance scan --service SERVICE_ID
```

Machine lifecycle/export commands require `--expected-revision` and
`--operation-id`; archive additionally requires `--yes`. Actual import
requires `--operation-id` and `--yes`. Machine purge also requires exactly one
of `--receipt` or `--override`, plus `--yes` and an exact
`--confirm-service-id`. JSON is the default stable output; `--format human`
is opt-in.

The internal `export_format_version: 1` artifact is strict canonical UTF-8
JSON Lines containing logical service, manifest, record, history, branch, and
membership state. It never copies SQLite databases/WAL/SHM or PostgreSQL DDL,
schemas, catalogs, roles, endpoints, credentials, or migration ledgers.
SHA-256 framing detects accidental tamper, truncation, duplication,
substitution, and reordering; it does not provide encryption, signatures,
operator authentication, hostile-author authenticity, replication, or backup
retention.

Materialised export requires a separately suspended or archived service. A
service-local monotonic fence prevents an already-admitted mutation from
committing across suspend/archive. Import validates the complete artifact
before writes, supports a zero-write dry run, stages an unpublished allocation,
and publishes only after exact re-observation. Purge is archived-only,
separately authorised, terminal, and leaves a minimal immutable tombstone.
There is no remap, clone, merge, overwrite, skip, partial-import, or automatic
cleanup mode.

Wheel and sdist release verification exercises this boundary from
outside the checkout with private streams on SQLite and both
SQLite↔PostgreSQL directions for pinned PostgreSQL 16–18.

## Agent-use examples

The [agent integration guide](docs/agent-integration.md) contains
vendor-adaptable stdio configuration and public-tool workflows for bootstrap,
retrieval, persistence, schema evolution, maintenance, and pre-compaction
persistence. The copyable [AGENTS.md example](examples/agent-instructions/AGENTS.md)
is deliberately short; an optional [seeded guide service](examples/agent-instructions/seeded-guide-service.md)
keeps deeper operating help discoverable without adding it to every bootstrap
response.

## Current exclusions

The current source contract deliberately excludes:

- generated schema-specific tools and per-domain services;
- additional retrieval modes beyond declared exact filters;
- third-party storage adapters or mixed-backend runtime composition;
- physical backup/restore and arbitrary repair;
- application-managed HTTP authentication or authorisation, hosted tenancy,
  and multi-host SQLite; and
- automatic truth resolution, merge, archival, deletion, or schema invention.

Repository tests and examples are synthetic contract fixtures. Do not add
operational databases, snapshots, credentials, transcripts, or personal
context to the public repository.
