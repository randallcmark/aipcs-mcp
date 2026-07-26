# Design evolution

AIPCS began as a broad research design for agent-instantiated persistent
context services. Dogfooding narrowed the public product to one primitive
server, stable generic tools, and schemas that agents can design and evolve.

| Earlier direction | Current public direction |
| --- | --- |
| Generate MCP tools from each schema. | One stable 21-tool surface operates over agent-defined schemas. |
| Generate a web service for every memory domain. | One local-first primitive server manages many services. |
| Treat undeveloped seeds as abandoned. | Seeds are durable cues; operators may explicitly archive and purge them through the administration CLI. |
| Make SQLite the product model. | SQLite is the default reference adapter; PostgreSQL is the schema-isolated secondary reference that proves the behavioral ports. |
| Treat local SQLite as general multi-writer storage. | One local POSIX host and effective user; WAL permits readers and serialises one writer. |
| Retain aliases and generated pointers. | Agents evolve schemas and move records explicitly. |
| Persist classification confidence and session counters as registry concepts. | Those concepts are retired; domain schemas may declare their own useful fields. |
| Keep tool definitions inside schemas. | Tool definitions belong to the fixed server contract, not manifest data. |
| Let discovery infer relevance or authority. | Bootstrap is shape-only; summary and maintenance expose bounded declared/mechanical facts without ranking truth. |
| Defer concurrency evidence. | Lifecycle uses registry durable intent; record/topology mutations use a separate completed-only local replay ledger. |
| Treat local transport proof as remote readiness. | Streamable HTTP now has explicit loopback, Host/Origin, and session boundaries; authentication and tenancy are still separate concerns. |

## What 1.2 establishes

Manifest v2 describes relational records, relationships, indexes, retrieval
intent, discovery facets, and server-managed record fields. Materialise and
evolve make that schema operational. Generic record CRUD/history, structured
exact and membership search, branch topology, bootstrap, summary, and
read-only maintenance complete the usable local memory loop.

Branches are topology above records, not aliases. A record may have one
primary branch and multiple related branches. Effective topology changes are
record mutations and therefore advance record revision and history.

Discovery remains intentionally lightweight. Bootstrap helps an agent choose a
service without opening service storage. Summary supplies the service's actual
retrieval affordances and bounded observations. Maintenance reports candidates
for agent judgment and never mutates, merges, archives, deletes, or decides
which memory is true.

## Concepts considered but not retained

Generated per-schema tools, aliases, registry classification confidence,
`session_count`, schema-owned tool declarations, and dedicated merge/split
operations were explored but are not part of the current contract. Parent
service links were also removed from the registry model; deeper hierarchy is
represented where needed through agent-defined record relationships and branch
parent topology.

The legacy manifest converter records discarded fields and warnings so this
evolution remains visible without carrying obsolete behavior into the live
contract.

## Still deferred

The current implementation remains local-first and pre-release. PostgreSQL is
a supported generic public-v1 reference backend when installed with its
`[postgresql]` extra. A bounded administration CLI exposes backend-neutral
operational lifecycle and portable data seams. Streamable HTTP enables trusted
service deployment, but hosted identity, application-managed authentication,
semantic search, package-index publication, and maintenance/deprecation policy
remain separate future work. Their absence is deliberate and is not filled by
examples or research artifacts.
