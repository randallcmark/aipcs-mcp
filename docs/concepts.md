# Concepts and lifecycle

## Primitive server

AIPCS exposes one stable MCP tool surface over many agent-defined memory
services. A manifest describes each service's relational record model; it does
not generate tools, endpoints, or executable code.

## Service lifecycle

The schema lifecycle is:

1. **Seed** records a durable cue and intent.
2. **Design** validates and stores an initial manifest.
3. **Materialise** creates the exact relational layout.
4. **Use** creates, retrieves, updates, and organises records.
5. **Evolve** applies one complete adjacent additive manifest.

Service revisions and schema versions are different. Every lifecycle mutation
uses exact current revisions and an idempotency key.

The operator lifecycle is:

```text
active -> suspended -> active
active -> archived
suspended -> archived
archived -> suspended
archived -> purged
```

Export of a materialised service requires it to be suspended or archived.
Purge is explicit, separately authorised, irreversible, and leaves a
non-reusable tombstone.

## Records and retrieval

Callers provide domain fields. AIPCS owns record identity, principal,
timestamps, provenance, and record revision. Updates and deletes require the
current `record_version`.

Retrieval is declared and structured:

- scalar fields use exact equality;
- declared `string_list` membership fields accept one member;
- annotation fields are returned but not filterable.

The agent chooses the fields and exact retrieval cues it needs; AIPCS keeps
that retrieval contract explicit rather than selecting relevance on its behalf.

Use `aipcs_bootstrap` to select a service without opening service storage.
Then use `aipcs_service_summary` to learn the service's actual filters,
facets, query guidance, branch cards, and bounded samples before querying.

## Branches

Branches organise records above the schema. A record can have one primary
branch and multiple related branches. Branches may have parents and an
`active`, `archived`, or `superseded` status. They are not aliases and do not
redirect lookups.

Effective branch membership changes are record mutations. They require current
record revisions, advance those revisions, and appear in record history.

## Maintenance

Maintenance is read-only candidate discovery. It can report mechanical signals
such as expiry, age, low declared numeric confidence, declared supersession,
missing authority fields, unbranched records, duplicate authority references,
or large annotations when the schema supports those signals.

AIPCS never decides which memory is true, merges records, or deletes content
because a candidate was reported. An agent or operator reviews and acts through
ordinary public tools.

## Version dimensions

Distribution version, MCP contract version, manifest version, configuration
version, service revision, schema version, record version, branch revision,
storage-adapter revision, and export-format version are independent. See
[compatibility](compatibility.md) before building upgrade logic.
