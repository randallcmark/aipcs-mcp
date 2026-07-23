# Storage contracts

AIPCS keeps storage mechanics behind backend-neutral ports. Portable contracts
use opaque locators, pure commands/specifications, detached values, bounded
failure categories, and explicit transaction ownership. Paths, DSNs, SQL,
connections, cursors, driver exceptions, filesystem layouts, and migration
table names are private adapter details.

SQLite is the current implementation. PostgreSQL is deferred; its future
adapter must preserve the behavioral contract rather than reproduce SQLite
DDL.

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

SQLite registry R3 is the current exact target. It retains immutable
checksummed migration history and the crash-resumable WAL policy introduced by
earlier revisions. Migration accepts only exact known predecessor/prepared
states. Unknown, altered, partial, dirty, substituted, or future storage fails
closed. The metadata-only audit retains the newest 1,000 rows per principal;
that cap never prunes mutation or lifecycle evidence and has no public query or
configuration surface.

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

## Deferred adapter work

The current ports do not imply implemented PostgreSQL, adapter discovery,
export/import, online backup, repair, archive/resume, purge, an administration
CLI, or a cross-store transaction. Those features require explicit contracts
and validation rather than exposure of private SQLite modules.
