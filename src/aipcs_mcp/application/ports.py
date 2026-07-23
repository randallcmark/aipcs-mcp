"""Narrow registry-only application ports."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from aipcs_mcp.lifecycle import LifecycleCommand, LifecycleIntent, RecoveryState

from .models import (
    AuditEvent,
    CompletedLifecycleClaim,
    EvolveCompletion,
    LifecycleRegistryOutcome,
    MaterialiseCompletion,
    NonLifecycleKind,
    NonLifecycleRegistryOutcome,
    RecoveryRequiredLifecycleClaim,
    Service,
    ServiceSaveResult,
)


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdProvider(Protocol):
    def new_service_id(self) -> UUID: ...


class ServiceRepository(Protocol):
    """Principal-scoped detached snapshots; list order is created time then service ID."""

    def find_domain(self, principal_id: str, domain_name: str) -> Service | None: ...
    def get(self, principal_id: str, service_id: UUID) -> Service | None: ...
    def list(self, principal_id: str, limit: int) -> list[Service]: ...
    def add(self, service: Service) -> None: ...
    def save(
        self, service_snapshot: Service, expected_service_revision: int
    ) -> ServiceSaveResult: ...


class MutationRegistry(Protocol):
    """One global typed idempotency and lifecycle registry, owned by the transaction."""

    def recovery_state(self, principal_id: str, service_id: UUID) -> RecoveryState:
        """Return one closed aggregate without exposing lifecycle-row evidence."""

    def resolve_non_lifecycle(
        self,
        kind: NonLifecycleKind,
        principal_id: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> NonLifecycleRegistryOutcome: ...

    def complete_non_lifecycle(
        self,
        kind: NonLifecycleKind,
        principal_id: str,
        idempotency_key: str,
        fingerprint: str,
        service: Service,
    ) -> None: ...

    def resolve_or_admit(self, command: LifecycleCommand) -> LifecycleRegistryOutcome:
        """Resolve a key or return prepared/stale/unsupported/blocker typed evidence."""

    def finalize_completed(
        self, completion: MaterialiseCompletion | EvolveCompletion
    ) -> CompletedLifecycleClaim | RecoveryRequiredLifecycleClaim: ...

    def finalize_recovery_required(
        self, prepared_intent: LifecycleIntent, at: datetime
    ) -> CompletedLifecycleClaim | RecoveryRequiredLifecycleClaim: ...


class AuditRepository(Protocol):
    def append(self, event: AuditEvent) -> None: ...


class RegistryUnitOfWork(Protocol):
    services: ServiceRepository
    mutations: MutationRegistry
    audits: AuditRepository

    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


UowFactory = Callable[[], RegistryUnitOfWork]
