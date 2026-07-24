# Administration CLI

The `aipcs` command operates one resolved profile per invocation. JSON is the
stable default output; add `--format human` for interactive reading.

Examples using `uvx --from aipcs-mcp` show the intended post-publication
package name. Before publication, replace `aipcs-mcp` with a supplied wheel,
source archive, or Git URL as described in the [quickstart](quickstart.md).

Every command accepts the same explicit configuration selectors documented in
[configuration](configuration.md). Examples below use SQLite; substitute a
PostgreSQL profile and DSN reference when appropriate.

```text
aipcs status
aipcs doctor [--service SERVICE_ID]
aipcs storage status [--service SERVICE_ID]
aipcs service list [--limit N]
aipcs service inspect SERVICE_ID
aipcs service suspend|resume|archive|restore SERVICE_ID
aipcs export SERVICE_ID --output FILE
aipcs import --input FILE [--dry-run]
aipcs service purge SERVICE_ID
aipcs maintenance scan --service SERVICE_ID
```

## Read-only checks

`status`, `doctor`, `storage status`, `service list`, `service inspect`, and
`maintenance scan` never create, migrate, repair, or change storage.

```text
uvx --from aipcs-mcp aipcs doctor \
  --profile sqlite \
  --principal-id my-agent \
  --sqlite-data-root /absolute/private/path/aipcs-data \
  --format human
```

An uninitialised or outdated store is reported rather than changed. Start
`aipcs serve` to perform the supported registry startup migration. Service
store migrations occur only on an admitted mutation, never during a read-only
admin command.

## Mutations and recovery values

Lifecycle and transfer mutations are revision-bound and idempotent. In
interactive mode, the CLI may generate an operation UUID, displays it with the
exact action and revision, then asks for confirmation. Save the operation UUID
until the terminal result is known.

In non-interactive use, supply all recovery-critical values:

```text
aipcs service suspend SERVICE_ID \
  --expected-revision 3 \
  --operation-id 11111111-2222-4333-8444-555555555555 \
  --yes \
  --profile sqlite \
  --principal-id my-agent \
  --sqlite-data-root /absolute/private/path/aipcs-data
```

If a result is retryable or the transport outcome is unknown, retry the exact
command with the same revision, operation ID, and inputs. A different request
under the same operation ID fails safely.

## Lifecycle transitions

- `suspend`: active to suspended;
- `resume`: suspended to active;
- `archive`: active or suspended to archived;
- `restore`: archived to suspended; and
- `purge`: archived to terminal tombstone.

Archive and purge require explicit confirmation. Purge additionally requires
either `--receipt RECEIPT` from a completed export or `--override`, plus exact
service identity confirmation:

```text
aipcs service purge SERVICE_ID \
  --expected-revision REVISION \
  --operation-id OPERATION_UUID \
  --receipt RECEIPT \
  --confirm-service-id SERVICE_ID \
  --yes \
  [configuration options]
```

`--override` is a deliberate replacement for receipt authority, not a repair
or force-delete mode. Purge is irreversible.

## Exit behavior

Success exits 0. Usage, validation, confirmation, conflict, stale revision,
unsupported operation, storage state, retryable uncertainty, and internal
failure have stable nonzero mappings. Automation should inspect the JSON error
`code` and `retryable` fields rather than parse human messages.

Errors never intentionally include paths, principals, DSNs, endpoints,
credentials, SQL, driver text, or operation evidence.
