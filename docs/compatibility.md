# Compatibility and release boundary

`aipcs-mcp` is pre-release (`0.0.0.dev0`). It has a narrow public stdio contract
for stateless capability discovery and SQLite-backed registry lifecycle. It is
not yet a supported release.

| Layer | Identifier | Current status |
| --- | --- | --- |
| Distribution | aipcs-mcp SemVer | Local development package and aipcs command; no released install. |
| MCP capability contract | aipcs_mcp_contract | Version `1.1.0`; safe capability, error, registry, and materialisation-lifecycle shapes. |
| Schema manifest | manifest_version | Manifest v2 is the only normal public design input. |
| Configuration document | config_version | Strict V1 configuration document and source precedence. |
| Legacy conversion | Explicit manifest-v1 converter | One-way library conversion with provenance and warnings. |
| Storage migration | Adapter revision | SQLite registry R3 and private service-store R2; adapter-private checksummed WAL policy, foundation readiness, and relational fidelity checks. |
| Export bundle | export_format_version | Not implemented. |

## Current contract

Normal design input accepts manifest v2 only. The explicit legacy converter is
the sole v1 entry point: it produces a v2 manifest and reports discarded fields
and warnings. It does not write data, create storage, or promise a reversible
round trip. It resets the converted document to public-v2 schema version 1 when
legacy history cannot be retained; it never invents a v2 transition chain.

The `aipcs serve` command starts over stdio only. Stateless exposes only
`aipcs_server_info`. A ready SQLite profile exposes server-info plus
`aipcs_service_seed`, `aipcs_service_list`, `aipcs_service_inspect`, and
`aipcs_service_design`, `aipcs_service_materialise`, and
`aipcs_service_evolve`. Server-info's `registry_lifecycle` feature is true only
when the four registry tools are registered after a ready startup migration;
`materialisation_lifecycle` is true only when both physical lifecycle tools and
their coordinator are fully bound. It deliberately omits storage locations,
credentials, network endpoints, principal identity, operation evidence, and
audit data.

AIPCS configuration is resolved by explicit CLI option, documented environment
variable, selected TOML file, and safe default in that order. `config show` is
redacted. `config validate` and `serve` succeed only for a runnable profile.
Stateless is runnable everywhere supported by the package. SQLite is
structurally runnable only on Linux/macOS with SQLite 3.51.3 or newer, subject
to live storage checks at `serve`. Windows SQLite and PostgreSQL are
unavailable. Configuration inspection does not claim a path is ready and does
not touch storage.

The SQLite certification boundary is one host on a local POSIX filesystem with
cooperating processes under the same effective user. WAL permits concurrent
readers, while SQLite serialises writers; it is not multi-host, network-
filesystem, Windows, or hostile-same-user support.

The SQLite lifecycle is principal-scoped. A configured principal is an opaque,
process-local boundary selected by the operator, not client identity or hosted
tenancy. The MCP transport fixes `created_via` to `mcp`; callers cannot set
either value. Mutation retries use a required idempotency key. Repeating the
same request replays the durable result; reusing the key with different content
returns `conflict` for seed/design and `changed_fingerprint` for materialise/evolve.

Design accepts and stores an initial manifest-v2 document but does not itself
create a service store. Materialise turns an active designed seed into an exact
SQLite relational schema, and evolve applies one complete validated adjacent
additive target. Neither operation creates records or generated tools.

The distribution contains a private SQLite service-store catalog,
domain-schema adapter, and transport-neutral lifecycle coordinator. The
catalog's pure locator allocation and independent foundation migration
establish adapter-private ownership; the domain adapter can inspect,
materialise, or apply one validated additive transition to a supplied physical
schema. The coordinator uses durable registry intent and exact re-observation
to reconcile those independently committed stores. The ready SQLite runtime
constructs these seams only behind the two generic public lifecycle operations;
their concrete types, filesystem layout, foundation migration ledger, SQL, and
repair mechanics are not a public adapter or portability contract. Direct
catalog/domain use can still create an orphan database or physical schema and
is unsupported. See the [private relational boundary](private-relational-boundary.md).

## V1-08 lifecycle contract

Materialise requires `service_id`, `expected_service_revision`,
`expected_schema_version`, and `idempotency_key`. Evolve requires those inputs and one
complete, deeply validated adjacent manifest-v2 target, never SQL, a schema delta, or migration
history prose. `schema_version`, server-owned `service_revision`, later per-record
`record_version`, and adapter migration revisions remain independent.

For either lifecycle operation, validation/detachment and the canonical principal-scoped fingerprint
precede lookup of the current expected revisions. An existing exact completed claim replays its
stored result even after the operation incremented `service_revision`; a changed-fingerprint claim
returns `changed_fingerprint`; an exact prepared claim resumes reconciliation; and an exact recovery-required claim
returns the same terminal bounded result. Only a new key may check current revisions and prepare a
durable intent. The registry remains the only current manifest/recovery authority: an immutable
target snapshot is operation evidence, not a service-store ledger or second manifest.

For this contract, malformed input, unsupported transition, stale expected revision,
changed-fingerprint reuse, recovery-required, storage-unavailable, and generic internal failure are
non-retryable. Storage-busy, different-key operation-in-progress, and operation-uncertain are
retryable. An exact same-key prepared claim resumes reconciliation rather than returning
operation-in-progress.

V1-08B was the private durable-intent prerequisite, not a public lifecycle release. Its historical
registry R2 migration upgrades only an exact, clean, public-reachable R1 registry in one
transaction; all other R1-like, dirty, partial, altered, unknown, or future states fail closed. R1
migration history and checksums remain historical evidence, while fresh creation receives the same
R1-then-R2 history before the current R3 WAL policy. The public migration state remains
`uninitialised`, `ready`, `dirty`, or `incompatible`; there is no public upgradeable or repair
state.

The historical R2 layout introduced a `service_revision` epoch, starting at 1, and reserved the
nullable materialisation metadata for only a safe logical backend/opaque namespace pair. Current
public service projections include service revision, aggregate recovery state, and non-null safe
materialisation metadata only after materialise. The single global idempotency ledger retains exact completed
legacy seed/design replays, including stored result bytes, and later holds materialise/evolve
evidence in `prepared`, `completed`, or `recovery_required` phases. A prepared lifecycle intent
blocks another lifecycle intent for that service, recovery-required preserves that blocker, and
completion releases it. The historical R2 layout itself exposed neither a
public operation nor a service-store action.

R2 also caps metadata-only audit rows at the newest 1,000 per principal by audit identifier. The
cap is enforced during migration and on each later audit append; lifecycle/idempotency evidence is
never governed or pruned by that cap or exposed through an audit API. It is a private constant, not a
configuration, environment, CLI, or MCP setting.

V1-08C advances the current registry to R3 and each private service-store
foundation to R2.
Both use an exact `prepared | ready` policy marker to make DELETE-to-WAL
conversion crash-resumable. Ready local storage requires WAL,
`synchronous=FULL`, `wal_autocheckpoint=1000`, secured SQLite-managed
sidecars, one configured busy timeout, and one PASSIVE checkpoint for explicit
migration/startup. `sqlite_busy_timeout_ms` is 1 through 30,000 ms (default
5,000); numeric BUSY-family results map to `StorageBusy` without adapter
retries. These are SQLite physical mechanics, not manifest, MCP, or PostgreSQL
compatibility fields. No public tool or capability is added.

V1-08D added the packaged internal coordinator. New-key
admission proves relational support before inserting prepared intent; the
prepared registry UoW commits and closes before service-store I/O; exact
foundation/target/source observations drive the frozen pure planner; and
terminal registry finalisation uses a fresh UoW. Each physical action runs at
most once per call, and retry uses the same idempotency key. Exact target state
may be adopted within the documented contained-store trust boundary, while
partial, extra, altered, incompatible, or unexpectedly deleted state becomes
recovery-required without repair. Inspection uncertainty and potentially
committed failures return operation-uncertain. Runtime, MCP, CLI,
configuration, public projections, and the five-tool snapshot remained
unchanged until the V1-08E composition.

An exact dirty SQLite foundation receives its one bounded migration action
before terminal classification. This is not automatic repair: the adapter
resumes its own exact crash-recovery `prepared` WAL states but leaves generic
historical dirt unchanged. Fresh dirt after that successful action becomes
recovery-required. The rule prevents a cooperating same-key worker from
terminalizing another worker's visible prepared WAL checkpoint.

V1-08E exposes the bounded projection
`recovery_state: clear | pending | recovery_required` with `service_revision`. It exposes no
operation identifier, fingerprint, idempotency key, target snapshot, storage path, fault text, or
repair procedure. `recovery_required` remains non-auto-repaired through V1-08; verified
export/import/restore or explicit purge belong to V1-10, and the administration CLI belongs to
V1-11.

## Not yet compatible

The project does not provide PostgreSQL, records, branches, search,
export/import, a public repair workflow,
multi-host SQLite guarantees, administration CLI, remote transport, or deployment
interface. V1-08 is sequenced as contract (A), durable intent (B), SQLite WAL/busy policy (C),
internal coordinator (D), public lifecycle (E), record runtime (F), and discovery/topology (G);
PostgreSQL begins only after V1-08G. Their future compatibility commitments will be documented when
they exist.

V1-08C added only the documented SQLite timeout configuration and private WAL
policy. V1-08D packaged the internal coordinator without MCP registration;
V1-08E now composes it but adds no public coordinator object or repair workflow.

The contract identifier is `1.1.0`, the first full-SemVer spelling of the
pre-release capability contract. A long-term version increment policy is not
defined yet. Support windows,
security-fix policy, deprecation rules, and a formal contract-version policy are
explicitly deferred; do not infer them from this pre-release identifier.

Earlier implementations and data stores are not a public runtime compatibility
promise. Do not treat the legacy converter as a storage importer.

See [configuration](configuration.md),
[application boundary](application-boundary.md),
[manifest v2](manifest-v2.md), [security](security.md), and
[design evolution](design-evolution.md).
