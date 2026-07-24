# Export, import, and migration

AIPCS moves logical service state with a backend-neutral export artifact. It
does not copy database files, PostgreSQL schemas, credentials, migration
ledgers, or adapter-private state.

## What the artifact contains

Export format v1 is strict canonical UTF-8 JSON Lines containing logical
service metadata, manifest, records, record history, branches, and membership.
SHA-256 framing detects accidental corruption, truncation, duplication,
substitution, and reordering.

The artifact is not encrypted or signed. Treat it as sensitive memory content
and protect it with operator controls. Digests do not establish author
authenticity.

## Export

A materialised service must first be suspended or archived. Use the current
service revision and one stable operation UUID:

```text
aipcs export SERVICE_ID \
  --output /private/path/service.aipcs.jsonl \
  --expected-revision REVISION \
  --operation-id OPERATION_UUID \
  --yes \
  [source configuration options]
```

Export writes through a private same-directory temporary file, fsyncs it, and
publishes without replacement. It never overwrites an existing destination.
The result contains a receipt used as purge authority; store it separately
until any purge decision is complete.

## Validate and import

Run a destination-side zero-write validation first:

```text
aipcs import \
  --input /private/path/service.aipcs.jsonl \
  --dry-run \
  [destination configuration options]
```

Then perform the import with a stable operation UUID:

```text
aipcs import \
  --input /private/path/service.aipcs.jsonl \
  --operation-id OPERATION_UUID \
  --yes \
  [destination configuration options]
```

Import rejects symlinks and non-regular files, validates the complete bundle
before any destination write, stages unpublished state, and publishes only
after exact re-observation. Exact retries replay. There is no overwrite,
remap, clone, merge, skip, partial import, or best-effort mode.

## Backend migration

Cross-backend migration is two commands, never one mixed-backend process:

1. configure the source backend;
2. suspend or archive and export;
3. configure the destination backend separately;
4. dry-run import;
5. import with a stable operation UUID;
6. inspect the destination and sample important records;
7. start the destination MCP process and verify agent retrieval; and
8. keep the source and artifact until the cutover is accepted.

SQLite-to-PostgreSQL and PostgreSQL-to-SQLite are release-tested. Logical
identity is preserved while ownership binds to the destination principal.

## Rollback

Before purge, rollback means stop using the destination and resume or restore
the retained source:

- suspended source: `service resume`;
- archived source: `service restore`, which returns it to suspended, then
  `service resume`.

After source purge there is no rollback to that installation. Recovery requires
importing a retained artifact into a destination where its identity is
available. A tombstoned identity is not reusable in the purged registry.

This is logical transfer and recovery, not an online physical backup policy.
Retention schedules, encryption, off-site copies, PostgreSQL physical backup,
and SQLite online backup remain operator responsibilities.
