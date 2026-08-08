# Release policy and checklist

## Versioning

The first public release is planned as `0.1.0`. Until `1.0.0`, minor releases
may make breaking changes with explicit release notes. Additive evolution is
preferred, especially for MCP tools, CLI commands, configuration, and storage
migrations.

GitHub is the source of truth. A GitHub release and PyPI publication are
separate maintainer approvals. PyPI publication is intentionally deferred until
the maintainer has completed a dedicated Trusted Publishing setup and release
rehearsal.

## Before a GitHub release

1. Confirm the working tree is clean and the candidate version and changelog
   are correct. The suite accepts a valid release version; the artifact
   verifier proves that source, wheel, and sdist versions match.
2. Run the manual **Release rehearsal** workflow on the exact candidate commit.
3. Repeat the documented fresh-clone smoke test on macOS and Ubuntu.
4. Review open security reports, dependency alerts, CodeQL findings, and any
   independent review findings. Critical or high-severity security or
   data-integrity issues block release; lower-severity findings are triaged by
   risk and effort.
5. Before changing repository visibility to public, enable GitHub Private
   Vulnerability Reporting, secret scanning/push protection where available,
   and confirm that `SECURITY.md` is current.
6. Run the manual **Package candidate** workflow. It builds distributions and
   records GitHub build provenance attestations for them.
7. Create and annotate the `v0.1.0` tag and GitHub release, including known
   limitations and migration notes.

Normal CI automatically scans reachable Git history for public-hygiene
violations. The manual release rehearsal repeats that scan alongside the
artifact, SQLite, and PostgreSQL checks on the exact candidate commit.

## Later PyPI publication

PyPI publication requires a separate approval. Configure a PyPI Trusted
Publisher for a dedicated publishing workflow before publishing; do not add a
long-lived PyPI token to repository secrets. Publish only artefacts built and
attested by GitHub Actions, then test the normal package-name invocation:

```text
uvx --from aipcs aipcs --help
```

## Maintenance

Only the latest public release receives best-effort maintenance and security
fixes. There is no response-time SLA, supported backport line, or guaranteed
release cadence. Planned non-security removals should be announced at least one
minor release ahead when feasible; immediate changes remain possible for
security or correctness.
