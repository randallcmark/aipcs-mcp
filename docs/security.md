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
host, and one active writer. Its location policy enforces an operator-owned
`0700` root, an owner-only service-store container, and `0600` database files.
It rejects WAL/SHM state and fails closed on Windows. `serve` performs only the
explicit registry startup migration; it fails before MCP is constructed when
that storage is unsafe, dirty, incompatible, newer, or not ready. Do not use
this boundary as a network-filesystem, multi-writer, remote, or PostgreSQL
deployment mechanism.

V1-08A freezes a future storage-policy boundary without changing this current behavior. V1-08C must
replace the rollback-journal envelope with a secured WAL/busy policy before V1-08D composes the
cross-store coordinator: WAL and SHM sidecars must be contained regular single-link,
same-owner, non-symlink files with adapter-controlled permissions and identity checks; only
startup/migration owns recovery and checkpoint verification; and one bounded typed busy policy maps
to a safe retryable result. WAL does not extend support to network filesystems or Windows SQLite.
V1-08D must rerun its full coordinator reconciliation/fault matrix under that final policy before
V1-08E can expose lifecycle operations.

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

The registry stores service metadata and initial manifests only. A design does
not invoke the packaged private service-store catalog or domain-schema adapter,
create a service database or agent-defined table, or make records durable. The
catalog can be exercised directly as an internal foundation and creates only
namespace-bound adapter metadata and its checksummed migration ledger; the
separate private domain adapter can operate on that ready foundation only
through direct internal code. Because both are uncomposed, direct use can leave
an orphan database or physical schema and is not a public workflow. It adds no
MCP tool, CLI command, configuration option, capability, repair, or recovery
path. Public materialisation, records, branches, retrieval, backup/export,
cross-store recovery, operator administration, remote transport, PostgreSQL,
and multi-user tenancy are not implemented. Future slices must preserve the
same boundary: validate input before persistence, keep configuration separate
from data, and redact sensitive implementation details from errors and
capability output. See the [private relational
boundary](private-relational-boundary.md).

There is not yet a supported release, maintenance, or security-fix policy.
Treat this pre-release contract as development software and report potential
security issues through the project's future published security channel.
