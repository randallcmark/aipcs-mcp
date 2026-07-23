# Security and trust boundary

AIPCS treats MCP arguments, manifests, record values, cursors, configuration,
and durable storage as untrusted at each boundary. Inputs are validated before
persistence; failures are bounded and redacted.

This document describes the current source contract. There is not yet a
published supported-release, maintenance, vulnerability-reporting, or
security-fix policy.

## Input boundary

- Public request models reject unknown fields and enforce strict types and
  documented bounds.
- Service, record, and branch identifiers are canonical non-zero UUIDs.
- Caller records contain domain fields only. Server-managed identity,
  principal, timestamps, provenance, and revisions are rejected.
- Search accepts at most 16 declared filters. Identifiers and predicates are
  compiled from the validated manifest; caller SQL is never accepted.
- Scalar filters are exact, membership filters accept one string, and
  annotations are not filters.
- Cursors are opaque, integrity-checked, and bound to the originating query.
- Manifest v1 enters only through the explicit one-way converter.
- Storage locations, backend controls, credentials, endpoints, principal
  values, and operation evidence are not tool arguments.

Record JSON is bounded to 64 KiB, strings to 16 KiB, string lists to 256
distinct items, pages to 100, branch assignment targets to 100, bootstrap to
100 services, summary facets to 20 values, summary branches to 100, samples to
three records per entity, and maintenance to 100 candidates.

## Principal and provenance

The configured SQLite principal is an opaque process-local ownership boundary,
not operating-system identity, MCP client authentication, or hosted tenancy.
The process uses one principal for its lifetime. Tools neither accept nor
return it.

The transport fixes `created_via` to `mcp`. Public records may return this safe
provenance label but omit `owner_id`. Cross-principal lookup is
indistinguishable from absence or stale state as appropriate; counts, facets,
branches, samples, maintenance, replay keys, and cursors are all
principal-scoped.

## Replay and concurrency boundaries

Seed/design and materialise/evolve mutations use the registry's global
principal/idempotency namespace. Lifecycle intent may be prepared, completed,
or recovery-required and is the only cross-store transition/recovery authority.
Public projections do not expose its key, fingerprint, target snapshot,
timestamps, fault text, or internal phase.

Record and topology mutations use a separate service-local R3 ledger. It
stores only completed outcomes atomically with local data/history changes.
It cannot block schema lifecycle, become current manifest authority, or imply a
cross-store transaction. Same-key changed content fails; exact retries replay
without another mutation.

Optimistic revisions protect service, record, and branch writes. Effective
topology assignment also compares and advances affected record revisions.
Storage busy and uncertain outcomes are not converted into success from
exception text; callers retry the exact request and key only when the public
error marks it retryable. Concurrent same-key lifecycle reconciliation may
surface retryable uncertainty before the terminal registry write is observed.
Repeated coarse `dirty` storage evidence cannot by itself make
recovery-required durable.

## Discovery and authority signals

Bootstrap is shape-only and value-free. It reads registry projection and never
opens service stores.

Summary may return bounded principal-scoped record samples and observed
declared domain facets. It does not return principal identity, replay evidence,
storage paths, or private SQL. Authority-field availability reports only
recognised fields declared by the manifest; it is not a truth judgement.

Maintenance is advisory and read-only. Its expired, stale, low-confidence,
superseded, missing-authority, unbranched, duplicate-authority, and
annotation-size signals are mechanical. Candidate details omit record prose
and authority-reference values. The server does not rank truth, infer semantic
duplicates, merge, archive, delete, purge, or rewrite records.

## SQLite filesystem boundary

SQLite is supported only on Linux/macOS, a local POSIX filesystem, one host,
SQLite 3.51.3 or newer, and cooperating processes under the same effective
user.

The location policy requires an operator-owned `0700` data root, an owner-only
service-store container, and `0600` database/WAL/SHM files. Descriptor-relative
no-follow checks reject unsafe ancestors, symlinks, unexpected file types,
links, ownership, modes, and observable substitution.

Persistent WAL permits concurrent readers and one SQLite writer. A single
bounded timeout controls lock acquisition; the adapter performs no internal
BUSY retry. WAL/SHM pathnames may legitimately be recreated by cooperating
SQLite peers, so validation checks the current contained pathname without
claiming hostile cross-process identity continuity.

This is containment under one effective user, not isolation from a malicious
process already running as that user. Keep the data root private. Network
filesystems, multi-host access, Windows SQLite, and hostile same-user workloads
are unsupported.

Do not copy, delete, edit, or repair live WAL/SHM files and do not treat the
main `.sqlite` file alone as an online backup. No supported online backup or
export command exists yet.

## Migration boundary

`serve` explicitly migrates the registry before MCP construction. Failure
prevents the server from starting.

Service-store reads never execute DDL. An exact clean R2 store is
migration-required. Only an admitted lifecycle, record, or topology mutation
may request the exact R2-to-R3 migration. A prepared materialise intent may
resume an exact clean R2 intermediate committed by a cooperating migration;
ordinary reads still do not upgrade it. Exact known prepared states may
resume; altered, partial, generic dirty, substituted, unknown, or future
storage fails closed and is not repaired.

Domain layout inspection compares the supplied manifest-derived specification
with exact tables, columns, indexes, foreign keys, and reserved-object
boundaries. Private direct adapter use can create orphan state and is not a
supported workflow.

## Safe responses and redaction

Tool failures use stable codes, concise messages, bounded validation issues,
and explicit retryability. They do not include:

- tracebacks or exception strings;
- SQL, database/table names, migration internals, or driver codes;
- filesystem paths, DSNs, endpoints, or credentials;
- principal values or `owner_id`;
- idempotency keys, fingerprints, lifecycle intent, or audit rows; or
- echoed record prose or authority references.

Startup failure emits one bounded error on stderr and does not begin MCP.
Stdout belongs exclusively to stdio MCP. A malformed JSON line may be rejected
by the underlying protocol before AIPCS dispatch, but it must not reveal
private state.

Server info reports only package/contract versions, manifest support,
transport, and enabled public feature flags. The safe `svc_` namespace may
appear after materialisation; the physical locator and data root may not.

Configuration reports are allowlisted and redact principal values, config
paths, storage roots, DSN-reference names, raw environment/TOML, endpoints, and
secrets. Configuration has no dotenv loading, implicit file discovery, include
mechanism, literal credential field, or remote listener setting.

## Repository boundary

Repository tests and examples are synthetic contract fixtures. Do not commit
operational databases, snapshots, transcripts, credentials, personal context,
or agent-specific operating instructions to the public repository.

PostgreSQL, remote MCP, authentication, hosted tenancy, export/import/purge,
repair, and administration workflows are deferred and must define their own
trust boundaries before release.
