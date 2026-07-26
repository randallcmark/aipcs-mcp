# Security and trust boundary

AIPCS treats MCP arguments, manifests, record values, cursors, configuration,
and durable storage as untrusted at each boundary. Inputs are validated before
persistence; failures are bounded and redacted.

This document describes the current source contract. The public reporting and
maintenance posture is in the repository [security policy](../SECURITY.md):
the latest public release receives best-effort security fixes only, and GitHub
Private Vulnerability Reporting is the canonical report channel after public
visibility. The maintainer aims to acknowledge a private report within seven
calendar days when possible; this is not an SLA.

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

## Streamable HTTP boundary

Streamable HTTP serves the same finite MCP tool catalogue as stdio. It does
not derive an AIPCS principal from an HTTP client, authenticate a user, or
provide tenant isolation. Opaque MCP transport session IDs are managed by the
SDK solely to correlate requests; they are not authorization credentials.

The endpoint binds to `127.0.0.1` by default. It enables the SDK's DNS
rebinding protection, requires a configured Host header match, accepts an
absent Origin for non-browser MCP clients, and rejects every presented Origin
unless it exactly matches the configured allowlist. Idle sessions are bounded
and proxy-supplied forwarding headers are not trusted.

An operator may explicitly permit a non-loopback IP bind only with an explicit
Host policy. That setting acknowledges a trusted-network or reverse-proxy
topology; it does not make the process safe for untrusted clients. Terminate
TLS and enforce authentication, authorization, rate limits, and any tenant
mapping at a trusted gateway before traffic reaches AIPCS. Never expose an
unauthenticated persistent-principal endpoint directly to an untrusted LAN or
the Internet.

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

### SQLite WAL-reset safety baseline

SQLite 3.51.3 fixed a rare WAL-reset bug that can corrupt a database when two
or more connections write or checkpoint the same WAL-mode file at the same
instant. AIPCS uses WAL for persistent SQLite storage, so it certifies SQLite
3.51.3 or newer rather than trying to infer downstream distribution backports.
Some older patched builds may be safe, but they are outside the supported
baseline and are rejected deliberately. The requirement is a data-integrity
control, not a feature preference. See SQLite's
[WAL-reset bug documentation](https://www.sqlite.org/wal.html#walreset) and
[3.51.3 release notes](https://sqlite.org/releaselog/3_51_3.html).

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
main `.sqlite` file alone as an online backup. AIPCS does not supply an online
physical backup command. The administration CLI's logical export does not copy
or restore SQLite physical state.

## Portable artifact boundary

Portable bundles are untrusted binary input. The application coordinator
accepts a stream, not a caller path. It incrementally enforces exact canonical JSONL,
closed frame shapes, a 1 MiB frame limit, 256 MiB default/1 GiB absolute total
limit, 64-level nesting limit, 1,000,000-frame limit, ordered digests, manifest
and lifecycle rules, and every logical cross-reference before any registry or
service-store write. Dry run performs zero writes.

Validated input is held only in an owner-private temporary directory and
file, never followed through a caller-controlled symlink, and removed on
success or failure. Fixed failures do not echo payloads, paths, principals,
keys, fingerprints, DSNs, endpoints, SQL, or driver text. Bundles exclude
credentials and physical database facts, but may contain the logical memory
content being transferred and must be protected accordingly by the operator.
CLI export uses a same-directory owner-private
temporary regular file, fsyncs content, and publishes atomically without
replacement. Import opens read-only without following the final symlink and
requires a regular file. Paths are never passed to the coordinator or returned
in results and errors. Purge requires archived state, an exact revision and
operation id, a verified receipt or explicit override, and exact service-id
confirmation. Physical absence is re-observed before the registry publishes a
terminal tombstone.

SHA-256 frame, section, and root digests detect accidental tamper, truncation,
substitution, duplication, and reordering. They are not signatures,
encryption, authentication, hostile-author authenticity, replay protection
between installations, replication, or backup retention. The format does not
authenticate a remote sender or define network transport.

Suspend/archive close a service-local monotonic write fence before registry
status changes, preventing an already-admitted mutation from crossing the
transition. Purge is never inferred from archive, dormancy, a missing store,
or a bundle. It requires an archived service, exact revision, separate
authority, and verified receipt or explicit override; completion follows
independent physical-absence observation and leaves only a minimal tombstone.

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

## PostgreSQL trust boundary

PostgreSQL is a reference adapter for an operator-provisioned database, not a
hosted service or tenancy boundary. It uses one dedicated non-superuser role
and creates only the fixed `aipcs_registry` schema plus exact private
`svc_<uuid hex>` schemas. It does not create databases, roles, extensions, or
grant elevated capabilities. Public schema access must be revoked.

Configuration stores only the name of an environment variable containing the
DSN. Offline configuration commands do not read that variable. `serve` reads
the selected secret once during runtime construction; reports, exceptions,
logs, representations, and public failures never disclose the DSN-reference
name, DSN, credentials, host, port, database, schema identifiers, driver text,
or SQLSTATE. libpq TLS and certificate verification settings are operator
controlled through the DSN and are not weakened or inferred by AIPCS.

PostgreSQL tests may connect only to an exact disposable fixture that they
created with synthetic credentials, loopback-only random port publication, no
host mounts, and fixture labels. Cleanup targets that exact fixture. Tests do
not fall back to an ambient DSN, inspect or mutate unrelated containers, or
connect to an operator's running PostgreSQL server.

## Repository boundary

Repository tests and examples are synthetic contract fixtures. Do not commit
operational databases, snapshots, transcripts, credentials, personal context,
or private maintainer-specific operating instructions to the public repository.
Vendor-neutral agent-integration examples must remain explicitly scoped under
`examples/`.

PostgreSQL is a supported generic public-v1 reference backend when the package
is installed with its `[postgresql]` extra. Streamable HTTP supports a trusted
service deployment, while application-managed authentication, hosted tenancy,
physical backup/restore, and arbitrary repair remain deferred and must define
their own trust boundaries before release.
