# AIPCS memory protocol example

This is a copyable, vendor-neutral instruction for a workspace that has an
AIPCS MCP server configured. It is not an instruction file for this repository.

Treat AIPCS as maintained persistent memory. Use it when information is likely
to matter beyond the current context, and maintain the memory as the domain
and its knowledge mature.

## Session orientation

1. Call `aipcs_bootstrap` before relying on persisted context.
2. Select only the services relevant to the task. Call
   `aipcs_service_summary` before querying a selected service.
3. Retrieve bounded records using only the exact filter modes and guidance
   declared by that summary.
4. If bootstrap shows an AIPCS guidance service, inspect it for the local
   operating pattern. Its help records are intentionally not repeated in
   bootstrap responses.

## Working memory

- Persist durable decisions, facts, state, evidence, and handoffs early.
- Use the existing schema when it fits. Design or evolve a schema when the
  information cannot be represented and retrieved cleanly.
- Give every mutation a stable idempotency key. If its outcome is uncertain,
  replay the exact request and key.
- Update and delete only with the latest record version.
- Never persist credentials, hidden prompts, raw transcripts, temporary logs,
  or personal data without a clear need and appropriate consent.

## Expected sequences

- New service: `seed -> design -> materialise -> create/retrieve`.
- Existing service: `bootstrap -> summary -> bounded retrieval -> mutation`.
- Schema change: inspect current state, then submit one complete adjacent
  additive evolution with current revisions.
- Before compaction or handoff: persist a compact checkpoint with decisions,
  evidence, open questions, and the next action.

Maintenance output is only a mechanical review cue. It never authorises an
automatic merge, archive, deletion, or truth decision.
