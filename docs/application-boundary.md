# Application boundary

The application boundary is an internal construction seam. It separates stdio
MCP and CLI adapters from use cases, and keeps future storage mechanics outside
the application layer. It does not add a storage backend, persistent service or
record operation, lifecycle operation, MCP tool, CLI command, or public adapter
extension mechanism.

## Principal context

Every future application use case receives an explicit opaque `principal_id`
and `created_via` context from its transport adapter. It must not infer identity,
ownership, or authority from environment values, filesystem state, connection
details, or a storage backend. This keeps ownership checks testable and allows
future authentication work to remain a transport concern.

## Unit of work ownership

A service mutation, its idempotency-ledger outcome, and its audit entry belong
to the same unit of work. The application use case owns the requested operation
and its business outcome; the adapter owns transaction scope, rollback, and
durable commit. A failed ledger or audit write must fail the mutation rather
than leave an unaccounted state change.

This boundary does not yet provide a concrete production unit of work or audit
store. A successfully acquired unit is always closed exactly once. `commit()`
and `rollback()` terminate its transaction attempt; `close()` releases
resources and performs adapter-safe cleanup of an unterminated attempt. The
application preserves the original failure when rollback or close also fail. A
close failure after an otherwise successful operation is a bounded internal
failure. A commit failure has indeterminate durable outcome, so callers retry
through idempotency rather than assuming a rollback erased it.

## Independent versions

Three version dimensions remain separate:

- Schema version describes the agent-defined manifest and its supported
  evolution.
- Adapter migration version describes physical storage layout and adapter-owned
  migration state.
- Operation state describes the future progress and recovery status of work that
  crosses independently committed stores.

A schema change is not an adapter migration, and neither is a substitute for a
durable operation/recovery record.

## Migration and serving rule

A future adapter applies its required migrations before it reports itself ready
to serve. Migration failure prevents serving. Read operations must not execute
DDL, and opening a record or branch connection must not opportunistically alter
storage layout.

The application layer requests behavior through its boundary; it never creates
tables, parses storage locations, opens database connections, or selects a
backend.

See [storage contracts](storage-contracts.md) for the pure future-adapter
vocabulary and migration-state boundary.

## Current capability

The current stateless server still exposes only aipcs_server_info over stdio.
Storage, service and record operations, schema application, lifecycle,
export/import, administration, and persistent configuration profiles remain unavailable.

See [compatibility](compatibility.md) and [security](security.md).
