# Private relational boundary

`aipcs-mcp` packages private SQLite and coordination seams so the distribution
can prove recoverable relational lifecycle behavior: the service-store catalog
establishes an opaque contained database foundation, the domain-schema store
compares, materialises, and evolves one supplied relational specification, and
the transport-neutral lifecycle coordinator reconciles those stores with
durable registry intent. None is a public API, MCP capability, CLI command,
configuration option, or operator workflow.

This guide explains that boundary for maintainers and release reviewers. It
does not provide an invocation recipe. The normative contract is in [storage
contracts](storage-contracts.md).

## What the private seam proves

The catalog creates only adapter metadata and its independent checksummed
foundation migration ledger. The domain-schema store works only after that
foundation is exactly ready, and only against a caller-supplied compiled
specification or deeply revalidated additive transition. It does not allocate
a service, create a directory or database, read or update registry state, or
provide records.

Relative to the supplied specification, an exact ready foundation with no
domain objects is `unmaterialised`; a complete exact domain layout is `ready`;
and partial, extra, altered, or orphaned domain objects are `incompatible`.
Inspection does not repair. Exact table inspection is fail-closed: only
inter-token ASCII whitespace may differ, while the remaining tokens and the
other object, pragma, and integrity signatures remain exact.

Initial materialisation accepts only schema version 1. A direct additive
evolution starts from an exact current layout or accepts an exact target as a
no-op. It can create new entity tables, append nullable fields to existing
tables, and add explicit indexes; it never rebuilds, renames, drops, copies,
backfills, or repairs data. The target is the physical authority for retry
safety, including metadata-only transitions. Installed release proof runs this
private behavior across a separate process restart.

## Authority and limits

The registry-held manifest is the architectural authority, but the private
seam deliberately does not cross-check or coordinate with that registry. A
direct private operation can therefore leave an orphan foundation or a
physical target that the public lifecycle does not know about. It is evidence
of adapter behavior, not a service lifecycle action.

Beyond the V1-06D foundation metadata and migration ledger, the service
database stores no domain manifest, domain schema version, transition,
provenance, domain-schema ledger, operation record, or repair state. Complete
external deletion of all domain objects is consequently indistinguishable from
never materialising them; partial deletion remains incompatible. Do not try to
repair or infer history from this seam.

The lifecycle coordinator implements the V1-08 recovery consequence behind
the public application boundary. The registry holds the sole current manifest and
may hold one immutable admitted target snapshot as lifecycle evidence; the
service database remains free of a manifest, schema-version row, fingerprint,
operation record, provenance seal, or domain ledger. Within the contained
same-operating-system-owner store boundary, an exact physical target that
predates a prepared intent is indistinguishable from a target committed
immediately before a crash and may be adopted by target-first finalisation.
This does not permit repair: any partial, extra, altered, incompatible, or
unexpectedly deleted state is recovery-required.

For a new key, relational compilation or additive-transition classification
occurs after registry blocker checks and before prepared intent is inserted.
Prepared intent commits and its registry UoW closes before catalog or
domain-schema I/O. Every physical action return is discarded and exact state
is re-observed through the pure recovery planner; the coordinator performs
each physical action at most once per call.

The bounded foundation reconciliation also resolves a SQLite concurrency
distinction. Crash-resumable WAL migration exposes its exact committed
`prepared` phases as `dirty`, so another same-key worker may see dirt while
valid migration is live. The coordinator runs `catalog.migrate()` for that
observation and re-observes within a fixed action budget. If the state remains
coarsely `dirty`, it returns retryable operation-uncertain and leaves the
registry intent prepared; repeated dirt alone is not terminal recovery proof.
An exact clean R2 `outdated` foundation is likewise a supported intermediate
that a prepared materialise or evolve operation resumes toward R3. Generic
historical dirt is not repaired, and exact incompatible evidence remains
recovery-required.

The coordinator preserves the frozen result contract: malformed input,
unsupported transition, stale expected revision, changed-fingerprint reuse,
recovery-required, storage-unavailable, and generic internal failure are
non-retryable; storage-busy, different-key operation-in-progress, and
operation-uncertain are retryable. An exact same-key prepared claim resumes
reconciliation rather than returning operation-in-progress. Inspection-time
migration errors and failures after a physical action or registry commit may
have taken effect are operation-uncertain; no exception is inferred as exact
incompatible state.

## V1-08B registry-only prerequisite

V1-08B gives the registry—not this private service-store seam—one durable
authority for a later lifecycle coordinator. Its R2 migration is a sequential,
checksummed upgrade only from an exact, clean, public-reachable R1 registry;
other states fail closed and receive no repair workflow. R1 remains immutable
historical migration evidence. R2 starts each internal server-owned
`service_revision` at 1 and reserves a safe logical backend/opaque namespace
pair for a later materialisation result. Neither value changes the current
public seeded result or identifies a filesystem location.

R2 extends the existing global idempotency ledger rather than creating a
service-store or per-lifecycle ledger. Strict completed legacy replays keep
their stored result bytes. Future materialise/evolve intent may be prepared,
completed, or recovery-required; prepared and recovery-required intent supply
the registry-side per-service transition blocker, while completion releases it.
The registry audit log remains metadata-only
and retains only the newest 1,000 rows per principal by audit identifier. That
bound never removes lifecycle/idempotency evidence and is not an operator or
public control.

V1-08B itself did not compose the service store, add a coordinator, observe or
reconcile physical state, change WAL/busy policy, create a runtime/configuration
surface, register MCP tools, or add a repair procedure. V1-08C then fixed the
private foundation at R2 under the same WAL policy as registry R3: local POSIX,
same-effective-user use; one writer with concurrent readers; a 1..30,000 ms
busy timeout (default 5,000); and `StorageBusy` without adapter retries.
V1-08D packages the private coordinator over those boundaries. V1-08E composes
it through the generic public lifecycle, and no repair procedure exists.

## Public boundary

The public runtime migrates the registry, then constructs the catalog,
domain-schema store, and coordinator from the same resolved SQLite policy
without opening a service store. A ready SQLite server exposes the 21-tool
lifecycle, record, discovery, topology, and maintenance surface. Design remains
seeded; only an admitted lifecycle, record, or topology mutation may create or
migrate service storage. Read-side calls never perform DDL. Public results
expose the terminal service or aggregate recovery state, never these private
objects or physical details. The CLI and configuration do not expose or
construct the coordinator independently.

V1-08 owns lifecycle concurrency and the record/discovery runtime. Its sequence
is lifecycle contract (A), durable intent (B),
SQLite WAL/busy policy (C), internal coordinator (D), public lifecycle composition (E), generic
records (F), and structured discovery/topology (G). These slices are now integrated under MCP
contract 1.2.0; PostgreSQL begins only after this milestone.
Treat the concrete catalog/domain/coordinator seam as an internal implementation
and release boundary, not a user-facing adapter or repair API.
