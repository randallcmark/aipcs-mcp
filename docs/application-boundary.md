# Application boundary

The application boundary is an internal construction seam. It separates stdio
MCP and CLI adapters from registry lifecycle use cases and keeps storage
mechanics outside the application layer. It is not a public adapter extension
mechanism or a standalone administration API.

## Principal context

Every lifecycle use case receives an explicit opaque `principal_id` and
`created_via` context from its transport adapter. The SQLite stdio transport
uses one configured principal for its full process lifetime and fixes
`created_via` to `mcp`. Neither value is request-controlled or publicly
observable. The application must not infer identity, ownership, or authority
from environment values, filesystem state, connection details, or a storage
backend. This keeps ownership checks testable and allows future authentication
work to remain a transport concern.

## Unit of work ownership

A service seed or initial-design mutation, its idempotency-ledger outcome, and
its metadata-only audit entry belong to the same unit of work. The application
use case owns the requested operation and business outcome; the adapter owns
transaction scope, rollback, and durable commit. A failed ledger or audit write
must fail the mutation rather than leave an unaccounted state change.

The SQLite registry provides the current production unit of work, mutation
ledger, and metadata-only audit store for seed and initial-design operations.
It does not provide a service-store or record unit of work. The packaged
private SQLite service-store catalog is not injected into the application and
has no application port for materialisation or records. The private
`DomainSchemaStore` relational-schema protocol is likewise uncomposed: it
freezes pure inspection/materialisation/evolution signatures but has no runtime
composition or call path. A successfully
acquired registry unit is always closed exactly once. `commit()` and `rollback()`
terminate its transaction attempt; `close()` releases resources and performs
adapter-safe cleanup of an unterminated attempt. The application preserves the
original failure when rollback or close also fail. A close failure after an
otherwise successful operation is a bounded internal failure. A commit failure
has indeterminate durable outcome, so callers retry through idempotency rather
than assuming a rollback erased it.

## Independent versions

The current application keeps version dimensions separate. The frozen V1-08A contract extends that
separation without adding a current runtime path:

- `manifest_version` describes the schema-document interpretation; normal public design input is
  manifest v2.
- `schema_version` describes agent-defined additive evolution and is a future lifecycle concurrency
  input, not a manifest or MCP compatibility version.
- A future server-owned `service_revision` is the lifecycle compare-and-swap revision; a successful
  design/materialise/evolve/operational transition increments it once, while exact replay does not.
- A future `record_version` is a per-record mutation revision reserved for V1-08F.
- Adapter migration revisions describe physical storage layout and adapter-owned migration state.

Future cross-store operation state (`prepared`, `completed`, or `recovery_required`) is distinct
from all of these values. A schema change is not an adapter migration, and neither is a substitute
for a durable operation/recovery record.

The uncomposed service-store catalog demonstrates that separation: its private
revision 1 can make adapter metadata ready without reading a manifest or
changing registry state. Such a database is not a materialised service. Direct
initialisation may leave it orphaned until a future operation record can
coordinate and reconcile the independently committed stores.

The relational specification is also not a second schema authority. It is an
immutable, backend-neutral projection supplied by the registry-authoritative
manifest at the point a future adapter needs comparison. No schema ledger,
manifest copy, schema-version row, or fingerprint is introduced into a service
store by this boundary.

## Frozen future lifecycle boundary

V1-08A documents, but does not compose, future materialise/evolve use cases. Materialise will
require `service_id`, `expected_service_revision`, `expected_schema_version`, and an idempotency
key. Evolve will require the same inputs plus a complete, deeply validated adjacent manifest-v2
target; it will not accept SQL, a migration delta, or history prose. Admission will validate and
detach the request, compute the principal-scoped fingerprint, and resolve any existing idempotency
claim before reading current expected revisions. Exact completed claims replay even after their
successful operation incremented the service revision.

The future registry intent is the only cross-store recovery and transition-lock authority. It may
retain one immutable admitted target snapshot, but that evidence never becomes a second current
manifest. A coordinator will adopt an exact contained physical target only within the documented
same-operating-system-owner boundary, because no domain provenance seal exists to distinguish a prior
exact target from a crash-completed one. It must report recovery-required rather than infer or
repair deleted, partial, extra, altered, or incompatible state. These are future V1-08B-D rules;
they do not create a public operation, storage I/O path, or recovery command.

Their frozen future result meaning is also exact: malformed input, unsupported transition, stale
expected revision, changed-fingerprint reuse, recovery-required, storage-unavailable, and generic
internal failure are non-retryable; storage-busy, different-key operation-in-progress, and
operation-uncertain are retryable. An exact same-key prepared claim resumes reconciliation rather
than returning operation-in-progress.

## Migration and serving rule

A SQLite adapter applies its required registry migration once before it reports
the process ready to serve. Migration failure prevents MCP construction. The
runtime does not construct or migrate the service-store catalog. Read
operations do not execute DDL, and opening a unit of work does not
opportunistically alter storage layout.

The application layer requests behavior through its boundary; it never creates
tables, parses storage locations, opens database connections, or selects a
backend.

See [storage contracts](storage-contracts.md) for the pure adapter vocabulary
and migration-state boundary.

## Current capability

Stateless exposes only `aipcs_server_info` over stdio. A ready SQLite profile
adds seed, list, inspect, and design. Design validates and stores an initial
manifest but does not apply it to physical tables: a resulting service remains
`seeded`, has no storage projection, and cannot hold records. There is no
standalone lifecycle/admin CLI, public service-store allocation or
materialisation, record API, export/import, PostgreSQL, remote transport, or
adapter discovery mechanism. The packaged private catalog and domain-schema
store remain outside this application and public capability surface.

See [compatibility](compatibility.md) and [security](security.md).
