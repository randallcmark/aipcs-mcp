# Compatibility and release boundary

`aipcs-mcp` is pre-release (`0.0.0.dev0`). It has a narrow public stdio contract
for stateless capability discovery and SQLite-backed registry lifecycle. It is
not yet a supported release.

| Layer | Identifier | Current status |
| --- | --- | --- |
| Distribution | aipcs-mcp SemVer | Local development package and aipcs command; no released install. |
| MCP capability contract | aipcs_mcp_contract | Version `1.0`; safe capability, error, and lifecycle shapes. |
| Schema manifest | manifest_version | Manifest v2 is the only normal public design input. |
| Configuration document | config_version | Strict V1 configuration document and source precedence. |
| Legacy conversion | Explicit manifest-v1 converter | One-way library conversion with provenance and warnings. |
| Storage migration | Adapter revision | Independent SQLite registry revision 1 and private service-store revision 1; adapter-private layouts, foundation readiness, and relational fidelity checks. |
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
`aipcs_service_design`. Server-info's `registry_lifecycle` feature is true only
when all four lifecycle tools are registered after a ready startup migration.
It deliberately omits storage locations, credentials, network endpoints,
principal identity, and audit data.

AIPCS configuration is resolved by explicit CLI option, documented environment
variable, selected TOML file, and safe default in that order. `config show` is
redacted. `config validate` and `serve` succeed only for a runnable profile.
Stateless is runnable everywhere supported by the package. SQLite is structurally
runnable only on Linux and macOS, subject to live storage checks at `serve`.
Windows SQLite and PostgreSQL are unavailable. Configuration inspection does not
claim a path is ready and does not touch storage.

The SQLite lifecycle is principal-scoped. A configured principal is an opaque,
process-local boundary selected by the operator, not client identity or hosted
tenancy. The MCP transport fixes `created_via` to `mcp`; callers cannot set
either value. Mutation retries use a required idempotency key. Repeating the
same request replays the durable result; reusing the key with different content
returns `conflict`.

Design accepts and stores an initial manifest-v2 document but does not
materialise a service store, create domain tables, create records, or expose
generated tools. `design_state` remains `seeded` and `storage` remains null.

The distribution also contains a private SQLite service-store catalog and
domain-schema adapter. The catalog's pure locator allocation and independent
foundation migration establish adapter-private ownership; the domain adapter
can directly inspect, materialise, or apply one validated additive transition
to a supplied physical schema. Both remain uncomposed implementation seams,
not MCP, CLI, configuration, application, or public compatibility contracts.
The current runtime constructs neither. Their filesystem layout, foundation
migration ledger, and relational behavior are not a portable-storage promise.
Direct use can create an orphan database or physical schema without changing
registry state; public materialisation remains gated on V1-08 recoverable
cross-store coordination. See the [private relational
boundary](private-relational-boundary.md).

## Not yet compatible

The project does not provide PostgreSQL, public service-store allocation or
materialisation, records, branches, search, export/import, recovery,
multi-writer guarantees, administration CLI, remote transport, or deployment
interface. Their future compatibility commitments will be documented when they
exist.

The contract identifier is currently `1.0`. Although earlier planning described
it as SemVer, no version increment policy is defined yet. Support windows,
security-fix policy, deprecation rules, and a formal contract-version policy are
explicitly deferred; do not infer them from this pre-release identifier.

Earlier implementations and data stores are not a public runtime compatibility
promise. Do not treat the legacy converter as a storage importer.

See [configuration](configuration.md),
[application boundary](application-boundary.md),
[manifest v2](manifest-v2.md), [security](security.md), and
[design evolution](design-evolution.md).
