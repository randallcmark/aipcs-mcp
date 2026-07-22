# Manifest v2

Manifest v2 is the only schema document accepted by normal public design
input. It describes an agent-defined relational memory model while keeping the
server contract generic and backend-neutral.

## Document shape

A manifest has:

- manifest_version 2 and a positive schema_version;
- one or more named entities, each with typed attributes;
- optional relationships and indexes;
- optional query patterns, discovery facets, retrieval guidance, and migration
  history.

Names are lowercase identifiers using letters, digits, and underscores. Unknown
fields are rejected.

Every entity must declare the same server-managed fields exactly: id, owner_id,
created_at, updated_at, created_via, and record_version. Only id may be a
primary key. The server owns their values; a future record API will not accept
caller-provided values for them.

Supported attribute types are string, integer, number, boolean, datetime, uuid,
and string_list. Enumerated values and retrieval modes are type-checked.

## Relationships, indexes, and retrieval intent

A relationship names a UUID source field and targets another entity's id. The
only currently accepted delete rule is restrict. This deliberately rules out
declarations that cannot be enforced consistently by every future adapter.

Indexes reference known scalar attributes. string_list fields cannot be
indexed. Query patterns are descriptive retrieval intent; discovery facets use
an explicit entity-and-field object rather than a shorthand string.

## Initial design and evolution

The current normal design request accepts an initial manifest only:
schema_version must be 1 and migration_history must be empty. In the ready
SQLite lifecycle profile, a validated initial manifest is stored against a
service seed. That is registry persistence only: it does not materialise a
service store, create tables, or make records available. A future migration and
lifecycle slice will define how later schema versions are applied.

## Retired input

The public contract rejects generated tool definitions, aliases, classification
confidence, parent-service links, session counters, endpoints, connection
strings, local storage paths, and materialisation state. These concepts either
belong to server implementation or were retired in favour of direct schema
evolution.

## Legacy manifest-v1 conversion

Manifest v1 is never normal design input. A separate one-way library converter
accepts legacy documents and returns a manifest-v2 document,
provenance, warnings, and a list of discarded fields. It may normalise legacy
types and server-managed fields; it rejects ambiguous relationships instead of
guessing.

The converter does not create a service, write a database, or provide a
reversible migration. It is a bridge into the public schema contract, not a
storage import tool.

See [compatibility](compatibility.md) and [security](security.md).
