# Compatibility and release boundary

This scaffold establishes names and compatibility intent; it does not implement
an MCP server or promise a supported installation.

| Layer | Identifier | Purpose |
| --- | --- | --- |
| Distribution | `aipcs-mcp` SemVer | Installed code and future `aipcs` command. |
| MCP capability contract | `aipcs_mcp_contract` SemVer | Tools, errors, and lifecycle semantics. |
| Schema manifest | `manifest_version` | Schema-document interpretation. |
| Storage migration | Adapter revision | Physical backend layout and repair state. |
| Export bundle | `export_format_version` | Portable data layout and import rules. |

The intended public-v1 envelope is Python 3.12+, local `stdio` transport,
server-owned generic tools, SQLite plus PostgreSQL reference adapters, explicit
relationship/index enforcement, additive schema evolution, and separate design
and operational lifecycle states.

Fuzzy or cross-service retrieval, public remote MCP, hosted tenancy, and
automatic memory deletion are outside public v1. Earlier private stores are not
a public runtime compatibility promise; a future one-way importer will document
its supported range with the implementation.
