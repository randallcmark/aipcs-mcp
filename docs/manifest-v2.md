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
fields are rejected. Entity and index names must not begin with the
case-insensitive `sqlite_` prefix, and an entity name may not collide with an
index name: relational table and index names share that namespace. Index names
remain explicit and globally unique.

Every entity must declare the same server-managed fields exactly: id, owner_id,
created_at, updated_at, created_via, and record_version. Only id may be a
primary key. The server owns their values; a future record API will not accept
caller-provided values for them.

Supported attribute types are string, integer, number, boolean, datetime, uuid,
and string_list. Enumerated values and retrieval modes are type-checked.
Enumerated numeric values must be finite; string and string-list element values
may not contain NUL characters. `allowed_values` is application metadata, not a
physical database constraint. A future record boundary will require every
`string_list` element to belong to its declared allowed set.

## Relationships, indexes, and retrieval intent

A relationship names an agent-declared, non-primary-key UUID source field and
targets another entity's `id`; the server-managed `id` cannot be a source.
Each source endpoint may name only one relationship, so one UUID value never
has ambiguous targets. The only currently accepted delete rule is `restrict`.
Delete and update restriction is immediate in both reference adapters; public v1 does not claim a
deferred `RESTRICT` combination that the databases implement differently.
Required relationship edges must be acyclic (including a required self-loop),
because a future single-record API could not create the first record in such a
cycle. Nullable edges may break a self or multi-entity cycle, and relationships
from distinct source fields remain valid. These rules deliberately reject
declarations that future adapters and record operations could not enforce or
populate consistently.

Indexes reference known scalar attributes. string_list fields cannot be
indexed. Query patterns are descriptive retrieval intent; discovery facets use
an explicit entity-and-field object rather than a shorthand string.

## Initial design and evolution

The current normal design request accepts an initial manifest only:
schema_version must be 1 and migration_history must be empty. Schema versions
are capped at 65. For an evolved manifest, history is a complete, continuous
one-step sequence from version 1 through the declared schema version; every
entry contains one to 32 non-empty human-readable operation annotations, each
at most 240 characters. Those annotations describe a change but never authorise
or derive a physical delta.

In the ready SQLite lifecycle profile, a validated initial manifest is stored
against a service seed. That is registry persistence only: it does not
materialise a service store, create tables, or make records available. A future
lifecycle slice will define how later schema versions are applied publicly. A
private SQLite adapter may directly apply a separately supplied validated
additive transition to an already materialised store, but it neither updates
registry-held manifest state nor exposes public service evolution.

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
guessing. Because private-v1 migration history is discarded rather than
reinterpreted, the converted document starts at public-v2 `schema_version` 1
with empty history and reports a warning when the legacy version was later.

The converter does not create a service, write a database, or provide a
reversible migration. It is a bridge into the public schema contract, not a
storage import tool.

See [compatibility](compatibility.md) and [security](security.md).
