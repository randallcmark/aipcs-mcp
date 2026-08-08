# Agent instruction examples

These files are examples for consumers, not operating instructions for the
source repository.

- [AGENTS.md](AGENTS.md) is a vendor-neutral session and persistence protocol.
- [Seeded guide service](seeded-guide-service.md) is an optional, structured
  help service an operator can create once for fresh agents to discover.

Copy only the sections that fit your agent environment. Replace service IDs,
entity names, and persistence policy with your own reviewed choices. Keep MCP
process configuration, database secrets, and local paths outside shared agent
instructions where your client supports a separate configuration store.
