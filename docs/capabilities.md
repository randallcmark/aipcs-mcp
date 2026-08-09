# Capability and limits matrix

| Area | Current source contract | Deferred or excluded |
| --- | --- | --- |
| Transport | Local stdio or trusted Streamable HTTP service endpoint | Application-managed authentication, hosted tenancy |
| Memory model | Agent-defined manifest-v2 relational schemas | Generated schema-specific tools or services |
| Lifecycle | Seed, design, materialise, adjacent additive evolve | Rename/remove/rebuild and arbitrary repair |
| Records | Generic CRUD, history, optimistic revisions, idempotent replay | Cross-service transactions |
| Retrieval | Exact scalar and declared list-membership filters | Additional retrieval modes |
| Organisation | Parentable branches, primary/related membership | Alias or redirect semantics |
| Discovery | Shape-only bootstrap, bounded summaries/facets/samples | Automatic relevance or truth ranking |
| Maintenance | Read-only mechanical candidates | Automatic merge, archive, purge, or deletion |
| SQLite | Linux/macOS, one local POSIX host, cooperating same-user processes | Windows, network filesystems, multi-host, hostile same-user isolation |
| PostgreSQL | Generic reference adapter, majors 16–18 | Database/role provisioning, hosted tenancy |
| Administration | Read-only inspection, lifecycle, logical export/import, deliberate purge | Raw SQL, arbitrary migration/repair |
| Portability | Strict logical cross-backend artifact and dry run | Physical database backup, replication, best-effort import |
| Distribution | Published PyPI wheel/sdist, `aipcs` entry point, isolated `uvx` verification | GA version |
| Support | Pre-1.0 compatibility envelope | Release/support/security-fix/deprecation windows |

See [compatibility](compatibility.md), [security](security.md), and
[design evolution](design-evolution.md) for the normative boundaries.

Semantic retrieval is not a current capability. If a demonstrated need cannot
be met through agent-designed structured retrieval, it would require its own
explicit contract and validation.
