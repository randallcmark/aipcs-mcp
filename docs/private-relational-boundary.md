# Private relational boundary

`aipcs-mcp` packages two private SQLite seams so the distribution can prove
relational adapter fidelity: the service-store catalog establishes an opaque,
contained database foundation, and the domain-schema store compares,
materialises, and evolves one supplied relational specification. Neither seam
is a public API, MCP capability, CLI command, configuration option, or
operator workflow.

This guide explains that boundary for maintainers and release reviewers. It
does not provide an invocation recipe. The normative contract is in [storage
contracts](storage-contracts.md).

## What the private seam proves

The catalog creates only adapter metadata and its independent checksummed
foundation migration ledger. The domain-schema store works only after that
foundation is exactly ready, and only against a caller-supplied compiled
specification or deeply revalidated additive transition. It does not allocate
a service, create a directory or database, read or update registry state, or
provide records.

Relative to the supplied specification, an exact ready foundation with no
domain objects is `unmaterialised`; a complete exact domain layout is `ready`;
and partial, extra, altered, or orphaned domain objects are `incompatible`.
Inspection does not repair. Exact table inspection is fail-closed: only
inter-token ASCII whitespace may differ, while the remaining tokens and the
other object, pragma, and integrity signatures remain exact.

Initial materialisation accepts only schema version 1. A direct additive
evolution starts from an exact current layout or accepts an exact target as a
no-op. It can create new entity tables, append nullable fields to existing
tables, and add explicit indexes; it never rebuilds, renames, drops, copies,
backfills, or repairs data. The target is the physical authority for retry
safety, including metadata-only transitions. Installed release proof runs this
private behavior across a separate process restart.

## Authority and limits

The registry-held manifest is the architectural authority, but the private
seam deliberately does not cross-check or coordinate with that registry. A
direct private operation can therefore leave an orphan foundation or a
physical target that the public lifecycle does not know about. It is evidence
of adapter behavior, not a service lifecycle action.

Beyond the V1-06D foundation metadata and migration ledger, the service
database stores no domain manifest, domain schema version, transition,
provenance, domain-schema ledger, operation record, or repair state. Complete
external deletion of all domain objects is consequently indistinguishable from
never materialising them; partial deletion remains incompatible. Do not try to
repair or infer history from this seam.

## Public boundary

The public runtime starts only the registry adapter. A ready SQLite server
exposes server-info plus seed, list, inspect, and initial design; design remains
seeded and does not create a service database, domain table, or record. Public
materialise/evolve operations, records, service-store composition, and domain
state reporting remain unavailable.

V1-08 owns the missing lifecycle work: cross-store materialisation/evolution
idempotency and revision coordination, registry/store reconciliation, recovery,
and eventual public composition.
Until then, use the supported public lifecycle only and treat this private seam
as an internal test and release boundary.
