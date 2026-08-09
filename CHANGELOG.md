# Changelog

All notable user-visible changes are recorded here. AIPCS is pre-1.0, so
minor releases may include breaking changes; those changes will be called out
explicitly.

## 0.1.1 — unreleased

### Added

- A local-SQLite-first Docker image that runs as an unprivileged user and
  persists the default data root in a Docker volume.

### Changed

- Rebuilt the container Python runtime against SQLite 3.53.4.
- Pinned PyPI publishing and disabled persisted checkout credentials in GitHub
  Actions workflows.
- Replaced production assertion guards with explicit invariant handling.

## 0.1.0 — released 2026-08-09

Initial public pre-release of the generic, local-first AIPCS MCP server.

### Added

- `aipcs` distribution, command, MCP server identity, and default local data
  root.
- SQLite and operator-provisioned PostgreSQL 16–18 reference backends.
- Copyable agent instructions and an optional seeded guide service for
  low-payload agent orientation.
