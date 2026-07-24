# Contributing

The project is preparing for its first public release. The licence, public
contribution intake, code of conduct, vulnerability-reporting channel, and
release policy are tracked as later release-readiness decisions. Until those
are published, treat this document as the development workflow rather than an
invitation to submit code under unspecified terms.

## Development setup

Requirements are Python 3.12 or newer and `uv`.

```text
uv sync --group dev
uv run pytest -q
uv run ruff check .
```

Run the complete local release rehearsal before proposing a release-affecting
change:

```text
uv run python scripts/verify_release.py
```

PostgreSQL integration tests use only verifier-created disposable containers
with synthetic credentials. Do not point tests at an ambient, shared,
homelab, or production database.

## Change rules

- Preserve the stable generic MCP contract unless a versioned contract change
  is intentional and documented.
- Keep CLI and MCP behavior in shared application services.
- Validate agent-provided data before persistence.
- Keep storage-specific SQL and filesystem behavior behind adapter ports.
- Add tests and update public documentation for behavior or compatibility
  changes.
- Use synthetic fixtures only.

Never commit databases, WAL/SHM files, exports, snapshots, transcripts,
credentials, personal context, local paths, private research artifacts, or
maintainer-specific agent instructions.

## Security reports

Do not open a public issue containing a suspected vulnerability, credential,
private record, database, export artifact, or sensitive path. A dedicated
private reporting policy is not yet published; that is a blocker for general
availability and will be added before public contribution intake.
