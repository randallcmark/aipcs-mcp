# Storage contracts

AIPCS keeps storage mechanics behind backend-neutral ports. Portable contracts
use opaque locators, pure commands/specifications, detached values, bounded
failure categories, and explicit transaction ownership. Paths, DSNs, SQL,
connections, cursors, driver exceptions, filesystem layouts, and migration
table names are private adapter details.

SQLite is the default local reference implementation. PostgreSQL is the
supported generic public-v1 secondary reference implementation when installed
with its `[postgresql]` extra. It preserves the same behavioral ports and
outcomes while using native PostgreSQL mechanics rather than reproducing
SQLite DDL.

## Adapter vocabulary

`StorageAdapterInfo` identifies a backend and its closed capabilities. Current
capabilities include registry, service-store foundation, domain schema, and
materialised data behavior. Capability information does not expose a location
or credential.

`ServiceStoreLocator` is an opaque logical backend/namespace pair. The public
SQLite namespace has the safe form `svc_<uuid hex>` and is not a path,
database name, endpoint, or secret.

Migration state is component-scoped and finite. Registry and service-store
adapter revisions are independent from manifest, schema, service, record,
branch, MCP, and future export versions.

## Registry storage

The registry is the authority for:

- principal-scoped service metadata;
- the current accepted manifest and schema version;
- service revision and operational/materialisation state;
- safe logical storage identity;
- global principal/idempotency replay evidence;
- lifecycle prepared/completed/recovery-required intent; and
- metadata-only audit.

One registry unit of work atomically owns a registry mutation, its replay
result, and audit append. Repository values are detached snapshots; they do not
retain connection state after close.

SQLite registry R4 and PostgreSQL registry R2 are the current exact targets.
They retain immutable checksummed migration history and add the shared
operational/portable claim authority, live/import-prepared/tombstoned identity
ledger, and flattened transfer receipts. SQLite R4 preserves the
crash-resumable WAL policy introduced by R3. Migration accepts only exact known
predecessor/prepared states. Unknown, altered, partial, dirty, substituted, or
future storage fails closed. The metadata-only audit retains the newest 1,000
rows per principal; that cap never prunes live mutation/lifecycle evidence and
has no public query or configuration surface.

## Service-store foundation

Each materialised service has one private service store selected by its opaque
locator. The foundation binds itself to that namespace, maintains its own
checksummed history, and reserves adapter-owned object names. Copying an exact
database under another locator cannot report ready.

Service-store revisions are:

- R1: foundation metadata and migration history;
- R2: exact persistent-WAL policy; and
- R3: completed local mutation replay, record history, branch topology, and
  record-branch membership foundations.

R3 reserved data contains service-local operational evidence, not a copy of
the manifest, schema version, service lifecycle phase, principal registry,
audit stream, path, or DSN. Domain entity tables remain non-reserved objects
compiled from the registry-authoritative manifest.

### Exact R2-to-R3 upgrade

An exact clean R2 store is a recognised `outdated` predecessor. Inspection is
read-only and never upgrades it.

- bootstrap remains available because it does not open the store;
- summary reports `data_status: "migration_required"` and leaves unavailable
  observations unknown;
- record/history/branch/maintenance reads return
  `storage_migration_required`; and
- only an admitted lifecycle, record, or topology mutation may run exact
  R2-to-R3 migration before its local transaction. A prepared materialise
  intent may therefore adopt a clean R2 window left by a cooperating peer.

The mutation path accepts only exact R2, exact adapter-prepared R3, or exact
ready R3 evidence. It performs no general repair and never accepts altered,
partial, generic dirty, unknown, or newer storage. Concurrent migrators
re-observe exact state after writer-lock acquisition and converge on one
immutable migration history.

## Domain schema store

The private `DomainSchemaStore` accepts an opaque locator and a detached
relational specification compiled from a validated manifest. It can:

- inspect layout relative to that supplied specification;
- materialise an exact schema-version-1 layout; and
- apply one supported adjacent additive transition.

It stores no second manifest, schema ledger, transition fingerprint, or current
schema authority. Inspection returns only unmaterialised, ready, or
incompatible relative to the supplied target. Initial materialisation and
evolution are exact and transactional.

Supported transitions include new entity tables, nullable appended fields,
new indexes, and approved metadata-only changes. The adapter never rebuilds,
renames, drops, copies, backfills, rewrites, or repairs domain tables. Exact
foreign keys use immediate `ON UPDATE RESTRICT ON DELETE RESTRICT`.

## Materialised-data port

The backend-neutral materialised-data port receives:

- an opaque service locator;
- principal and fixed provenance context;
- a detached `RecordSpecification`;
- a typed command or query; and
- detached pure outcomes only.

The current SQLite adapter requires an exact ready R3 foundation and exact
domain layout on the same selected service database.

### Record transactions

Create, update, and delete validate all domain values before persistence.
Callers cannot provide server-managed fields. The adapter supplies UUID,
principal ownership, UTC timestamps, `created_via`, and `record_version`.

Create, update, and delete commit atomically with:

- relationship/constraint checks;
- a before/after record-history event where applicable; and
- completed local mutation replay evidence.

Update/delete compare the exact expected record revision. One winner advances
the record revision once; stale callers fail without a partial write.
Delete removes the current row but preserves its detached history event.

The service-local replay ledger is keyed by principal and idempotency key and
binds operation kind plus canonical payload fingerprint. Same-key/same-request
returns the stored result; changed content fails. It has no prepared phase and
is not lifecycle, schema, or cross-store authority.

The `__aipcs_` idempotency-key prefix is reserved for service-local control
evidence and is rejected by record and topology request contracts. The
portable lifecycle seam uses one exact control row in the existing internal
ledger for a monotonic write-admission fence. An exact legacy store with no
control row is interpreted as generation 1/open; closing or reopening the
fence writes a later generation atomically. This does not change the immutable
R1–R3 migration history or make mutation replay rows portable.

### Retrieval

Record get/list/search/history are read-only and principal-scoped. List,
search, history, and branch list use opaque query-bound cursors and page sizes
from 1 through 100.

Filter compilation accepts only manifest-declared domain fields:

- exact scalar comparison;
- one string member against declared membership `string_list`; and
- no filter for annotations.

Unknown, malformed, server-managed, annotation, or unsupported filters fail
before SQL construction. No caller SQL, identifier, sort expression, or
backend predicate crosses the port.

The adapter hydrates `string_list` values as JSON arrays and projects no
principal/owner field. Record/history JSON is bounded to 64 KiB.

## Branch topology

Branches are service-local topology above records. R3 stores branch metadata
and principal/entity/record membership mappings.

A branch has a UUID, unique principal-scoped 3–64 character lowercase
alphanumeric-and-hyphen slug, title, intent, optional type, optional parent,
status, retrieval summary, timestamps, and positive `branch_revision`. A slug
starts with a letter and ends with an alphanumeric character. Parent references
are restricted. Archiving or superseding is a status update; there is no branch
delete operation.

Each record has at most one primary branch and may have multiple related
branches. Assignment/unassignment accepts 1–100 distinct targets, each with an
expected record revision. The transaction:

- validates branch and every target;
- replaces prior primary membership when assigning primary;
- adds/removes only requested related membership;
- advances each effectively changed record once;
- appends the corresponding record-history event; and
- commits one completed replay result.

The whole request succeeds or fails; there is no partial target set. Exact
no-op behavior remains deterministic and does not create duplicate mappings.

## Discovery reads

Bootstrap is registry-only and never allocates, opens, inspects, or migrates a
service store.

Summary reads a ready service store using bounded `SELECT` operations:

- truthful per-entity principal-scoped counts;
- up to 20 values per declared domain facet;
- up to 100 branch cards;
- records lacking a primary branch; and
- optional 0–3 principal-scoped record samples per entity.

Manifest-derived affordances, authority-field availability, and query guidance
come from the authoritative manifest, not from inferred database shape.

Maintenance is deterministic, principal-scoped, bounded to 100 candidates, and
read-only. It supports only declared/mechanical signals: expired validity,
stale age, low numeric confidence, supersession, missing declared authority,
unbranched records, exact duplicate authority references, and oversized
declared annotations. A signal is unavailable when requisite fields are not
declared. Maintenance stores no score and performs no update.

## SQLite physical policy

SQLite storage is supported on a local POSIX filesystem under one effective
user. The location policy requires an operator-owned `0700` root and `0600`
database/WAL/SHM files, rejects unsafe links/types/ownership, and uses
descriptor-relative checks.

Ready databases use persistent WAL, `synchronous=FULL`,
`wal_autocheckpoint=1000`, a configured busy timeout from 1 through 30,000 ms,
and SQLite `BEGIN IMMEDIATE` writer serialisation. Numeric BUSY-family outcomes
map to bounded `storage_busy`; the adapter does not retry internally.

SQLite operational sidecars are durable state. The adapter does not copy,
delete, edit, or repair them. Network filesystems, multi-host sharing, Windows
SQLite, and hostile same-user processes are unsupported.

## PostgreSQL reference-adapter contract

One AIPCS installation uses one operator-provisioned PostgreSQL database. The
registry occupies the exact private schema `aipcs_registry`; every materialised
service occupies one exact private `svc_<uuid hex>` schema in the same
database. The logical service locator remains opaque above the adapter and is
never an endpoint, database name, or credential.

The adapter is designed for a dedicated non-superuser role with only database
`CONNECT` and schema-creation authority. It neither creates databases, roles,
or extensions nor requires superuser, `CREATEDB`, `CREATEROLE`, replication,
or row-security-bypass privileges. Installation policy revokes public schema
access. Transport security and certificate policy remain operator-managed
libpq connection settings supplied through the secret DSN.

Registry and service work retain their existing transaction boundary: a
registry transaction is committed before service-schema work begins, and
finalisation uses a new registry transaction and connection. Foundation,
domain, and data mutations are transactional within their owning schema.
Adapter migration and lifecycle serialisation use transaction-scoped advisory
locks with bounded lock and statement timeouts. Inspection is read-only,
queries structured `pg_catalog` state, and never executes DDL.

PostgreSQL R1 is the behavioral counterpart of the current SQLite foundation,
not a byte-for-byte or DDL-equivalent format. It uses native `uuid`, `bigint`,
`double precision`, `boolean`, microsecond UTC `timestamptz`, text, and `jsonb`
representations. Text columns and text comparison/index expressions use the
built-in deterministic `C` collation so equality, uniqueness, ordering, and
cursor boundaries remain bytewise across operator database locales. Object
references are schema-qualified wherever PostgreSQL grammar permits;
`CREATE INDEX` uses its validated explicit unqualified index name with a
schema-qualified target table and catalog verification of the resulting index
schema. Exact semantic conformance is measured using synthetic normalized
results across adapters; database files, private SQL, and migration history are
not portable artifacts.

The supported compatibility policy is PostgreSQL major versions 16 through 18.
Release verification passes the full contract-parity suites on pinned
PostgreSQL 16 and 18 endpoints. `psycopg` 3 is an optional PostgreSQL
dependency. The adapter has no general driver or adapter retry loop. For a
data mutation with commit uncertainty only, it may make one evidence-led
re-observation and safe retry using the same idempotency key, matching SQLite;
it never retries from generic SQLSTATE or error text. Private, phase-aware
SQLSTATE outcomes map into the existing bounded storage failure categories
without exposing SQLSTATE, driver text, identifiers, endpoints, or
credentials.

## Internal portable service-store seam

The application owns backend-neutral immutable values for record, history,
branch, and branch-membership members plus a closeable snapshot-reader port.
Members have one canonical cross-adapter order. The port accepts a detached
logical `Service` allocation and never accepts a path, DSN, connection,
schema object, SQL identifier, or adapter instance.

Both reference adapters implement:

- a stable read-only snapshot that requires an exact closed fence;
- local transactional staging into an unpublished service allocation;
- exact typed re-observation for safe retry;
- monotonic compare-and-change write admission;
- exact archived-allocation purge with independent absence observation; and
- an admission check inside every record/topology mutation transaction.

SQLite uses its anchored location policy, WAL snapshot, and immediate writer
transaction. PostgreSQL uses schema-qualified statements, repeatable-read
snapshots, and one transaction-scoped service advisory lock shared by data
mutations and fence transitions. Logical staging never copies SQLite database,
WAL, or SHM files and never exports PostgreSQL DDL, catalog state, roles,
schemas, endpoints, or migration history.

The private format is `export_format_version: 1` under the exact
`aipcs-json-c14n-1` and `aipcs-bundle-limits-1` profiles. Frames are at most
1 MiB including LF; the default total is 256 MiB, the absolute maximum is
1 GiB, nesting is at most 64 container levels, and the frame count is at most
1,000,000. Exact canonical re-encoding, ordered section digests, a root
SHA-256 digest, and trailer counts reject malformed, noncanonical, truncated,
duplicated, reordered, substituted, or accidentally altered input. Those
hashes are integrity evidence, not encryption, signatures, authentication,
hostile-author authenticity, replication, or backup retention.

The private portable coordinator composes this seam with the shared registry
authority. Import first validates canonical framing, limits, digests, closed
payload shapes, manifest/state rules, and every logical cross-reference into
an application-owned private spool. Dry run stops there with zero registry or
service-store writes. A committed import then reserves an unpublished
identity, stages and independently re-observes exact logical state, and
publishes the service and verified receipt atomically in a fresh registry
transaction. A restart replays completed evidence, adopts only an exact
same-root stage, or returns bounded uncertainty/recovery-required; it never
deletes or guesses at a staged store.

Export emits seeded metadata without opening a service store. A materialised
service must already be suspended or archived with an exact closed fence. The
coordinator snapshots logical members, verifies the fence and prepared
registry claim again, completes a local verified receipt, and only then emits
the private stream. Suspend/archive close the fence before registry state
changes; resume opens it before the registry becomes active. Registry
admission remains the lifecycle authority, while the service-local fence
prevents an already-admitted mutation from crossing that transition.

Purge is never inferred from archival alone. Registry admission first requires
an archived service and either its verified export receipt or an explicit
override. SQLite then removes only identity-verified service database and
sidecar files through the anchored location policy. PostgreSQL drops only the
known tables in the exact service schema and the empty schema, all with
`RESTRICT`; it never cascades into an external object. The coordinator
independently re-observes absence before a fresh registry transaction records
the tombstone. Finalisation deletes obsolete receipts and completed claim
payloads for the service so the retained tombstone is minimal and self-
consistent. Failure or uncertain observation leaves the prepared claim for
bounded reconciliation rather than publishing a false purge.

Receipts remain bound to the exact completed claim, bundle root, backend,
service identity, revision, and operational status at issuance. They are
historical evidence rather than current-state replicas: later valid service
revisions do not make a registry incompatible. Import and export receipts,
registry audit, replay claims, and purge tombstones remain installation-local
control evidence and are never copied into a bundle as source authority.

The composition root selects one closed `sqlite | postgresql` implementation
for the coordinator. Portable lifecycle adds no MCP tool or configuration key.
Only the CLI owns operator-selected transfer paths: storage and portable
application ports still receive logical locators and already-open private
streams.

## Deferred adapter work

PostgreSQL is a supported generic public-v1 reference adapter when the
package is installed with its `[postgresql]` extra. Third-party adapters,
mixed-backend runtime composition, physical backup/restore, arbitrary repair,
remote administration, application-managed authentication, and cross-store
transactions remain deferred. Those
features require explicit contracts and validation rather than exposure of
either reference adapter's private modules.
