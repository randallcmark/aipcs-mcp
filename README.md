# AIPCS MCP

`aipcs-mcp` is a pre-release, self-hosted persistence foundation for AI agents.
It is being built around stable, generic MCP primitives and an agent-designed
relational memory schema.

The current V1-05 foundation preserves a minimal, stateless MCP server boundary:

- manifest v2 validation for portable relational schemas;
- an explicit, one-way legacy manifest-v1 library converter;
- stable structured errors and safe capability information;
- strict, inspectable configuration for a stateless stdio runtime;
- an `aipcs serve` command restricted to stdio; and
- one read-only `aipcs_server_info` MCP tool, verified through a real client.

It does **not** yet provide persistence, a storage adapter, service or record
lifecycle operations, export/import bundles, an administration CLI, or a
released installation workflow.

## Public v1 direction

Public v1 is local-first and stdio-only. The server will own a small set of
generic primitives; schemas describe records, relationships, indexes, and
retrieval intent rather than creating bespoke MCP tools or services.

SQLite will be the zero-configuration reference adapter. PostgreSQL will be a
second reference adapter demonstrating the same storage boundary. Neither is
implemented in this slice.

## Contract documentation

- [Configuration](docs/configuration.md) documents precedence, profiles, and
  redacted inspection.
- [Manifest v2](docs/manifest-v2.md) describes the current schema boundary.
- [Application boundary](docs/application-boundary.md) describes the internal
  separation between transport, application use cases, and future adapters.
- [Security and trust boundary](docs/security.md) describes safe inputs,
  errors, capability information, and transport restrictions.
- [Compatibility](docs/compatibility.md) records what is and is not a public
  compatibility promise.
- [Design evolution](docs/design-evolution.md) explains decisions retained from
  earlier design work and concepts intentionally retired.

## Development status

There is no supported release installation or production deployment at this
stage. From a checkout, the stateless server can be run as a **development smoke
only**:

    uvx --from . aipcs serve

Configuration can be inspected or validated without starting a server:

    uvx --from . aipcs config show
    uvx --from . aipcs config validate

These are not released installation instructions. The stateless profile is the
only runnable V1-05 profile. SQLite and PostgreSQL profiles may be inspected but
are explicitly unavailable until their adapters exist; they do not create or
connect to storage.

AIPCS remains stdio-only. Listener transports and listener-oriented environment
settings are rejected before configuration resolution or server construction.
Configuration adds no MCP tool, data operation, lifecycle behavior, or backend.

Tests and example data in this repository are synthetic contract fixtures.
They must not contain operational records, credentials, or personal context.

## Out of scope for public v1

- dynamically generated domain-specific MCP tools or per-domain web services;
- fuzzy, semantic, full-text, or cross-service retrieval;
- remote MCP, hosted tenancy, OAuth/DCR, or zero-knowledge hosting; and
- automatic deletion, archival, merging, or rewriting of memory.
