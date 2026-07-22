# Storage contracts

V1-06A defines pure, backend-neutral contracts. V1-06B adds a private SQLite
registry reference adapter behind those unchanged contracts; it does not make a
storage profile runnable or add a public storage capability report.

The reference adapter is certified only for local POSIX filesystems on Linux
and macOS, one host and one active writer. It uses a descriptor-anchored `0700`
root and `0600` registry file, rejects WAL/SHM, allows native rollback-journal
recovery only during explicit migration, and fails closed on Windows.

`ServiceStoreLocator` is an opaque logical identifier for a future
materialised service store. It is exactly `svc_` followed by the lowercase
32-character hexadecimal form of a non-zero service UUID. It is not a path,
URI, DSN, host, database name, credential, SQL identifier, or agent-authored
domain name.
`StorageSummary` uses the same exact namespace grammar when this logical
identifier is later projected publicly; it is never a second free-form label.

`StorageAdapterInfo` says which of the closed `registry` and `service_store`
components a future SQLite or PostgreSQL adapter owns. It does not say that a
configured store is present, compatible, migrated, or ready. That dynamic
state is represented separately by `MigrationState`.

Migration revisions are adapter-private positive integers. `applied_revision`
uses zero only when no public migration is applied. `ready` means it equals the
adapter target revision; `uninitialised` means zero and is migratable only after
the future adapter verifies an absent or empty physical store. An unversioned
non-empty store, unknown/newer revision, missing required ledger, or checksum
mismatch is `incompatible`; an interrupted known migration is `dirty`. Ordinary
migration does not repair either state. Revisions are not package, MCP,
manifest, schema, record, or export versions.

`inspect_migration()` is observation only. `migrate()` is the sole explicit
initialisation or layout-changing operation. Their concrete I/O and safe
operator diagnostics are deferred to the adapter slices.

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
is always for that component. A service-store catalog declares its backend and
`service_store` ownership; allocated locators use that backend, foreign-backend
locators fail safely, and its migration state is always service-store state.

The reusable test-only conformance cases in `tests/storage_contracts/` assert
application behaviour and UoW traces without SQL, storage layout, connection,
or driver assertions. They are not shipped as a storage adapter.
