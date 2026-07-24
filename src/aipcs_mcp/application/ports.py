"""Narrow registry-only application ports."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from aipcs_mcp.lifecycle import LifecycleCommand, LifecycleIntent, RecoveryState
from aipcs_mcp.records import RecordSpecification

from .models import (
    AuditEvent,
    CompletedLifecycleClaim,
    EvolveCompletion,
    LifecycleRegistryOutcome,
    MaterialisationStorage,
    MaterialiseCompletion,
    NonLifecycleKind,
    NonLifecycleRegistryOutcome,
    RecoveryRequiredLifecycleClaim,
    Service,
    ServiceSaveResult,
)
from .portable_store import (
    FenceTransitionResult,
    PortableStoreMember,
    StageResult,
    WriteAdmissionFence,
)
from .registry_authority import (
    RegistryAuthorityCommand,
    RegistryAuthorityIntent,
    RegistryAuthorityOutcome,
    TransferReceipt,
)

DataOperation = Literal[
    "create_record",
    "get_record",
    "list_records",
    "search_records",
    "update_record",
    "delete_record",
    "record_history",
    "create_branch",
    "list_branches",
    "update_branch",
    "assign_branch_records",
    "service_summary",
    "maintenance_scan",
]


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdProvider(Protocol):
    def new_service_id(self) -> UUID: ...


class AdmittedDataStore(Protocol):
    """Invoke one data operation after application-level registry admission.

    The gateway receives a detached logical allocation, never a registry unit
    of work. Composition owns any adapter-specific conversion from that value
    to its data-store locator.
    """

    def execute(
        self,
        operation: DataOperation,
        storage: MaterialisationStorage,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        value: object,
    ) -> object: ...


class ServiceRepository(Protocol):
    """Principal-scoped detached snapshots; list order is created time then service ID."""

    def find_domain(self, principal_id: str, domain_name: str) -> Service | None: ...
    def get(self, principal_id: str, service_id: UUID) -> Service | None: ...
    def count(self, principal_id: str) -> int: ...
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


class RegistryAuthority(Protocol):
    """Typed portable claims backed by the same global namespace as mutations."""

    def resolve_or_prepare(self, command: RegistryAuthorityCommand) -> RegistryAuthorityOutcome: ...

    def finalize_operational(self, prepared: RegistryAuthorityIntent, at: datetime) -> RegistryAuthorityOutcome: ...

    def complete_export(
        self, prepared: RegistryAuthorityIntent, receipt: TransferReceipt
    ) -> RegistryAuthorityOutcome: ...

    def publish_import(
        self, prepared: RegistryAuthorityIntent, service: Service, receipt: TransferReceipt
    ) -> RegistryAuthorityOutcome: ...

    def finalize_purge(self, prepared: RegistryAuthorityIntent, at: datetime) -> RegistryAuthorityOutcome: ...


class RegistryUnitOfWork(Protocol):
    services: ServiceRepository
    mutations: MutationRegistry
    audits: AuditRepository
    authority: RegistryAuthority

    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


UowFactory = Callable[[], RegistryUnitOfWork]


class StoreSnapshotReader(Protocol):
    """One stable, closeable backend snapshot in canonical member order."""

    @property
    def fence(self) -> WriteAdmissionFence: ...

    def __iter__(self) -> Iterator[PortableStoreMember]: ...
    def close(self) -> None: ...
    def __enter__(self) -> StoreSnapshotReader: ...
    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


class PortableServiceStore(Protocol):
    """Logical snapshot/staging and service-local write-admission boundary."""

    def open_snapshot(
        self, service: Service, expected_fence: WriteAdmissionFence
    ) -> StoreSnapshotReader: ...

    def stage(
        self,
        service: Service,
        members: Iterable[PortableStoreMember],
        initial_fence: WriteAdmissionFence,
    ) -> StageResult: ...

    def observe(
        self,
        service: Service,
        expected_fence: WriteAdmissionFence,
        members: Iterable[PortableStoreMember],
    ) -> bool: ...

    def read_fence(self, service: Service) -> WriteAdmissionFence: ...

    def transition_fence(
        self,
        service: Service,
        expected: WriteAdmissionFence,
        target_state: Literal["open", "closed"],
    ) -> FenceTransitionResult: ...
