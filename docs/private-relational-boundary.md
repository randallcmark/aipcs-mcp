# Relational implementation boundary

`aipcs-mcp` packages SQLite and PostgreSQL adapters behind internal catalog,
domain-schema, data, discovery, topology, registry, and portable-store ports.
These concrete classes are not public APIs, plugin interfaces, MCP
capabilities, configuration extensions, or repair tools.

This guide records the implementation boundary for contributors. The
behavioral contract is in [storage contracts](storage-contracts.md).

## Registry and service-store authority

The registry is authoritative for service identity, lifecycle state, current
manifest, service/schema revision, cross-store intent, transfer receipts, and
purge tombstones.

Each service store owns the materialised domain layout, records, record
history, branch topology, membership, write fence, and completed local
mutation replay. It does not keep a second current manifest or schema-version
authority.

A direct adapter operation can create physical state the public lifecycle does
not know about. Concrete adapters must therefore be reached only through the
composition and application boundaries in normal runtime use.

## Exact relational inspection

The domain-schema store operates against a compiled, backend-neutral
relational specification. An exact ready foundation with no domain objects is
`unmaterialised`; an exact complete domain layout is `ready`; and partial,
extra, altered, orphaned, unknown, or future layouts fail closed.

Inspection never repairs. Initial materialisation accepts schema version 1.
Additive evolution may create new entities, append nullable fields, add
indexes, and apply approved metadata-only transitions. It never rebuilds,
renames, drops, copies, backfills, or guesses.

Physical migration ledgers and reserved objects are adapter-private. They are
not portable data or extension points.

## Recovery and replay

Cross-store lifecycle has no distributed transaction. The registry first
admits immutable intent, then the coordinator performs at most one bounded
physical action per reconciliation step, re-observes exact state, and
finalises through a fresh registry transaction.

Prepared intent may resume an exact known predecessor or exact target.
Partial, altered, incompatible, or unexpectedly deleted state is terminal
recovery evidence. Coarse SQLite `dirty` evidence may also describe valid peer
progress, so it returns retryable uncertainty until exact state is observed;
exception text is never interpreted as success.

Record and topology mutations use a different completed-only service-local
ledger. Data, history, and replay evidence commit in one local transaction.
That ledger has no prepared phase, schema authority, lifecycle blocker, or
cross-store recovery role.

## Public composition

The runtime selects exactly one `sqlite | postgresql` implementation from
validated configuration. A ready persistent process exposes the same 21-tool
MCP contract. Read-side calls never execute DDL. Only admitted lifecycle,
record, topology, or portable operations may invoke supported storage
transitions.

The administration CLI uses application ports. It does not import concrete
adapters, issue SQL, expose physical identifiers, or pass caller paths into
portable storage. File handling opens or publishes private streams at the CLI
boundary.

Treat all concrete adapter modules and physical schemas as internal
implementation detail. Third-party adapters, arbitrary repair, physical
backup/restore, remote administration, and mixed-backend runtime composition
require their own future contracts.
