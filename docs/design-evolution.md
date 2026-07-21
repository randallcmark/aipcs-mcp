# Design evolution

AIPCS started as a broad design for agent-instantiated persistent context
services. Dogfooding narrowed the public product toward one primitive server,
stable generic MCP tools, and an agent-designed relational memory model.

The current code establishes the manifest and trust boundaries plus a stateless
stdio capability server. Storage and lifecycle behavior remain future slices.

| Earlier direction | Public-v1 direction |
| --- | --- |
| Generate MCP tools from every schema. | The server will own stable generic tools; schemas describe data and retrieval only. |
| Generate a web service for every memory domain. | One local-first primitive server will manage many services. |
| Treat undeveloped seeds as abandoned. | Seeds are durable cues; archive and purge remain explicit future actions. |
| Use SQLite as the only storage shape. | SQLite is the reference adapter; PostgreSQL is the first secondary adapter. |
| Store inert relationship and index declarations. | Manifest v2 accepts only declarations it can validate and later enforce. |
| Retain aliases, confidence scores, and tool declarations. | These are retired from the public input contract; agents evolve the schema directly. |
| Defer recovery and administration. | Portability, lifecycle controls, and an operator CLI are planned release requirements. |
| Treat local transport proof as remote readiness. | Public v1 supports local stdio only. |

Manifest v2 makes relationships, indexes, retrieval intent, and
server-managed record fields explicit. Its legacy converter is intentionally
one-way: it returns provenance and warnings rather than silently preserving
ambiguous or retired constructs.

This public summary records the product boundary without carrying operational
history or private artifacts into the repository.
