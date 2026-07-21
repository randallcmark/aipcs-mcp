# AIPCS MCP

`aipcs-mcp` is the future open-source implementation of AIPCS: a self-hosted
persistence layer for AI agents using stable, generic MCP primitives.

This repository is currently a public-v1 scaffold. Version `0.0.0.dev0` does
not yet provide an MCP server, CLI, storage adapter, or installable workflow.
It establishes a clean public history and hygiene boundary before implementation
is introduced.

## Public-v1 direction

The intended v1 product is local-first and `stdio`-only. It will provide
server-owned generic primitives, SQLite as the zero-configuration reference
adapter, a PostgreSQL reference adapter, validated relational schemas, portable
lifecycle operations, and an `aipcs` command when the runtime is complete.

## Non-goals for public v1

- dynamically generated domain-specific MCP tools or per-domain web services;
- fuzzy, semantic, full-text, or cross-service retrieval;
- public remote MCP, hosted tenancy, OAuth/DCR, or zero-knowledge hosting; and
- automatic deletion, archival, merging, or rewriting of memory.

See [compatibility](docs/compatibility.md) and
[design evolution](docs/design-evolution.md).

## Development status

There is no supported installation command yet. Treat this tree as scaffolding
until a released runtime and contributor guidance exist.
