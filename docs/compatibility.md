# Compatibility and release boundary

`aipcs-mcp` is pre-release (`0.0.0.dev0`). The current public surface is a stateless
contract-validation and stdio capability server; it is not a supported release.

| Layer | Identifier | Current status |
| --- | --- | --- |
| Distribution | aipcs-mcp SemVer | Local development package and aipcs command; no released install. |
| MCP capability contract | aipcs_mcp_contract SemVer | One server-info tool with versioned safe capability and error shapes. |
| Schema manifest | manifest_version | Manifest v2 is the only normal public design input. |
| Configuration document | config_version | Strict V1 configuration document and source precedence. |
| Legacy conversion | Explicit manifest-v1 converter | One-way library conversion with provenance and warnings. |
| Storage migration | Adapter revision | Contract only; no adapter or migration implementation. |
| Export bundle | export_format_version | Not implemented. |

## Current contract

Normal design input accepts manifest v2 only. The explicit legacy converter is
the sole v1 entry point: it produces a v2 manifest and reports discarded fields
and warnings. It does not write data, create storage, or promise a reversible
round trip.

The `aipcs serve` command starts over stdio only. Its sole MCP tool exposes a
structured capability envelope containing contract versions, supported manifest
versions, and enabled features; it deliberately omits storage locations,
credentials, network endpoints, and owner information.

AIPCS configuration is resolved by explicit CLI option, documented environment
variable, selected TOML file, and safe default in that order. `config show` is
redacted. `config validate` and `serve` succeed only for a runnable profile.
Stateless is the only runnable V1-05 profile. SQLite and PostgreSQL descriptors
are recognised but unavailable; they neither construct storage nor alter MCP
capabilities.

The internal application boundary separates MCP and CLI adapters from use cases.
V1-06A adds pure storage value objects and future-adapter protocols, but no
storage backend, lifecycle operation, command, tool, or adapter extension
mechanism.

## Not yet compatible

The project does not yet provide a SQLite or PostgreSQL adapter, persistent
service or record operations, schema migration engine, export bundle,
administration CLI, or deployment interface. Their future compatibility
commitments will be documented when they exist.

Earlier implementations and data stores are not a public runtime compatibility
promise. Do not treat the legacy converter as a storage importer.

See [configuration](configuration.md),
[application boundary](application-boundary.md),
[manifest v2](manifest-v2.md), [security](security.md), and
[design evolution](design-evolution.md).
