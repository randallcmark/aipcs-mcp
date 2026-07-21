# Compatibility and release boundary

aipcs-mcp is pre-release (0.0.0.dev0). The current public surface is a stateless
contract-validation and stdio capability server; it is not a supported release.

| Layer | Identifier | Current status |
| --- | --- | --- |
| Distribution | aipcs-mcp SemVer | Local development package and aipcs command; no released install. |
| MCP capability contract | aipcs_mcp_contract SemVer | One server-info tool with versioned safe capability and error shapes. |
| Schema manifest | manifest_version | Manifest v2 is the only normal public design input. |
| Legacy conversion | Explicit manifest-v1 converter | One-way library conversion with provenance and warnings. |
| Storage migration | Adapter revision | Not implemented. |
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

## Not yet compatible

The project does not yet provide a SQLite or PostgreSQL adapter, persistent
service or record operations, schema migration engine, export bundle,
administration CLI, or deployment interface. Their future compatibility
commitments will be documented when they exist.

Earlier implementations and data stores are not a public runtime compatibility
promise. Do not treat the legacy converter as a storage importer.

See [manifest v2](manifest-v2.md), [security](security.md), and
[design evolution](design-evolution.md).
