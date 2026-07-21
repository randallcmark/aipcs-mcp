# Design evolution

AIPCS began with a broad reference design for agent-instantiated persistent
context services. Dogfooding narrowed public v1 to one server with stable,
generic MCP tools and an agent-designed relational memory model.

| Earlier direction | Public-v1 direction |
| --- | --- |
| Generate MCP tools from every schema. | The server owns stable generic tools; schemas describe data and retrieval only. |
| Generate a web service for every memory domain. | One local-first primitive server manages many services. |
| Treat undeveloped seeds as abandoned. | Seeds are durable cues; archive and purge remain explicit actions. |
| Use SQLite as the only storage shape. | SQLite is the reference adapter; PostgreSQL is the first secondary adapter. |
| Store inert relationship and index declarations. | Public v1 accepts only declarations it validates and enforces. |
| Defer recovery and administration. | Portability, lifecycle controls, and an operator CLI are release requirements. |
| Treat local transport proof as remote readiness. | Public v1 supports local `stdio` only. |

The research record retains the full design history. This summary explains the
public product boundary without importing private experiments or operations.
