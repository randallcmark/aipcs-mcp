# Design evolution

AIPCS started as a broad design for agent-instantiated persistent context
services. Dogfooding narrowed the public product toward one primitive server,
stable generic MCP tools, and an agent-designed relational memory model.

The current code establishes the manifest and trust boundaries plus a
local-stdio SQLite registry lifecycle. It persists service seeds and initial
manifest designs, but service-store materialisation and records remain future
slices.

| Earlier direction | Public-v1 direction |
| --- | --- |
| Generate MCP tools from every schema. | The server will own stable generic tools; schemas describe data and retrieval only. |
| Generate a web service for every memory domain. | One local-first primitive server will manage many services. |
| Treat undeveloped seeds as abandoned. | Seeds are durable cues; archive and purge remain explicit future actions. |
| Use SQLite as the only storage shape. | SQLite is the current registry reference; PostgreSQL remains a future secondary adapter. |
| Store inert relationship and index declarations. | Manifest v2 accepts only declarations it can validate and later enforce. |
| Retain aliases, confidence scores, and tool declarations. | These are retired from the public input contract; agents evolve the schema directly. |
| Defer recovery and administration. | Portability, recovery, and an operator CLI remain planned release requirements. |
| Treat local transport proof as remote readiness. | Public v1 supports local stdio only. |

The current registry lifecycle exposes fixed seed, list, inspect, and design
tools only after SQLite startup is ready. Design stores a validated manifest;
it does not materialise a store, create domain tables, create records, or
generate tools. PostgreSQL, remote operation, multi-writer support, and
administration remain out of scope.

Manifest v2 makes relationships, indexes, retrieval intent, and
server-managed record fields explicit. Its legacy converter is intentionally
one-way: it returns provenance and warnings rather than silently preserving
ambiguous or retired constructs.

This public summary records the product boundary without carrying operational
history or private artifacts into the repository.
