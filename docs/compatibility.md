# Compatibility and release boundary

`aipcs-mcp` remains pre-release (`0.0.0.dev0`). The source tree implements the
public-v1 contract below. A supported distribution channel, release window,
deprecation policy, and security-fix commitment remain to be defined.

| Layer | Identifier | Current source contract |
| --- | --- | --- |
| Distribution | `aipcs-mcp` SemVer | Development package and `aipcs` command; no supported distribution channel yet. |
| MCP capability | `aipcs_mcp_contract` | `1.2.0`; 21-tool SQLite or PostgreSQL surface when the selected persistent profile is ready. |
| Schema | `manifest_version` | Manifest v2 is normal design input. |
| Configuration | `config_version` | Strict configuration v1 with documented precedence. |
| Registry storage | adapter revision | SQLite registry R4; PostgreSQL fixed-schema R2. |
| Service storage | adapter revision | SQLite service store R3; PostgreSQL schema-isolated R1 foundation, including record/topology storage. |
| Export bundle | `export_format_version` | Internal logical format v1; no public MCP/CLI/path surface in V1-10. |

These identifiers are independent. `schema_version`, server-owned
`service_revision`, per-record `record_version`, per-branch
`branch_revision`, adapter revisions, and the MCP contract version must not be
substituted for one another.

## Runtime profiles

The stateless stdio profile exposes only `aipcs_server_info`. A fully ready
SQLite profile exposes the 21 tools listed in the
[README](../README.md#mcp-contract). Server info reports the following
features independently:

- registry lifecycle;
- materialisation lifecycle;
- record runtime; and
- discovery/topology.

The live `tools/list` result is authoritative. A partial or unavailable
binding must not advertise tools it cannot execute.

SQLite compatibility is one host, Linux or macOS, a local POSIX filesystem,
SQLite 3.51.3 or newer, and cooperating processes under the same effective
user. Persistent WAL supports concurrent readers and one serialised writer.
Windows SQLite, network filesystems, multi-host writers, and hostile same-user
isolation are outside the boundary.

PostgreSQL is a supported generic public-v1 stdio reference adapter for major
versions 16 through 18 when installed with the `[postgresql]` extra. It uses
`psycopg` 3 and one operator-provisioned database with a fixed registry schema
and schema-isolated service storage. Release verification passes the full
contract-parity suites on pinned PostgreSQL 16 and 18 endpoints.

## Schema and lifecycle compatibility

Normal design accepts manifest v2 only. The explicit one-way legacy converter
is the sole manifest-v1 entry point. It returns provenance, warnings, and
discarded-field information but neither imports data nor promises a reversible
round trip.

Design stores a complete initial manifest without creating physical domain
tables. Materialise accepts exact current service/schema revisions and creates
the initial layout. Evolve accepts a complete adjacent additive target,
not SQL, a partial delta, or free-form history. The registry manifest remains
the current schema authority.

Materialise/evolve use registry-held durable intent. An exact completed request
replays after service revision advancement; an exact prepared request resumes
finite reconciliation; changed content under the same key fails; and
recovery-required remains terminal without automatic repair.

## Data compatibility

The 1.2 record surface is generic across manifest-defined entities. Public
record documents omit `owner_id`; callers cannot set any server-managed field.
Create/update/delete use local service-store idempotency evidence, while
update/delete require exact record revisions. This completed-only local replay
ledger is distinct from the registry lifecycle coordinator: it has no prepared
phase, recovery authority, current manifest, or cross-store transaction.

Structured search supports exact scalar filters and declared `string_list`
membership. Annotation fields, undeclared fields, and server-managed fields are
not filterable. Cursors are opaque and bound to their originating query.
Semantic/fuzzy search and cursor interpretation are not compatible behaviors.

Branches support primary and related record membership, active/archived/
superseded status, parent topology, and exact branch revisions. Effective
assignment changes advance record versions and appear in record history.

Bootstrap is shape-only and does not open service storage. Summary is bounded
and may return record counts, declared domain facets, branch cards,
unbranched counts, authority-field availability, query guidance, and optional
samples. Maintenance returns mechanical candidates only and is not a truth,
ranking, deletion, archival, or merge contract.

## SQLite migration compatibility

Fresh service stores receive the immutable R1, R2, then R3 migration history.
R2 established the WAL-ready foundation. R3 adds exact reserved tables for
completed local mutation replay, record history, branches, and record-branch
membership. Those reserved objects are adapter-private and are not a portable
SQL or extension contract.

An existing exact clean R2 service store is an `outdated` predecessor. Reads
do not migrate it: record/history/branch/maintenance calls return
`storage_migration_required`, while service summary reports
`data_status: "migration_required"` with manifest-derived affordances and no
invented zero counts. Bootstrap remains available because it never opens the
store.

Only an admitted lifecycle, record, or topology mutation may run the exact
R2-to-R3 migration before its local transaction. A prepared materialise may
adopt an exact clean R2 window committed by a cooperating peer, then
re-observes before domain materialisation. The migration accepts only the
exact known predecessor and exact prepared/ready states. It never guesses,
repairs, or accepts altered, partial, generic dirty, unknown, or newer
layouts. Read-side operations perform no DDL.

Registry R4 and service-store R2/R3 WAL transitions retain checksummed history,
exact physical descriptors, bounded busy handling, and fail-closed state
classification. These are SQLite mechanics, not MCP or PostgreSQL portability
fields.

SQLite registry R4 and PostgreSQL registry R2 add equivalent portable
lifecycle claims, identity reservations/tombstones, and transfer receipts.
They do not change the public 21-tool MCP surface.

## Internal portable lifecycle compatibility

`export_format_version: 1` is an internal, backend-neutral logical recovery
and transfer contract. It is strict canonical UTF-8 JSON Lines under
`aipcs-json-c14n-1` with closed header/member/trailer shapes, ordered
frame/section/root SHA-256 digests, and named bounded limits. Readers reject
unknown/newer formats, fields, sections, noncanonical encodings, and physical
database artifacts; there is no best-effort downgrade, skip, partial import,
or forward-field tolerance.

Format v1 preserves service, record, history-event, branch, and membership
identity while rebinding ownership to the authorised destination principal. It
contains no source principal, path, namespace, credential, endpoint, SQL,
migration ledger, replay claim, receipt, tombstone, or physical backend state.
The non-authoritative `source_backend` label does not make either adapter's DDL
or files compatible data.

Operational lifecycle supports only `active -> suspended`,
`suspended -> active`, `active|suspended -> archived`, and
`archived -> suspended`. Materialised export requires a separately completed
suspend/archive and exact closed write fence. Import validates completely
before writes, stages unpublished state, and publishes after exact
re-observation. Purge is archived-only, separately authorised, terminal, and
leaves a non-reusable identity tombstone.

Transfer receipts describe the exact completed claim and service state at
issuance. They remain valid historical control evidence after later service
revisions; they are not current-service projections or portable content.
Installed wheel/sdist verification covers SQLite and both cross-backend
directions on pinned PostgreSQL 16 and 18, including tamper-before-write,
redaction, restart replay, purge, physical absence, and exact container
cleanup.

V1-10 intentionally exposes none of this through MCP or mixed-backend runtime
composition. V1-11 C2 recognizes the complete operator command grammar and
implements the read-only status, doctor, storage, service, and maintenance
commands for the configured homogeneous backend. These inspection paths do
not migrate or repair storage. C3 implements the exact operational transition
matrix through the existing coordinator with stable retry and recovery exits.
C4 adds logical file export/import and dry-run validation. Cross-backend
transfer remains two separately configured commands rather than a mixed
runtime. Purge and the supported distribution workflow remain later slices.

## Error and retry compatibility

Public failures are bounded and redacted. In particular:

- validation, unsupported transition, stale revision, changed fingerprint,
  recovery required, storage unavailable, and internal failure are
  non-retryable;
- storage busy, another lifecycle operation in progress, and operation
  uncertain are retryable with the exact original request and key; and
- local record/topology mutations additionally distinguish
  `stale_record_version`, `stale_branch_revision`, constraint failure,
  changed fingerprint, and `storage_migration_required`.

Backend exception text, SQL, paths, principal values, and private evidence are
never compatibility data.

## Deferred compatibility

No compatibility commitment yet exists for:

- third-party adapters or mixed-backend runtime composition;
- semantic, fuzzy, embedding, or cross-service search;
- public export/import/restore, online backup, repair, archive/resume, or
  purge workflows;
- administration CLI workflows or supported `uvx` packaging;
- remote MCP, authentication, hosted tenancy, or multi-host deployment; or
- support windows, security-fix windows, or deprecation periods.

Those capabilities will define their own compatibility boundaries when
implemented and validated. Earlier prototypes and research data stores are not
a public runtime compatibility promise.
