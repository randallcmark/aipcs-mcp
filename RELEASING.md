# Release policy and checklist

## Versioning

The package is published on PyPI as `aipcs`. Until `1.0.0`, minor releases
may make breaking changes with explicit release notes. Additive evolution is
preferred, especially for MCP tools, CLI commands, configuration, and storage
migrations.

GitHub is the source of truth. Publishing a GitHub release triggers the existing
[Publish to PyPI workflow](.github/workflows/publish-pypi.yml). It checks that the
release tag matches the package version, builds and attests the distributions,
then publishes through PyPI Trusted Publishing using the `pypi` environment.
Complete the release gates before publishing the GitHub release: package
publication follows automatically through that workflow.

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
7. Create an annotated `v<VERSION>` tag for the verified candidate and publish
   the GitHub release, including known limitations and migration notes. Monitor
   the triggered **Publish to PyPI** workflow through successful completion.

Normal CI automatically scans reachable Git history for public-hygiene
violations. The manual release rehearsal repeats that scan alongside the
artifact, SQLite, and PostgreSQL checks on the exact candidate commit.

## PyPI publication

Keep the PyPI Trusted Publisher configured for the existing publishing workflow
and `pypi` environment; do not add a long-lived PyPI token to repository secrets.
The workflow publishes only distributions built and attested by GitHub Actions.
After it succeeds, verify the expected version on PyPI and test the normal
package-name invocation:

```text
uvx --from aipcs aipcs --help
```

## Maintenance

Only the latest public release receives best-effort maintenance and security
fixes. There is no response-time SLA, supported backport line, or guaranteed
release cadence. Planned non-security removals should be announced at least one
minor release ahead when feasible; immediate changes remain possible for
security or correctness.
