# Troubleshooting

Start with JSON output:

```text
aipcs config validate [configuration options]
aipcs status [configuration options]
aipcs doctor [configuration options]
aipcs storage status [configuration options]
```

These commands are bounded and redacted. Do not add database credentials,
storage paths, or private record content to public issue reports.

## The profile validates but the server does not start

`config validate` checks structure only. It does not read the PostgreSQL DSN,
load an optional driver, open a path, connect to a database, or inspect
migrations. `serve` performs those live checks and may fail later.

For SQLite, verify the supported platform, local filesystem, private ownership
and modes, SQLite version, and parent accessibility. For PostgreSQL, verify the
`[postgresql]` extra, referenced environment variable, connectivity, dedicated
role permissions, and supported major version.

## Read-only commands report uninitialised or migration required

That is intentional: inspection never mutates storage. Start the configured
`aipcs serve` process to perform the supported registry migration. A supported
service-store predecessor migrates only when an admitted mutation needs it.
There is no general repair command.

## `storage_busy` or `operation_uncertain`

Check the JSON `retryable` field. When true, retry the exact request with the
same idempotency key or operation UUID, revision, and content. Do not change an
input or generate a new identifier while the original outcome is unknown.

SQLite serialises writers and uses one bounded busy timeout. Persistent
contention may mean another cooperating process has a long write transaction.

## `stale_revision`

Another successful mutation advanced the object. Inspect it again, decide
whether the intended action still applies, then issue a new operation with the
new exact revision and a new idempotency key or operation UUID.

## `changed_fingerprint`

The same idempotency key or operation UUID was reused with different content.
Recover the original request and replay it exactly, or use a new identifier
for a genuinely new action.

## Import is rejected

Use a regular, non-symlink file. Confirm it is a complete AIPCS export, has not
been modified, is within documented limits, and targets a registry where the
service identity is available. Run `import --dry-run` before an actual import.

## SQLite database files appear incomplete when copied

Do not copy only the main database while WAL is active. Retain the complete
data root and use the logical export workflow for supported cross-installation
transfer. A physical online-backup procedure is not currently supplied.

## Safe diagnostics

Public errors are intentionally terse. Preserve:

- package version;
- MCP contract version from `aipcs_server_info`;
- operating system and SQLite or PostgreSQL major version;
- command name, safe JSON error code, and retryable flag; and
- whether the problem reproduces with synthetic data.

Do not publish config files, environment values, paths, DSNs, logs containing
record prose, export artifacts, databases, transcripts, or personal agent
instructions.
