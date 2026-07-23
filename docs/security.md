# Security and trust boundary

AIPCS treats agent-provided tool arguments, schemas, and configuration as
untrusted. Public validation happens before lifecycle persistence. The runtime
also confines storage implementation details behind bounded startup and tool
errors.

## Input, configuration, and transport

- Public models reject unknown fields and enforce bounded, typed values.
- Service identifiers must be lowercase canonical UUIDs.
- Retired fields, principal or provenance controls, storage locations, backend
  or connection fields, and endpoint settings are rejected from normal input.
- Manifest v1 is accepted only by the explicit one-way converter, never by
  normal public design input.
- Configuration is strict TOML selected only with an explicit `--config` path.
  It has no implicit file discovery, dotenv loading, include mechanism, or
  literal credential field.
- Configuration reports are allowlisted and redact principal values, file and
  storage paths, DSN-reference names, secrets, endpoints, raw TOML, and raw
  environment values.
- Public v1 is stdio only. Listener-oriented transport settings are rejected
  before configuration resolution and before any MCP server construction.
- `aipcs_service_seed` and `aipcs_service_design` require bounded idempotency
  keys. Same-key, same-request retries replay the prior result; a different
  request using the same key fails with `conflict`.

The configured SQLite principal is opaque and process-local. It is neither an
operating-system identity nor MCP client identity, authentication, or tenancy.
It is never accepted in a tool request or exposed in a result, config report,
error, log, or representation. The transport owns the fixed `created_via: mcp`
provenance value; callers cannot override it.

SQLite is supported only for a local POSIX filesystem on Linux and macOS, one
host, Python 3.12+, SQLite 3.51.3+, and cooperating processes under the same
effective user. Its location policy enforces an operator-owned `0700` root, an
owner-only service-store container, and `0600` database/WAL/SHM files.
Persistent WAL permits concurrent readers and one SQLite writer at a time.
Every observable operational sidecar is a contained regular single-link,
same-owner, non-symlink file with descriptor-relative metadata checks. Full
no-follow `openat`/`fstat` validation occurs before SQLite opens and after it
closes. While a SQLite handle is live, checks use descriptor-relative
no-follow metadata lookup without opening and closing another descriptor:
POSIX `close()` can cancel SQLite advisory locks on the same inode. A
cooperating SQLite peer may also unlink or recreate WAL/SHM pathnames while
another connection retains SQLite's internal file descriptor, so the adapter
revalidates each current pathname rather than claiming cross-process path
identity continuity. See SQLite's
[POSIX advisory-lock warning](https://www.sqlite.org/howtocorrupt.html#_posix_advisory_locks_canceled_by_a_separate_thread_doing_close_).
The adapter never edits, copies, deletes, or repairs WAL/SHM content.

`serve` performs only the explicit registry startup migration; it fails before
MCP construction when storage is busy, unsafe, dirty, incompatible, newer, or
not ready. Only explicit migration owns DELETE-to-WAL conversion, legacy
rollback recovery, and the ready PASSIVE checkpoint. One bounded
`sqlite_busy_timeout_ms` setting configures SQLite lock acquisition from 1
through 30,000 ms (default 5,000); numeric BUSY-family outcomes become
`StorageBusy` without an adapter retry. They remain distinct from unavailable,
incompatible, `SQLITE_LOCKED`, and uncertain commit outcomes. WAL does not
extend support to network filesystems, multi-host use, Windows SQLite, or a
hostile same-user process.

V1-08C establishes the final local SQLite policy under which V1-08D's private
cross-store coordinator is tested. V1-08D reruns coordinator
reconciliation/fault and installed-artifact proof under that policy before
V1-08E may expose lifecycle operations.

The coordinator treats a dirty service-store foundation as one bounded
recovery check, not immediate corruption: the existing migration action may
resume an exact AIPCS-prepared WAL phase, then fresh observation decides.
Persistent generic dirt becomes recovery-required and is never repaired.

V1-08B is deliberately narrower than the V1-08D coordinator. Its historical
R2 registry migration accepts only exact, clean, public-reachable R1 state and
otherwise fails closed with the existing bounded migration outcome; it adds no
public upgrade, repair, or recovery command. That R2 layout holds one global
durable lifecycle/idempotency ledger whose prepared entries can block a
conflicting lifecycle intent for the same service. Its legacy completed replays
retain their existing result bytes, while lifecycle evidence and its bounded
terminal category are never projected through MCP. Explicit inspection and
migration finalization strictly decode all registry rows in addition to checking
physical signatures, so canonical-but-forged service, lifecycle, completion,
or audit data is incompatible and is never silently repaired. V1-08C advances
that registry to R3 and does not change runtime composition, MCP registration,
or service-store access.

Filesystem validation is a local same-effective-user boundary, not isolation
from a malicious process running as that user. Descriptor-anchored operations
reject unsafe ancestors, containers, links, file types, modes, sidecars, and
observable replacement, but a hostile same-user process is already inside the
operator's filesystem authority. Keep the data root private and do not expose
it to untrusted same-user workloads.

## Safe responses

Tool failures use structured envelopes with a stable code, concise message, and
bounded remediation issues. Validation failures are mapped to public-facing
issues; tracebacks, database details, SQL, credentials, registry names, local
paths, principal values, payload echoes, and audit data are not part of the
contract. Startup failure produces one bounded `internal_error` on stderr and
does not begin MCP.

The MCP boundary owns conversion before framework validation can leak an
exception string. Valid tool requests with unknown, malformed, badly typed, or
oversized object arguments receive a safe envelope. A non-object MCP
`arguments` value is rejected by the SDK with its fixed JSON-RPC `-32602`
invalid-params response rather than an AIPCS envelope. A malformed JSON line is
rejected before dispatch: no application-envelope or response-shape guarantee
is made, but the process must remain safe for a subsequent valid request.

Capability information intentionally reports only public features: package and
contract versions, manifest support, enabled transport, and the
`registry_lifecycle` capability. It must not expose credentials, DSNs,
filesystem locations, principal or owner information, audit data, or network
endpoints. The capability is true only when a ready SQLite process registers
all four lifecycle tools; an unavailable profile is not a server capability.

The opaque `svc_` locator namespace is a non-secret logical identifier.
Physical data-root and database paths remain private: they are not locator
fields and must not appear in responses, capabilities, errors, logs, audit
records, or representations.

R2's reserved materialisation metadata is limited to a safe logical backend
and that opaque namespace. Public current lifecycle results continue to return
null materialisation fields and omit the internal service revision. The
metadata-only audit store retains at most the newest 1,000 entries per
principal by audit identifier; this retention bound is private, is not an audit
query feature, and never prunes idempotency or lifecycle evidence.

When V1-08E later adds lifecycle recovery projection, it may expose only
`service_revision` and `recovery_state: clear | pending | recovery_required`. It must not expose
an idempotency key, fingerprint, operation identifier, target snapshot, phase timestamp, fault
text, repair procedure, SQL, locator, path, principal, or audit content. A recovery-required state
is deterministic and non-auto-repaired through V1-08; no repair command is implied.

The future error envelope freezes retryability without exposing backend detail: malformed input,
unsupported transition, stale expected revision, changed-fingerprint reuse, recovery-required,
storage-unavailable, and generic internal failure are non-retryable; storage-busy, different-key
operation-in-progress, and operation-uncertain are retryable. An exact same-key prepared claim
resumes reconciliation rather than returning operation-in-progress.

## Test data and operational boundary

All repository test data is synthetic and marked with its provenance. Do not
add operational stores, database copies, credentials, or personal context to
this repository.

The registry stores service metadata and initial manifests only. A public
design does not invoke the packaged private service-store catalog,
domain-schema adapter, or lifecycle coordinator; it creates no service
database or agent-defined table and makes no records durable. The private
coordinator admits supported work before physical I/O, closes every registry
UoW before service-store access, exposes only bounded result categories, and
persists no path, credential, SQL, driver error, or physical locator. It is
packaged for internal restart and release proof but has no runtime, MCP, CLI,
or configuration composition. Direct catalog/domain use can still leave an
orphan database or physical schema and is not a public workflow. No public
materialisation, records, branches, retrieval, backup/export, repair, operator
administration, remote transport, PostgreSQL, or multi-user tenancy is
implemented. Future slices must preserve the same boundary: validate input
before persistence, keep configuration separate from data, and redact
sensitive implementation details from errors and capability output. See the [private relational
boundary](private-relational-boundary.md).

There is not yet a supported release, maintenance, or security-fix policy.
Treat this pre-release contract as development software and report potential
security issues through the project's future published security channel.
