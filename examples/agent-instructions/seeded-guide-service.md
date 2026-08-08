# Seeded AIPCS guide service

Bootstrap is deliberately a small structural response. If a workspace wants
fresh agents to have tool and memory-design guidance without attaching it to
every bootstrap refresh, create this service once. Its intent makes it visible
in bootstrap; an agent then chooses it and retrieves its bounded guidance
records as needed.

This is example knowledge, not a required AIPCS system service. Adapt the
language, schema, and records to the host's operating policy. Do not put
credentials, local paths, private transcripts, or user data in the guide.

## 1. Seed and design

Call `aipcs_service_seed`:

```json
{
  "domain_name": "aipcs_guide",
  "domain_class": "reference",
  "intent_description": "START HERE: AIPCS guidance for tool sequencing, safe persistence, and schema evolution. Inspect this service when a fresh agent needs operating help.",
  "idempotency_key": "seed-aipcs-guide-v1"
}
```

Keep the returned `service_id`. Then call `aipcs_service_design` with that ID:

```json
{
  "service_id": "REPLACE_WITH_SERVICE_ID",
  "idempotency_key": "design-aipcs-guide-v1",
  "schema": {
    "manifest_version": 2,
    "schema_version": 1,
    "entities": [
      {
        "name": "guidance",
        "attributes": [
          {"name": "id", "type": "uuid", "required": true, "primary_key": true},
          {"name": "owner_id", "type": "string", "required": true},
          {"name": "created_at", "type": "datetime", "required": true},
          {"name": "updated_at", "type": "datetime", "required": true},
          {"name": "created_via", "type": "string", "required": true},
          {"name": "record_version", "type": "integer", "required": true},
          {"name": "subject", "type": "string", "required": true},
          {"name": "kind", "type": "string", "required": true},
          {"name": "summary", "type": "string", "required": true, "retrieval_mode": "annotation"},
          {"name": "detail", "type": "string", "required": true, "retrieval_mode": "annotation"}
        ]
      }
    ],
    "relationships": [],
    "indices": [
      {"name": "guidance_subject_idx", "entity": "guidance", "fields": ["subject"]},
      {"name": "guidance_kind_idx", "entity": "guidance", "fields": ["kind"]}
    ],
    "query_patterns": ["Retrieve one guidance subject by exact subject or kind."],
    "discovery_facets": [{"entity": "guidance", "field": "subject"}],
    "retrieval_guidance": "Use exact subject or kind. Retrieve only the guidance needed for the current task.",
    "migration_history": []
  }
}
```

Materialise it with the exact revisions returned by design.

## 2. Create the guide records

Use `aipcs_record_create` once for each record below, replacing the service ID.
The idempotency keys are deliberately stable so an uncertain call can be
replayed exactly.

```json
{
  "service_id": "REPLACE_WITH_SERVICE_ID",
  "entity_name": "guidance",
  "record": {
    "subject": "start",
    "kind": "workflow",
    "summary": "Orient before relying on memory.",
    "detail": "Call bootstrap. Select only relevant services, then call service summary. Follow its declared exact filters and retrieve bounded records; bootstrap is shape-only and does not recall record values."
  },
  "idempotency_key": "create-aipcs-guide-start-v1"
}
```

```json
{
  "service_id": "REPLACE_WITH_SERVICE_ID",
  "entity_name": "guidance",
  "record": {
    "subject": "persistence",
    "kind": "workflow",
    "summary": "Persist independently retrievable durable units early.",
    "detail": "Store decisions and rationale, stable facts, completed evidence, useful state, unresolved risks, and explicit next actions. Do not dump a transcript; choose fields and exact retrieval cues that a later agent can use."
  },
  "idempotency_key": "create-aipcs-guide-persistence-v1"
}
```

```json
{
  "service_id": "REPLACE_WITH_SERVICE_ID",
  "entity_name": "guidance",
  "record": {
    "subject": "mutation_safety",
    "kind": "safety",
    "summary": "Use idempotency and current revisions for safe changes.",
    "detail": "Every intended mutation gets a stable idempotency key. Replay the exact request and key when its outcome is uncertain. Retrieve fresh state before updating or deleting, and use the latest record or service revision required by the operation."
  },
  "idempotency_key": "create-aipcs-guide-mutation-safety-v1"
}
```

```json
{
  "service_id": "REPLACE_WITH_SERVICE_ID",
  "entity_name": "guidance",
  "record": {
    "subject": "schema_evolution",
    "kind": "workflow",
    "summary": "Let the agent own the memory model as knowledge grows.",
    "detail": "Use a service's current schema when it supports the required durable information and exact recall. When it no longer fits, inspect the complete manifest and evolve through one adjacent additive change. Do not compensate for a poor model with unbounded listings or invented filter modes."
  },
  "idempotency_key": "create-aipcs-guide-schema-evolution-v1"
}
```

```json
{
  "service_id": "REPLACE_WITH_SERVICE_ID",
  "entity_name": "guidance",
  "record": {
    "subject": "handoff",
    "kind": "workflow",
    "summary": "Leave a compact durable checkpoint before a context handoff.",
    "detail": "Persist the decisions, evidence, open questions, and next concrete action that a future agent will need. On resumption, bootstrap and retrieve that bounded checkpoint instead of relying on a compressed conversation summary."
  },
  "idempotency_key": "create-aipcs-guide-handoff-v1"
}
```

## 3. Fresh-agent use

With the copyable [AGENTS.md](AGENTS.md) installed in the host workspace, a
fresh agent calls `aipcs_bootstrap`, sees the guide service's explicit intent,
calls `aipcs_service_summary` for it, and searches `guidance` by the exact
`subject` or `kind` needed. It can then select the domain service and follow
the same bootstrap-to-summary-to-bounded-retrieval sequence.
