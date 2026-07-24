# Capability and limits matrix

| Area | Current source contract | Deferred or excluded |
| --- | --- | --- |
| Transport | Local MCP over stdio | Remote MCP, listeners, authentication, hosted tenancy |
| Memory model | Agent-defined manifest-v2 relational schemas | Generated schema-specific tools or services |
| Lifecycle | Seed, design, materialise, adjacent additive evolve | Rename/remove/rebuild and arbitrary repair |
| Records | Generic CRUD, history, optimistic revisions, idempotent replay | Cross-service transactions |
| Retrieval | Exact scalar and declared list-membership filters | Substring, fuzzy, semantic, embedding, cross-service search |
| Organisation | Parentable branches, primary/related membership | Alias or redirect semantics |
| Discovery | Shape-only bootstrap, bounded summaries/facets/samples | Automatic relevance or truth ranking |
| Maintenance | Read-only mechanical candidates | Automatic merge, archive, purge, or deletion |
| SQLite | Linux/macOS, one local POSIX host, cooperating same-user processes | Windows, network filesystems, multi-host, hostile same-user isolation |
| PostgreSQL | Generic stdio reference adapter, majors 16–18 | Database/role provisioning, hosted tenancy |
| Administration | Read-only inspection, lifecycle, logical export/import, deliberate purge | Raw SQL, arbitrary migration/repair |
| Portability | Strict logical cross-backend artifact and dry run | Physical database backup, replication, best-effort import |
| Distribution | Wheel, sdist, `aipcs` entry point, isolated `uvx` verification | Package-index publication and GA version |
| Support | Pre-release compatibility envelope | Release/support/security-fix/deprecation windows |

See [compatibility](compatibility.md), [security](security.md), and
[design evolution](design-evolution.md) for the normative boundaries.
