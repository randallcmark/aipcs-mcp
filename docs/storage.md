# Storage and deployment

AIPCS is local-first and stdio-only. One running process uses one configured
backend and one fixed principal.

## SQLite

SQLite is the default reference backend for one Linux or macOS host. Use a
local POSIX filesystem and a private operator-owned data root. AIPCS requires
safe ownership and modes, uses persistent WAL, permits concurrent readers, and
serialises writers through SQLite's writer slot.

Example:

```text
uvx --from aipcs-mcp aipcs serve \
  --profile sqlite \
  --principal-id my-agent \
  --sqlite-data-root /absolute/private/path/aipcs-data
```

Do not place the root on NFS, SMB, a synchronised folder, or another network
filesystem. Do not copy only a live `.sqlite` file or edit WAL/SHM files.
SQLite is not a hostile-same-user or multi-host isolation boundary.

## PostgreSQL

PostgreSQL is the secondary reference adapter and demonstrates the same public
behavior behind the storage ports. Supported majors are 16 through 18.
Install the optional dependency and provide a database through a referenced
environment variable:

```text
export AIPCS_DATABASE_DSN='postgresql://ROLE:PASSWORD@HOST:5432/DATABASE'
uvx --from 'aipcs-mcp[postgresql]' aipcs serve \
  --profile postgresql \
  --principal-id my-agent \
  --postgres-dsn-env AIPCS_DATABASE_DSN
```

AIPCS expects an operator-provisioned database and a dedicated non-superuser
role. It creates the fixed `aipcs_registry` schema and one private
`svc_<uuid>` schema per service. The role must be able to create and manage
those schemas. AIPCS does not create databases, roles, extensions, or grants.
Revoke public schema access and configure TLS verification in the DSN where
the connection crosses a trust boundary.

The DSN is read only when runtime storage is constructed. Configuration
reports and failures do not expose the reference name, DSN, endpoint,
credentials, database, schema identifiers, SQL, or driver details.

## Principal continuity

The configured principal scopes all services and records. It is an opaque
local ownership value, not authentication. Changing it selects a different
logical view of the same backend; it does not rename or transfer existing
data. Keep it stable and do not expose a persistent backend to untrusted
clients.

## Deployment boundary

Supported:

- a local stdio MCP process;
- SQLite on one supported host; or
- PostgreSQL 16–18 in an operator-managed environment, including a homelab,
  when the process still communicates with its MCP client over local stdio.

Deferred:

- remote MCP listeners;
- authentication and hosted tenancy;
- multi-host SQLite;
- adapter plugins and mixed-backend runtime composition; and
- automated database/role provisioning.

To move logical data between backends, use separate source export and
destination import commands as described in
[export and migration](backup-and-migration.md).
