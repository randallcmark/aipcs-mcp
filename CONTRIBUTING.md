# Contributing

The project is preparing for its first public release. Contributors retain
copyright in their work; by submitting a contribution, they license it under
[Apache-2.0](LICENSE). The maintainer welcomes legitimate issues and pull
requests, but cannot promise a review, response, or merge time. Maintainers
may close work that is out of scope or cannot be sustained.

Every pull request must link an issue so that scope, alternatives, and review
history are visible. Participation is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md); conduct reports may be sent privately to
[conduct@indigo-blocks.uk](mailto:conduct@indigo-blocks.uk).

## Development setup

Requirements are Python 3.12 or newer and `uv`. The complete SQLite-backed
suite additionally requires a Python runtime bundling SQLite 3.51.3 or newer.
The supported local route is managed Python 3.14:

```text
uv python install 3.14
uv sync --group dev --python 3.14
uv run --python 3.14 pytest -q
uv run --python 3.14 ruff check .
```

On an older SQLite runtime, the regular test command skips SQLite-backed tests
with an explicit safety reason and still runs runtime-independent coverage. Do
not treat that as full SQLite validation.

Run the complete local release rehearsal before proposing a release-affecting
change:

```text
uv run python scripts/verify_release.py
```

PostgreSQL integration tests use only verifier-created disposable containers
with synthetic credentials. Do not point tests at an ambient, shared,
homelab, or production database.

## Change rules

- Open or link an issue before submitting a pull request so the intent,
  alternatives, and review trail are visible.

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
private reporting policy is in [SECURITY.md](SECURITY.md). Once this repository
is public, use GitHub's **Report a vulnerability** flow in its Security tab.
