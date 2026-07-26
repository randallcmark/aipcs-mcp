# Storage and deployment

AIPCS is local-first. One running process uses one configured backend and one
fixed principal. `stdio` is the default local transport; Streamable HTTP is a
supported trusted-service transport.

Examples using the `aipcs-mcp` distribution name are post-publication forms.
Before publication, replace the value after `uvx --from` with a supplied wheel,
source archive, or Git URL as described in the [quickstart](quickstart.md).

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
  with either local stdio or a configured Streamable HTTP endpoint; and
- one Streamable HTTP MCP endpoint for a trusted operator deployment, with
  loopback binding by default and explicit non-loopback/Host policy when a
  reverse proxy or private network requires it.

The HTTP listener is not an authentication, authorization, tenancy, or TLS
implementation. Keep the default loopback bind for a local service. For a
homelab or container deployment, place a non-loopback listener behind a
trusted authenticated TLS gateway, configure its exact Host header in AIPCS,
and do not expose the AIPCS port directly to an untrusted LAN or the Internet.

## Container service example

The supplied `Dockerfile` builds the PostgreSQL-capable reference image. It
does not create a database, store a DSN, publish a port, or provide a reverse
proxy. Supply an operator-owned configuration file and secret reference at
runtime.

Build a local image:

```text
docker build --tag aipcs-mcp:local .
```

For a service behind a host-local reverse proxy, the container must bind its
own network interface while Docker publishes the port only to the host's
loopback address. A generic configuration is:

```toml
config_version = 1
profile = "postgresql"
transport = "streamable-http"

[identity]
principal_id = "homelab_memory"

[postgresql]
dsn_env = "AIPCS_DATABASE_DSN"

[streamable_http]
host = "0.0.0.0"
port = 8000
path = "/mcp"
allowed_hosts = ["127.0.0.1:8000", "localhost:8000"]
allowed_origins = []
session_idle_timeout_seconds = 1800
allow_non_loopback = true
```

Run it on an operator-managed Docker network that can reach PostgreSQL, but
publish the MCP port only to the host loopback:

```text
docker run --rm \
  --network homelab-internal \
  --publish 127.0.0.1:8000:8000 \
  --env AIPCS_DATABASE_DSN \
  --volume /absolute/operator/config.toml:/etc/aipcs/config.toml:ro \
  aipcs-mcp:local
```

An upstream proxy may terminate TLS and authenticate clients before forwarding
to that loopback port. Its public hostname must be added to `allowed_hosts` if
it preserves the original Host header. Do not enable a broad Docker port
publish (`8000:8000`) unless the network itself is deliberately trusted and
the authentication boundary is already enforced upstream.

Deferred:

- application-managed authentication and hosted tenancy;
- multi-host SQLite;
- adapter plugins and mixed-backend runtime composition; and
- automated database/role provisioning.

To move logical data between backends, use separate source export and
destination import commands as described in
[export and migration](backup-and-migration.md).
