# AIPCS memory protocol example

This file is a copyable example. It does not govern development of the
`aipcs-mcp` repository.

## Session start

1. Call `aipcs_bootstrap` before relying on memory.
2. Select a relevant service and call `aipcs_service_summary`.
3. Retrieve bounded records using only the filter modes declared by the
   summary.

## Persistence

- Persist decisions, durable facts, useful state, and explicit handoffs when
  they become stable enough to help a future session.
- Use the service's existing schema when it represents the information
  cleanly.
- Use a stable idempotency key for each intended mutation. If the result is
  uncertain, retry the exact request and key.
- Update and delete only with the latest record version.
- Do not store credentials, hidden prompts, raw transcripts, temporary logs,
  or personal data without an explicit need and appropriate consent.

## Schema evolution

- Inspect the current service before proposing a schema change.
- Evolve only when the current schema cannot represent durable information
  cleanly.
- Submit one complete adjacent additive manifest with exact current service
  and schema revisions.
- Never send SQL or treat migration annotations as executable instructions.

## Maintenance

- Treat maintenance output as mechanical candidates for review.
- Retrieve relevant records before deciding.
- Never infer that a stale, unbranched, low-confidence, duplicate-reference,
  or superseded candidate should be deleted automatically.

## Before compaction or handoff

Persist a compact checkpoint containing:

- decisions and rationale;
- completed work and evidence;
- open risks or questions; and
- the next concrete action.

After compaction, retrieve that checkpoint rather than relying on a compressed
conversation summary.
