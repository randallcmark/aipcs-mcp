# Architecture

AIPCS is a contract-first primitive server with a stable generic MCP surface.
Agent-defined schemas are data; they never become generated Python, SQL,
network endpoints, or per-schema tools.

```text
MCP stdio / administration CLI
              |
       validation and projection
              |
    application use cases and ports
       /                    \
SQLite adapters      PostgreSQL adapters
```

## Public boundaries

- **MCP transport:** local stdio only; one fixed principal per process.
- **CLI:** operator inspection, lifecycle, and logical transfer over the same
  application services.
- **Manifest:** backend-neutral relational schema and retrieval intent.
- **Application:** lifecycle, record, discovery, topology, maintenance, and
  portable coordination without SQL or filesystem paths.
- **Adapters:** backend-specific registry, domain schema, records, topology,
  discovery, and portable storage behind behavioral ports.

The registry owns service identity, current manifest, service/schema revision,
lifecycle state, and cross-store intent. Each service store owns domain data,
record history, branches, membership, and completed local mutation replay.
Neither adapter's physical schema is a public extension API.

## Consistency model

There is no distributed transaction between registry and service storage.
Lifecycle operations use durable registry intent and finite reconciliation.
Local record/topology mutations commit data, history, and replay evidence in
one service-store transaction.

Exact revisions provide optimistic concurrency. Stable idempotency keys and
operation UUIDs make retries deterministic. Uncertain outcomes never become
success based on exception text.

## Storage portability

SQLite and PostgreSQL implement the same public behavior behind explicit
ports. The goal is a clear adapter seam, not an unbounded plugin system.
Third-party adapters are not a current compatibility promise. Logical export
and import are the supported cross-backend boundary.

## Related detail

- [Application boundary](application-boundary.md)
- [Private relational boundary](private-relational-boundary.md)
- [Storage contracts](storage-contracts.md)
- [Security](security.md)
- [Compatibility](compatibility.md)
