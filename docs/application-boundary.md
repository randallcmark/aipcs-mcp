# Application boundary

The application boundary separates MCP request/projection code from registry,
lifecycle, and materialised-data storage. It is an internal construction seam,
not a public adapter plugin or administration API.

## Principal context

Every use case receives an opaque `principal_id` and fixed `created_via`
context from the transport. The SQLite stdio process uses one configured
principal for its lifetime and fixes `created_via` to `mcp`. Neither value is
request-controlled. Principal identity is never projected in tools, errors,
configuration reports, logs, or representations.

Ownership is therefore explicit at the port boundary. Application code does
not infer it from the operating-system user, environment, current directory,
filesystem path, database, or MCP client.

## Cohesive application seams

The registry application owns:

- seed, list, inspect, and initial design;
- service metadata and current authoritative manifest;
- server-owned `service_revision`;
- registry idempotency and metadata-only audit; and
- lifecycle admission/finalisation repositories.

The transport-neutral lifecycle coordinator owns materialise/evolve use cases.
It prepares registry intent, closes the registry unit of work, performs exact
service-store and domain actions, re-observes state, then finalises through a
fresh registry unit of work.

The portable coordinator applies the same finite pattern to
suspend/resume/archive/restore, logical export/import, and admitted purge. It
accepts only typed commands and private binary streams. Bundle parsing and
logical payload values remain storage-independent; the top-level coordinator
alone maps bounded storage failures and invokes the selected portable-store
port. It is not reachable from the MCP contract or configuration surface.
Administration composes read-only commands through an application-owned
inspection port. The runtime composition root translates concrete migration
and domain inspection into bounded application facts; application and CLI
modules do not import adapters. These commands do not migrate, repair, or
execute DDL. Operational suspend/resume/archive/restore use the same
`PortableCoordinator` only after exact registry readiness. They preserve
revision and idempotency preconditions and perform no hidden compound
transition. Export/import pass only already-open private binary streams to the
coordinator. CLI file handling owns exclusive publication and safe read-only
opening; the coordinator retains canonical validation, spooling, limits, and
validation-before-write. Purge similarly passes one typed command through the coordinator.
Archived state, exact revision, authority, physical absence, and terminal
tombstone publication remain coordinator/registry responsibilities; the CLI
owns only strong confirmation and safe projection.

The data application owns generic record, branch, discovery, and maintenance
use cases. It receives the registry-authoritative materialised service and a
detached pure `RecordSpecification`, then delegates to a backend-neutral
materialised-data store. It does not parse SQL, paths, DSNs, connections, or
driver errors.

## Transaction and replay ownership

Seed/design mutations, their replay evidence, and their metadata-only audit
entry commit in one registry unit of work.

Materialise/evolve are cross-store lifecycle operations. Registry intent is
the only transition-lock and recovery authority and may be `prepared`,
`completed`, or `recovery_required`. A prepared intent commits before physical
service-store I/O. There is deliberately no cross-database transaction, hidden
process mutex, or second current-manifest ledger.

Import has the same registry-first boundary: the canonical artifact is fully
validated and privately spooled before admission, a prepared identity
reservation commits before staging, and the service becomes visible only when
the exact re-observed store and verified receipt are published together in a
fresh registry transaction. Operational transitions commit the service-local
write fence before their registry status/revision update; a prepared shared
claim blocks competing lifecycle work during that bounded window.

Purge also remains registry-first. The registry validates the archived state
and verified export receipt or explicit override before the adapter may remove
the exact service allocation. The coordinator re-observes physical absence and
only then finalises the registry tombstone in a fresh unit of work. A partial
or uncertain delete never becomes completed evidence. Finalisation removes
obsolete transfer receipts and completed claim payloads for that service before
retaining the minimal immutable tombstone and bounded local audit.

Transfer receipts are immutable evidence of the exact completed export/import
that issued them. They record that completion's service revision and
operational status; they are not a second current-service projection. A later
valid operational transition or schema revision therefore does not invalidate
registry readiness, while altered receipt/claim identity, bundle root,
backend, or issuance state still fails closed.

Record and topology mutations are different. Their service-local R3 mutation
ledger stores only completed outcomes and commits atomically with the record,
history, branch, or assignment change. The key namespace is
`(principal_id, idempotency_key)` within that service store, so changing the
operation or payload under the same key fails. Exact retries replay the
completed result. This ledger has no prepared phase, schema authority,
lifecycle blocker, recovery state, or cross-store role.

A potentially committed failure is never guessed from exception text.
Lifecycle may return `operation_uncertain`; a local mutation retry uses the
same request and key to discover a completed replay or safely retry. The
physical `dirty` observation is deliberately coarse and can describe valid
peer progress, so repeated dirt after the bounded reconciliation budget
returns retryable uncertainty and leaves the registry intent prepared. It is
not terminal recovery evidence.

An exact `outdated` foundation is also resumable for a prepared materialise
intent. It can be the clean predecessor committed by a cooperating migration
before the exact target revision becomes visible. The coordinator migrates it
through the adapter boundary, re-observes the foundation, and only then
inspects or creates the domain schema. It does not mark the registry intent
recovery-required merely because this predecessor window was observed.

## Independent revisions

The application keeps these dimensions separate:

- `manifest_version`: interpretation of the schema document;
- `schema_version`: agent-authored additive schema evolution;
- `service_revision`: lifecycle compare-and-swap revision;
- `record_version`: per-record mutation and topology revision;
- `branch_revision`: branch metadata compare-and-swap revision;
- `export_format_version`: interpretation of a logical portable artifact;
- adapter revision: private physical storage layout; and
- `aipcs_mcp_contract`: public MCP shape and behavior.

A successful service lifecycle transition increments `service_revision` once.
A successful record mutation increments `record_version` once. An effective
branch assignment/unassignment is a record mutation and advances every affected
record once. A branch metadata update increments `branch_revision`. Exact
completed replays and no-op failures do not advance revisions.

## Record and branch behavior

The pure record contract validates domain values before the adapter sees them.
Server-managed identity, principal, timestamps, provenance, and record revision
are not caller fields. Relationships and constraints are enforced in the same
local transaction as the mutation.

Record list/search/history and branch list are bounded cursor reads. Cursors
are opaque, query-bound values created below the transport seam. Search filters
are compiled only from declared fields:

- scalar equality;
- one-string membership for declared `string_list` membership fields; and
- no filter for annotations or server-managed fields.

Branches form server-managed topology above records. A record may have one
primary branch and many related branches. Assigning primary replaces the
previous primary. Related assignment adds membership. Assignment requests are
bounded, all-or-nothing, idempotent, and carry an expected record revision for
each target. Effective topology changes append record-history events.

## Discovery and maintenance

Bootstrap is a registry-only application read. It returns bounded shape cards
and never allocates, opens, inspects, or migrates service storage.

Summary combines manifest-derived affordances with data-store observations:
record counts, declared domain facets, branch cards, unbranched count, and
optional 0–3 samples per entity. For an exact R2 service store it returns
`data_status: "migration_required"` and does not invent zero counts.

Maintenance is a deterministic read over declared/mechanical facts. It may
report expired, stale-age, low-confidence, superseded, missing-authority,
unbranched, duplicate-authority, and annotation-blob candidates. It never
mutates data or decides truth, relevance, merge, archival, or deletion.

## Migration and serving rules

`serve` explicitly migrates the registry before MCP construction. Runtime
composition alone does not touch service stores.

Service-store reads perform no DDL. Missing/unready data operations fail
safely. An exact clean R2 service store is reported as migration-required.
Only an admitted lifecycle, record, or topology mutation may request the exact
R2-to-R3 migration. A prepared materialise intent can resume the clean R2
intermediate committed by its own or a cooperating migration; read-side calls
still never upgrade it. Altered, generic dirty, partial, unknown, or future
foundations remain fail-closed.

The application layer never creates tables itself, discovers storage plugins,
or exposes concrete adapters. A persistent process selects one homogeneous
backend for both registry and service stores; mixing SQLite and PostgreSQL
within one process is not a supported runtime shape. Concrete catalog,
relational, record, topology, and discovery implementations remain private
compositions behind the ports.

See [storage contracts](storage-contracts.md),
[security](security.md), and [compatibility](compatibility.md).
