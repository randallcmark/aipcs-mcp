# Storage contracts

The storage layer defines pure, backend-neutral contracts. The current SQLite
registry adapter implements the registry portion and is composed only by the
local stdio runtime. A private SQLite service-store catalog implements the
separate service-store portion as an uncomposed foundation. Neither is a
general adapter plugin API or a PostgreSQL implementation.

The reference adapter is certified only for local POSIX filesystems on Linux
and macOS, one host, Python 3.12+, SQLite 3.51.3+, and cooperating processes
running as the same effective user. It uses a descriptor-anchored `0700` root
and `0600` database/WAL/SHM files. Persistent WAL permits concurrent readers
and one native SQLite writer at a time. Native rollback-journal recovery is
owned only by explicit migration. Windows, network filesystems, and multi-host
SQLite are unsupported.

These filesystem controls operate within a local same-effective-user trust
boundary. They reject unsafe modes, links, nonregular files, and observable
root or main-database replacement. SQLite-managed WAL/SHM pathnames may
legitimately disappear or be recreated by a cooperating peer while another
connection retains an internal descriptor, so every current pathname is
revalidated but cross-process sidecar inode continuity is not claimed. Full
no-follow open/fstat validation is restricted to before SQLite opens and after
it closes; live checks use descriptor-relative no-follow metadata lookup so
the adapter never closes an independently opened descriptor onto an inode
whose POSIX advisory locks SQLite may hold. These controls are not a sandbox
against a malicious process running as the same operating-system user. Keep
the data root private and do not run untrusted same-user processes against it.

`aipcs serve --profile sqlite` is the sole public composition path: it obtains
the opaque location from resolved configuration, calls `migrate()` once before
MCP construction, and proceeds only at the exact ready revision. `config show`
and `config validate` do not construct an adapter, open the registry, create a
directory, or migrate. A failed startup does not start a partial MCP server.

`ServiceStoreLocator` is an opaque logical identifier for a service-store
allocation. It is exactly `svc_` followed by the lowercase
32-character hexadecimal form of a non-zero service UUID. It is not a path,
URI, DSN, host, database name, credential, SQL identifier, or agent-authored
domain name.
`StorageSummary` uses the same exact namespace grammar when this logical
identifier is later projected publicly; it is never a second free-form label.

`StorageAdapterInfo` says which of the closed `registry` and `service_store`
components a SQLite or future PostgreSQL adapter owns. It does not say that a
configured store is present, compatible, migrated, or ready. That dynamic
state is represented separately by `MigrationState`.

Migration revisions are adapter-private positive integers. `applied_revision`
uses zero only when no adapter migration is applied. `ready` means it equals the
adapter target revision; `uninitialised` means zero and is migratable only after
the future adapter verifies an absent or empty physical store. An unversioned
non-empty store, unknown/newer revision, missing required ledger, or checksum
mismatch is `incompatible`; an interrupted known migration is `dirty`. Ordinary
migration does not repair either state. Revisions are not package, MCP,
manifest, schema, record, or export versions.

`inspect_migration()` is observation only. `migrate()` is the sole explicit
initialisation or layout-changing operation. The public runtime uses only the
registry migration at SQLite startup; no tool call allocates or migrates a
service store. Inspection uses one read snapshot and changes no application,
schema, ledger, or journal-mode state. A valid SQLite WAL open may still
create, rebuild, retain, replace, or remove SQLite-managed WAL/SHM operational
files; every observable file remains inside the same secured location policy.
Safe diagnostics remain intentionally bounded.

Registry R3 and service-store R2 record the WAL physical policy as exact
checksummed migrations. Each database contains one permanent singleton policy
row for `aipcs.sqlite.wal.v1`, in `prepared` or `ready` phase. Explicit
migration commits a dirty prepared predecessor in DELETE mode, switches the
persistent header to WAL, then commits the ready policy and target history.
Only exact predecessor/DELETE, prepared/DELETE, prepared/WAL, and ready/WAL
states are adopted; every other marker/header/ledger combination fails closed.
Each successful ready migration runs one PASSIVE checkpoint and independently
re-observes readiness.

`sqlite_busy_timeout_ms` configures Python connection waiting and SQLite's one
busy handler, from 1 through 30,000 ms (default 5,000). `StorageBusy` is created
only from a numeric SQLite BUSY-family primary result or a PASSIVE-checkpoint
busy indicator. It is not inferred from driver text, `SQLITE_LOCKED`, elapsed
wall time, or an uncertain post-commit outcome. The adapters contain no retry
loop.

The historical V1-08B registry R2 migration is a private sequential,
checksummed upgrade beneath the current R3 WAL policy.
It admits only an exact, clean, public-reachable R1 registry and otherwise
fails closed; it does not recalculate R1 evidence, relabel an R1 database, or
offer repair. Fresh creation applies the same R1 then R2 migration history.
The public `MigrationState` remains `uninitialised`, `ready`, `dirty`, or
`incompatible`: exact R1 below the target has no public upgradeable state.
R2 adds an internal positive `service_revision` epoch and nullable safe logical
materialisation values. The latter are only a closed backend choice and the
opaque `ServiceStoreLocator` namespace grammar already described here—never a
physical location, connection detail, credential, or service-store ledger.
Explicit R2 inspection and migration finalization validate both the exact
physical signatures and every stored service, typed mutation phase/result, and
audit row. Canonical but semantically forged row data therefore fails closed as
incompatible without a repair write. Ordinary unit-of-work open does not add an
unbounded full-registry scan; each row codec remains strict when that row is
used.

The historical R2 layout retains one global registry mutation/idempotency ledger across legacy and
future lifecycle work. Exact legacy completed replays keep their stored result
bytes; a future lifecycle intent is `prepared`, `completed`, or
`recovery_required`, with prepared and recovery-required rows acting as the
per-service transition blocker and completion releasing it. This describes
durable registry state only. It does not compose a service-store coordinator,
inspect a service store, or expose a lifecycle operation.

Registry unit-of-work callers close every successfully acquired unit exactly
once. A successful commit or rollback ends its transaction attempt, then close
releases resources. Closing an unterminated unit performs adapter-safe rollback
cleanup. The original application failure wins over rollback or close failures;
a close failure after an otherwise successful operation becomes a bounded
internal failure. A commit exception has indeterminate durable outcome, so a
caller receives a bounded failure, resources are closed, and a retry relies on
the existing idempotency replay semantics.

Repository and mutation-ledger values cross the port as detached snapshots.
They must not retain a connection-backed cursor or a mutable reference to
durable adapter state after the unit of work closes.

A registry adapter's information includes `registry`, and its migration state
is always for that component. The current public lifecycle persists registry
metadata, idempotency outcomes, and metadata-only audit information within one
unit of work. Audit data and principal identity are never projected by MCP.

R2 bounds that metadata-only audit store to the newest 1,000 rows per principal
by audit identifier. The migration removes only older over-cap audit rows, and
an audit append trims older rows for its own principal in the same registry
transaction. The cap never prunes mutation, idempotency, or lifecycle evidence;
it has no configuration, environment, CLI, MCP, or audit-query surface.

The private SQLite service-store catalog declares only `service_store`
ownership. `allocate()` purely derives the canonical locator and performs no
filesystem or registry I/O. `inspect_migration()` does not create a directory,
database, table, migration row, or journal-mode change, subject to SQLite's
documented operational WAL/SHM open effects above. Explicit `migrate()` maps a
validated locator to the adapter-private layout below and initialises the R1
predecessor followed by the R2 WAL policy:

```text
<sqlite-data-root>/
  registry.sqlite
  service-stores/
    svc_<32 lowercase hexadecimal characters>.sqlite
```

The container is `0700`; every selected database is an owned, single-link
regular `0600` file. The database contains three reserved adapter tables:
service-store metadata, its independent checksummed migration ledger, and the
WAL policy marker. The
metadata binds the database to the exact locator namespace, so copying or
renaming a valid database to a different locator cannot report ready. It stores
no principal, domain name, manifest, schema version, lifecycle state, audit,
record, branch, or idempotency data. Adapter readiness validates its reserved
objects only; future manifest-aware validation of domain objects belongs to the
materialisation slice.

The catalog rejects a foreign-backend locator before I/O, reports migration
state only for `service_store`, and does not repair dirty or incompatible
state. It is packaged for internal composition work but is not constructed by
the current runtime or application. Direct initialisation can create an orphan
database because there is deliberately no registry transition or cross-store
operation record in this slice. Public materialisation must remain unavailable
until that operation becomes recoverable. No record operation, PostgreSQL
adapter, repair, backup, import/export, or cross-store transaction exists yet.

The reusable test-only conformance cases in `tests/storage_contracts/` assert
registry application and service-catalog behavior without treating SQL,
filesystem layout, connections, or drivers as portable contract details. They
are not shipped as a storage adapter.

## Private SQLite domain-schema store

`SQLiteDomainSchemaStore` is the private SQLite implementation of the
`DomainSchemaStore` protocol. It uses the same descriptor-anchored location
policy and opaque locator mapping as `SQLiteServiceStoreCatalog`: a valid
SQLite locator selects only
`service-stores/svc_<32 lowercase hexadecimal characters>.sqlite` below the
configured SQLite data root. It does not allocate a locator, create a
directory or database, compose with the registry, or add a public storage
adapter surface.

Every `inspect()` and `materialise()` call first requires that selected,
already-existing database to pass the exact revision-2 WAL-ready service-store foundation
inspection on the very connection used for the domain operation. A missing,
empty, dirty, incompatible, copied, or otherwise non-ready foundation is a
`StorageMigrationError`, not a domain `unmaterialised` result. In particular,
all domain objects being absent is `unmaterialised` only after the exact
foundation metadata and migration ledger prove readiness; without that ledger,
the store must not infer whether the empty domain is a fresh service or an
unknown database. Invalid locators, specifications, and transitions are
`StorageContractError`; unavailable or unsafe storage remains a bounded
`StorageUnavailable` error.

Against a ready foundation, inspection compares the non-reserved database
objects exactly with the supplied compiled relational specification. It returns
`unmaterialised` when every domain table and index is absent, `ready` only for
the complete exact layout with a clean foreign-key check, and `incompatible`
for any partial, extra, altered, or orphaned domain layout. These states are
relative to that supplied specification and are not persisted as a manifest,
schema version, fingerprint, or second ledger. Inspection never repairs.
Because no domain-schema ledger is stored, a ready foundation after complete external deletion of
every domain object is observationally identical to a never-materialised domain and is therefore
also `unmaterialised`; any partial disappearance remains detectable as `incompatible`. Later
registry-lifecycle composition supplies the historical context needed to treat an unexpectedly
empty previously materialised service as a cross-store recovery case.

For domain tables only, exact SQL fidelity means the same fail-closed SQLite
lexical-token sequence with inter-token ASCII whitespace ignored. Quoted
identifiers and literals, case, spelling, punctuation, constraints, comments,
and every other token remain exact; malformed or unsupported SQL is
incompatible. This accommodates SQLite's whitespace rewrite after an accepted
`ALTER TABLE ... ADD COLUMN` without accepting a semantic rewrite. Object
names, table and foreign-key signatures, internal objects, explicit-index SQL
and signatures, and `foreign_key_check` remain exact.

`materialise()` supports initial physical DDL only, and therefore accepts only
`schema_version == 1`. It atomically creates the deterministic tables and
ordered explicit indexes only from `unmaterialised`; repeat materialisation of
the exact layout is a no-op, and an incompatible layout is preserved. SQLite
foreign keys are named, `ON DELETE RESTRICT ON UPDATE RESTRICT`, immediate,
and never `DEFERRABLE`; nullable foreign-key fields are the supported way to
stage otherwise cyclic data. `evolve()` deeply revalidates one caller-supplied,
adjacent additive transition, then under one `BEGIN IMMEDIATE` transaction accepts an
exact target as a no-op or an exact current layout as the source for canonical
DDL. It may create new entity tables, append nullable fields to existing
tables, and create new explicit indexes; relationships are emitted only from a
new source table. It never rebuilds, renames, drops, copies, backfills,
rewrites, or repairs a table. If neither current nor target is exact, the
layout is `incompatible` and remains untouched. The target is the physical
authority for repeat safety, including metadata-only transitions; no schema
version, transition, or ledger is persisted. Python object identity is not a
provenance seal.

This private store is not composed by the runtime, MCP tools, CLI,
configuration, registry lifecycle, or record surface. It provides no domain
rows, record operations, migration history, schema repair, service allocation,
or cross-store transaction.

## Private relational schema contract

`DomainSchemaStore` is a private, backend-neutral protocol for a later domain
schema adapter. It accepts an opaque `ServiceStoreLocator` and an immutable
compiled relational specification, or a deeply revalidated additive transition. Its
only operations are inspection relative to a supplied specification, exact
initial materialisation, and exact transition. Inspection reports only
`unmaterialised`, `ready`, or `incompatible` relative to that supplied target.

The specification is a pure projection of a validated manifest: schema version;
named entities and declared-order fields; named relationships; and named,
ordered indexes. Relationships carry the fixed v1 `restrict` update/delete
policy and `immediate` constraint timing. Required relationship cycles are rejected; nullable
cycles are staged through null relationship values rather than temporarily invalid transactions.
The specification excludes descriptions, retrieval
metadata, facets, query patterns, and allowed values. Its exact immutable value
is the comparison authority; the port stores no manifest, schema version,
fingerprint, history, path, DSN, SQL, credentials, or schema ledger.

The supported transition grammar is intentionally additive: new entities,
nullable append-only fields, explicit new indexes, and approved application-only
metadata changes (including typed allowed-value expansion). It rejects
renames/removals, rebuilds, required additions, relationship retrofit on an
existing source table, index mutation, and allowed-value narrowing. The
registry-held manifest is the architectural authority, but this private store
does not read or cross-check registry state; later lifecycle composition owns
that coordination. This port is uncomposed in the current runtime. It does not
materialise a public service or alter the public MCP, CLI, configuration, or
lifecycle surface.
