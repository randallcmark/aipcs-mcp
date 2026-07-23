# Configuration

AIPCS resolves configuration once for each CLI invocation. The resolved snapshot
is immutable and is passed to runtime wiring; application use cases do not read
configuration files or environment variables.

Configuration selects a fixed V1 runtime shape. Resolution itself never creates
storage, connects to a database, or migrates an adapter. `serve` is the only
command that evaluates live SQLite readiness.

## Commands

    aipcs config show [--config PATH] [overrides]
    aipcs config validate [--config PATH] [overrides]
    aipcs serve [--config PATH] [overrides]

`config show` prints one redacted JSON success envelope and may inspect a
profile that cannot run on the current platform. `config validate` confirms a
structurally runnable profile; it does not probe the configured path or
database. `serve` requires a runnable profile and is the sole command that
constructs storage, performs the single startup migration, and starts MCP.
`serve` emits no configuration report on stdout because stdout belongs to the
stdio MCP protocol.

The supported non-secret overrides are `--profile`, `--transport`,
`--principal-id`, `--sqlite-data-root`, `--sqlite-busy-timeout-ms`,
`--postgres-dsn-env`, and `--log-level`.
When `serve` succeeds, the resolved log level is applied to stderr logging;
configuration commands do not mutate process logging.

## Precedence and file selection

For every field, the winning value is:

    explicit CLI option > documented AIPCS environment variable > selected TOML file > default

The resolver records the winning source without retaining or reporting losing
values. An omitted value differs from a present blank value; blank required
values fail validation.

| Field | Environment variable |
| --- | --- |
| Profile | `AIPCS_PROFILE` |
| Transport | `AIPCS_TRANSPORT` |
| Principal identity | `AIPCS_PRINCIPAL_ID` |
| SQLite data-root descriptor | `AIPCS_SQLITE_DATA_ROOT` |
| SQLite busy timeout (milliseconds) | `AIPCS_SQLITE_BUSY_TIMEOUT_MS` |
| PostgreSQL DSN reference | `AIPCS_POSTGRES_DSN_ENV` |
| Stderr log level | `AIPCS_LOG_LEVEL` |

Only these exact names participate in AIPCS field precedence. For an omitted
SQLite root, `XDG_DATA_HOME`, `HOME`, or future Windows `LOCALAPPDATA` is read
solely to select the documented redacted platform default.
Present-but-blank values fail validation; they do not fall through to a
lower-precedence source.

Before resolution, the stdio safety guard also rejects non-stdio values in
`AIPCS_MCP_TRANSPORT`, plus any nonblank listener setting in `FASTMCP_HOST`,
`FASTMCP_PORT`, `FASTMCP_MOUNT_PATH`, `FASTMCP_SSE_PATH`,
`FASTMCP_MESSAGE_PATH`, `FASTMCP_STREAMABLE_HTTP_PATH`, `AIPCS_HOST`,
`AIPCS_PORT`, `AIPCS_MOUNT_PATH`, `AIPCS_MCP_HOST`, `AIPCS_MCP_PORT`, or
`AIPCS_MCP_MOUNT_PATH`. These are rejection guards, not supported settings.

`--config PATH` is the only configuration-file selector. There is no environment
selector, implicit project file, current-working-directory search, package
directory search, or home-directory search. This makes editor and stdio launches
reproducible.

## TOML document

The selected file is a regular UTF-8 TOML file of at most 64 KiB with
`config_version = 1`. Its shape allows at most 128 keys and eight levels. It
rejects unknown keys, wrong types, malformed input, excessive structure, and
literal credential fields. Because the operator selects the path explicitly
and the document cannot contain supported secrets, a symlink to a regular file
is accepted.

    config_version = 1
    profile = "stateless"
    transport = "stdio"

    [identity]
    principal_id = "local_operator"

    [sqlite]
    busy_timeout_ms = 5000

    [logging]
    level = "warning"

The optional SQLite `data_root` descriptor must be absolute and no longer than
4,096 characters. When a SQLite profile omits it, resolution selects a redacted
platform default: `$XDG_DATA_HOME/aipcs-mcp` (or `~/.local/share/aipcs-mcp`) on
Linux, `~/Library/Application Support/aipcs-mcp` on macOS, and a future
`LOCALAPPDATA` location on Windows. Resolution never opens or creates this
path, and reports it as not explicitly configured. SQLite `busy_timeout_ms`
is an integer from 1 through 30,000 and defaults to 5,000. TOML must provide a
real integer; CLI and environment values must be canonical unsigned decimal
text (with no signs, whitespace, leading zeroes, or separators). Explicit
SQLite settings are invalid for stateless and PostgreSQL profiles. PostgreSQL requires a
`dsn_env` reference matching `^[A-Z][A-Z0-9_]{0,127}$`; its value is not read and
connectivity is not tested. Logging level is one of `debug`, `info`, `warning`,
or `error`.

## Profiles and availability

| Profile | Structural availability | `serve` behavior |
| --- | --- | --- |
| stateless | Available | Starts a server-info-only stdio process. |
| sqlite on Linux or macOS with SQLite 3.51.3+ | Available | Performs the sole explicit registry migration, then starts the five-tool lifecycle server only if storage is ready. |
| sqlite on Windows | Unavailable | Rejected before server construction. |
| postgresql | Unavailable | Rejected before server construction. |

Persistent profiles require an explicit printable `principal_id` of at most 128
characters. Stateless mode may omit
it. This remains true when `config show` inspects an unavailable persistent
profile. AIPCS does not derive it from the operating-system user, host, current
directory, or MCP client. It is an opaque local process principal, not an
authentication or tenancy mechanism.

The reports' `available` and `runnable` fields state structural profile support,
not live store readiness. Missing parents, unsafe permissions, inaccessible
locations, database incompatibility, and migration state are deliberately not
probed by configuration commands. They can only cause `serve` to fail safely
before MCP starts.

For a SQLite process, either supply an absolute `sqlite_data_root` or use the
redacted platform default described above. The root is a local POSIX directory;
cooperating processes under the same effective user may have many readers, but
SQLite serialises writers under the configured timeout. A contention result is
`StorageBusy`; the adapter performs no internal retry. Do not treat the profile
as network-filesystem, multi-host, hostile-same-user, or Windows support.
PostgreSQL fields remain recognised only for future configuration compatibility
and do not select an adapter.

## Safe reports and failures

`config show` contains only an allowlisted report: configuration version, selected
profile, availability, non-sensitive settings, principal/storage
configured/not-configured booleans, and field source labels. It never prints
principal values, file names, paths, DSN-reference names, credentials, endpoints,
raw TOML, or untrusted environment values.

`config validate` succeeds only for a structurally valid runnable profile.
Invalid configuration returns the existing `validation_failed` error envelope.
Listener requests return `transport_not_supported`; unavailable Windows SQLite
and PostgreSQL profiles return `unsupported_operation`. Failures are one public
envelope on stderr and return exit status 2; successful config commands return
one JSON success envelope on stdout and exit 0. A storage/startup failure from
`serve` is one bounded `internal_error` envelope on stderr; it does not start
MCP or expose a path, database name, principal, driver, or migration detail.

There is no dotenv loading, configuration inheritance, include mechanism,
secret-file support, remote configuration, profile plugin system, automatic
reload, database probe by configuration commands, or directory creation by
`config show`/`config validate`. There is no database URL, raw SQL, adapter
plugin, retry, or standalone administration command.

## Development use

The checkout commands below are development invocations, not a released install:

    uv run aipcs config validate
    uv run aipcs serve --profile sqlite --principal-id local-agent \
      --sqlite-data-root /absolute/operator-owned/aipcs-data

See [security](security.md) and [compatibility](compatibility.md).
