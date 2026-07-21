# Security and trust boundary

AIPCS treats agent-provided schema and configuration input as untrusted. The
current public contract validates it before any future persistence boundary is
reached.

## Input and transport

- Public models reject unknown fields and enforce bounded, typed values.
- Service identifiers must be lowercase canonical UUIDs.
- Retired fields, storage locations, connection strings, and endpoint settings
  are rejected from normal input.
- Manifest v1 is accepted only by the explicit one-way converter, never by
  normal public design input.
- Public v1 is stdio only. Listener-oriented transport settings are rejected
  before any MCP server construction.

No listener, remote transport, hosted service, or authentication flow is
implemented in this slice.

## Safe responses

Contract failures use structured envelopes with a stable code, concise message,
and remediation details. Validation failures are mapped to public-facing
issues; tracebacks, database details, credentials, and local paths are not
part of the contract.

Capability information intentionally reports only public features: package and
contract versions, manifest support, enabled transport, and safe operational
status. It must not expose credentials, DSNs, filesystem locations, owner
information, or network endpoints.

## Test data and operational boundary

All repository test data is synthetic and marked with its provenance. Do not
add agent transcripts, operational stores, database copies, credentials, or
personal context to this repository.

Persistence, storage adapters, lifecycle operations, backup/export, and
operator administration are not implemented yet. Future slices must preserve
the same boundary: validate input before persistence, keep configuration
separate from data, and redact sensitive implementation details from errors
and capability output.

There is not yet a supported release, maintenance, or security-fix policy.
Treat this pre-release contract as development software and report potential
security issues through the project's future published security channel.
