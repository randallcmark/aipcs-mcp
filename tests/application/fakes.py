from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from aipcs_mcp.application.models import (
    AuditEvent,
    CompletedNonLifecycleClaim,
    ConflictClaim,
    NewClaim,
    NonLifecycleKind,
    NonLifecycleRegistryOutcome,
    Service,
    ServiceSaveResult,
)
from aipcs_mcp.application.operational_lifecycle import (
    OperationalLifecycleIntent,
    OperationalLifecyclePhase,
    transition_target_status,
)
from aipcs_mcp.application.registry_authority import (
    CompletedRegistryClaim,
    IdentityCollisionKind,
    ImportIdentity,
    LocalRegistryEvent,
    PortableIntent,
    PortableOperationKind,
    PreparedRegistryClaim,
    PurgeAuthorityKind,
    PurgeTombstone,
    ReceiptKind,
    ReceiptVerification,
    RecoveryRequiredRegistryClaim,
    RegistryAuthorityCommand,
    RegistryAuthorityIntent,
    RegistryAuthorityOutcome,
    RegistryClaimConflict,
    RegistryIdentityCollision,
    RegistryOperationInProgress,
    RegistryStaleRevision,
    RegistryUnsupportedTransition,
    TombstonedRegistryClaim,
    TransferReceipt,
    prepare_registry_intent,
)
from aipcs_mcp.lifecycle import RecoveryState

BACKEND_SENTINEL = "postgresql://secret@host/private"


class FixedClock:
    def __init__(self, *values: datetime):
        self.values = list(values) or [datetime(2026, 1, 1, tzinfo=UTC)]
        self.calls = 0

    def now(self) -> datetime:
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return value


class SequentialIds:
    def __init__(self, *values: UUID):
        self.values = list(values)
        self.calls = 0

    def new_service_id(self) -> UUID:
        self.calls += 1
        if self.values:
            return self.values.pop(0)
        return UUID(int=self.calls)


class FakeRegistry:
    """Durable fake state shared by fresh transaction objects."""

    def __init__(self) -> None:
        self.items: dict[UUID, Service] = {}
        self.ledger: dict[tuple[str, str], tuple[str, str, Service]] = {}
        self.events: list[AuditEvent] = []
        self.claims: dict[tuple[str, str], PreparedRegistryClaim | CompletedRegistryClaim | RecoveryRequiredRegistryClaim] = {}
        self.tombstoned_claims: dict[tuple[str, str], TombstonedRegistryClaim] = {}
        self.import_reservations: dict[UUID, ImportIdentity] = {}
        self.receipts: dict[UUID, TransferReceipt] = {}
        self.tombstones: dict[UUID, PurgeTombstone] = {}
        self.local_events: list[LocalRegistryEvent] = []
        self.recovery_states: dict[tuple[str, UUID], RecoveryState] = {}
        self.uows: list[FakeUow] = []
        self.commits = 0
        self.fail_at: str | None = None
        self.force_stale_save = False
        self.rollback_raises = False
        self.error_sentinel = BACKEND_SENTINEL

    def uow(self) -> FakeUow:
        if self.fail_at == "uow":
            raise RuntimeError(self.error_sentinel)
        value = FakeUow(self)
        self.uows.append(value)
        return value


class FakeUow:
    def __init__(self, state: FakeRegistry) -> None:
        self.state = state
        self.items = deepcopy(state.items)
        self.ledger = deepcopy(state.ledger)
        self.events = deepcopy(state.events)
        self.claims = deepcopy(state.claims)
        self.tombstoned_claims = deepcopy(state.tombstoned_claims)
        self.import_reservations = deepcopy(state.import_reservations)
        self.receipts = deepcopy(state.receipts)
        self.tombstones = deepcopy(state.tombstones)
        self.local_events = deepcopy(state.local_events)
        self.recovery_states = deepcopy(state.recovery_states)
        self.calls: list[str] = []
        self.closed = False
        self.close_count = 0
        self.terminal_action: str | None = None

    @property
    def services(self) -> FakeUow:
        return self

    @property
    def mutations(self) -> FakeUow:
        return self

    @property
    def audits(self) -> FakeUow:
        return self

    @property
    def authority(self) -> FakeUow:
        return self

    def find_domain(self, principal_id: str, domain_name: str) -> Service | None:
        self._called("find_domain")
        found = next(
            (
                service
                for service in self.items.values()
                if service.principal_id == principal_id and service.domain_name == domain_name
            ),
            None,
        )
        return deepcopy(found)

    def get(self, principal_id: str, service_id: UUID) -> Service | None:
        self._called("get")
        service = self.items.get(service_id)
        if service is None or service.principal_id != principal_id:
            return None
        return deepcopy(service)

    def count(self, principal_id: str) -> int:
        self._called("count")
        return sum(service.principal_id == principal_id for service in self.items.values())

    def list(self, principal_id: str, limit: int) -> list[Service]:
        self._called("list")
        scoped = [item for item in self.items.values() if item.principal_id == principal_id]
        ordered = sorted(scoped, key=lambda item: (item.created_at, item.service_id))
        return deepcopy(ordered[:limit])

    def add(self, service: Service) -> None:
        self._called("add")
        self.items[service.service_id] = deepcopy(service)

    def save(
        self, service_snapshot: Service, expected_service_revision: int
    ) -> ServiceSaveResult:
        self._called("save")
        if self.state.force_stale_save:
            return "stale_revision"
        current = self.items.get(service_snapshot.service_id)
        if (
            current is None
            or current.principal_id != service_snapshot.principal_id
            or current.service_revision != expected_service_revision
        ):
            return "stale_revision"
        if service_snapshot.service_revision != expected_service_revision + 1:
            raise RuntimeError(BACKEND_SENTINEL)
        self.items[service_snapshot.service_id] = deepcopy(service_snapshot)
        return "saved"

    def resolve_non_lifecycle(
        self,
        kind: NonLifecycleKind,
        principal_id: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> NonLifecycleRegistryOutcome:
        self._called("resolve_non_lifecycle")
        if (principal_id, idempotency_key) in self.claims:
            return ConflictClaim()
        if (principal_id, idempotency_key) in self.tombstoned_claims:
            return ConflictClaim()
        previous = self.ledger.get((principal_id, idempotency_key))
        if previous is None:
            return NewClaim()
        if previous[0] == fingerprint:
            return CompletedNonLifecycleClaim(previous[1], deepcopy(previous[2]))
        return ConflictClaim()

    def recovery_state(self, principal_id: str, service_id: UUID) -> RecoveryState:
        self._called("recovery_state")
        return self.recovery_states.get((principal_id, service_id), RecoveryState.CLEAR)

    def complete_non_lifecycle(
        self,
        kind: NonLifecycleKind,
        principal_id: str,
        idempotency_key: str,
        fingerprint: str,
        service: Service,
    ) -> None:
        self._called("complete_non_lifecycle")
        if service.principal_id != principal_id:
            raise RuntimeError(BACKEND_SENTINEL)
        if (principal_id, idempotency_key) in self.claims or (
            principal_id,
            idempotency_key,
        ) in self.tombstoned_claims:
            raise RuntimeError(BACKEND_SENTINEL)
        self.ledger[(principal_id, idempotency_key)] = (fingerprint, kind, deepcopy(service))

    def append(self, event: AuditEvent) -> None:
        self._called("append")
        self.events.append(deepcopy(event))

    def resolve_or_prepare(self, command: RegistryAuthorityCommand) -> RegistryAuthorityOutcome:
        self._called("resolve_or_prepare")
        intent = prepare_registry_intent(command)
        key = (intent.principal_id, intent.idempotency_key)
        tombstoned = self.tombstoned_claims.get(key)
        if tombstoned is not None:
            if (
                tombstoned.fingerprint == intent.fingerprint
                and tombstoned.operation_kind == intent.kind.value
                and tombstoned.service_id == intent.service_id
            ):
                return deepcopy(tombstoned)
            return RegistryClaimConflict()
        existing = self.claims.get(key)
        if existing is not None:
            if _same_intent(existing.intent, intent):
                return deepcopy(existing)
            return RegistryClaimConflict()
        if key in self.ledger:
            return RegistryClaimConflict()
        if type(intent) is PortableIntent and intent.kind is PortableOperationKind.IMPORT:
            collision = self._import_collision(intent.identity)
            if collision is not None:
                return collision
            prepared = PreparedRegistryClaim(intent)
            self.claims[key] = prepared
            assert intent.identity is not None
            self.import_reservations[intent.service_id] = intent.identity
            return prepared

        service = self.items.get(intent.service_id)
        if service is None or service.principal_id != intent.principal_id:
            return RegistryStaleRevision()
        expected_revision = intent.expected_service_revision
        if expected_revision is None or service.service_revision != expected_revision:
            return RegistryStaleRevision()
        if type(intent) is PortableIntent and intent.kind is PortableOperationKind.EXPORT:
            if service.design_state == "materialised" and service.operational_status == "active":
                return RegistryUnsupportedTransition()
        if (
            type(intent) is PortableIntent
            and intent.kind is PortableOperationKind.PURGE
            and service.operational_status != "archived"
        ):
            return RegistryUnsupportedTransition()
        blocker = self._active_blocker(intent.service_id)
        if blocker is not None:
            if type(blocker) is RecoveryRequiredRegistryClaim:
                return blocker
            return RegistryOperationInProgress(blocker.intent)
        if type(intent) is OperationalLifecycleIntent:
            try:
                transition_target_status(command, service.design_state, service.operational_status)  # type: ignore[arg-type]
            except Exception:
                return RegistryUnsupportedTransition()
        if type(intent) is PortableIntent and intent.kind is PortableOperationKind.PURGE:
            assert intent.purge_authority is not None
            if intent.purge_authority.kind is PurgeAuthorityKind.VERIFIED_RECEIPT:
                receipt = self.receipts.get(intent.purge_authority.receipt_id)
                if (
                    receipt is None
                    or receipt.kind is not ReceiptKind.EXPORT
                    or receipt.verification is not ReceiptVerification.VERIFIED
                    or receipt.principal_id != intent.principal_id
                    or receipt.service_id != intent.service_id
                    or receipt.service_revision != intent.expected_service_revision
                    or receipt.operational_status != "archived"
                ):
                    return RegistryUnsupportedTransition()
        prepared = PreparedRegistryClaim(intent)
        self.claims[key] = prepared
        return prepared

    def finalize_operational(
        self, prepared: RegistryAuthorityIntent, at: datetime
    ) -> RegistryAuthorityOutcome:
        self._called("finalize_operational")
        if type(prepared) is not OperationalLifecycleIntent:
            raise RuntimeError(BACKEND_SENTINEL)
        existing = self._prepared_or_terminal(prepared)
        if type(existing) is not PreparedRegistryClaim:
            return existing
        service = self.items.get(prepared.service_id)
        try:
            if (
                service is None
                or service.principal_id != prepared.principal_id
                or service.service_revision != prepared.expected_service_revision
                or at < service.updated_at
            ):
                raise ValueError
            target = transition_target_status(
                _command_from_operational_intent(prepared),
                service.design_state,
                service.operational_status,
            )
        except Exception:
            return self._mark_recovery(prepared, at)
        result = replace(
            service,
            operational_status=target,
            updated_at=at,
            last_activity_at=at,
            service_revision=service.service_revision + 1,
        )
        self.items[result.service_id] = deepcopy(result)
        terminal = prepared.with_phase(OperationalLifecyclePhase.COMPLETED)
        completed = CompletedRegistryClaim(terminal, result)
        self.claims[(prepared.principal_id, prepared.idempotency_key)] = completed
        self._local_event(terminal, "completed", at)
        return completed

    def complete_export(
        self, prepared: RegistryAuthorityIntent, receipt: TransferReceipt
    ) -> RegistryAuthorityOutcome:
        self._called("complete_export")
        if type(prepared) is not PortableIntent or prepared.kind is not PortableOperationKind.EXPORT:
            raise RuntimeError(BACKEND_SENTINEL)
        existing = self._prepared_or_terminal(prepared)
        if type(existing) is not PreparedRegistryClaim:
            return existing
        service = self.items.get(prepared.service_id)
        if (
            service is None
            or service.principal_id != prepared.principal_id
            or service.service_revision != prepared.expected_service_revision
            or receipt.principal_id != prepared.principal_id
            or receipt.service_id != prepared.service_id
            or receipt.operational_status != service.operational_status
        ):
            return self._mark_recovery(prepared, receipt.created_at)
        if (
            receipt.kind is not ReceiptKind.EXPORT
            or receipt.verification is not ReceiptVerification.VERIFIED
            or receipt.service_revision != prepared.expected_service_revision
        ):
            return self._mark_recovery(prepared, receipt.created_at)
        terminal = prepared.with_phase(OperationalLifecyclePhase.COMPLETED)
        completed = CompletedRegistryClaim(terminal, receipt)
        self.claims[(prepared.principal_id, prepared.idempotency_key)] = completed
        self.receipts[receipt.receipt_id] = deepcopy(receipt)
        self._local_event(terminal, "completed", receipt.created_at)
        return completed

    def publish_import(
        self, prepared: RegistryAuthorityIntent, service: Service, receipt: TransferReceipt
    ) -> RegistryAuthorityOutcome:
        self._called("publish_import")
        if type(prepared) is not PortableIntent or prepared.kind is not PortableOperationKind.IMPORT:
            raise RuntimeError(BACKEND_SENTINEL)
        existing = self._prepared_or_terminal(prepared)
        if type(existing) is not PreparedRegistryClaim:
            return existing
        identity = prepared.identity
        if (
            identity is None
            or service.service_id != identity.service_id
            or service.principal_id != identity.principal_id
            or service.domain_name != identity.domain_name
            or (service.storage is not None and service.storage.backend != prepared.destination_backend.value)
            or prepared.service_id not in self.import_reservations
            or self.items.get(service.service_id) is not None
            or receipt.kind is not ReceiptKind.IMPORT
            or receipt.principal_id != identity.principal_id
            or receipt.service_id != identity.service_id
            or receipt.bundle_root_sha256 != prepared.bundle_root_sha256
            or receipt.storage_backend is not prepared.destination_backend
            or receipt.service_revision != service.service_revision
            or receipt.operational_status != service.operational_status
            or receipt.verification is not ReceiptVerification.VERIFIED
        ):
            return self._mark_recovery(prepared, receipt.created_at)
        self.items[service.service_id] = deepcopy(service)
        self.import_reservations.pop(service.service_id)
        self.receipts[receipt.receipt_id] = deepcopy(receipt)
        terminal = prepared.with_phase(OperationalLifecyclePhase.COMPLETED)
        completed = CompletedRegistryClaim(terminal, service)
        self.claims[(prepared.principal_id, prepared.idempotency_key)] = completed
        self._local_event(terminal, "completed", receipt.created_at)
        return completed

    def finalize_purge(
        self, prepared: RegistryAuthorityIntent, at: datetime
    ) -> RegistryAuthorityOutcome:
        self._called("finalize_purge")
        if type(prepared) is not PortableIntent or prepared.kind is not PortableOperationKind.PURGE:
            raise RuntimeError(BACKEND_SENTINEL)
        existing = self._prepared_or_terminal(prepared)
        if type(existing) is not PreparedRegistryClaim:
            return existing
        service = self.items.get(prepared.service_id)
        if (
            service is None
            or service.principal_id != prepared.principal_id
            or service.service_revision != prepared.expected_service_revision
            or at < service.updated_at
            or service.operational_status != "archived"
        ):
            return self._mark_recovery(prepared, at)
        assert prepared.purge_authority is not None
        if prepared.purge_authority.kind is PurgeAuthorityKind.VERIFIED_RECEIPT:
            receipt = self.receipts.get(prepared.purge_authority.receipt_id)
            if (
                receipt is None
                or receipt.kind is not ReceiptKind.EXPORT
                or receipt.verification is not ReceiptVerification.VERIFIED
                or receipt.principal_id != prepared.principal_id
                or receipt.service_id != prepared.service_id
                or receipt.service_revision != prepared.expected_service_revision
                or receipt.operational_status != "archived"
            ):
                return self._mark_recovery(prepared, at)
        tombstone = PurgeTombstone(
            service.principal_id,
            service.service_id,
            f"svc_{service.service_id.hex}",
            prepared.created_via,
            at,
            prepared.purge_authority,
            prepared.purge_authority.receipt_id,
        )
        self.tombstones[service.service_id] = tombstone
        self._scrub_prior_service_claims(service.service_id, (prepared.principal_id, prepared.idempotency_key))
        del self.items[service.service_id]
        terminal = prepared.with_phase(OperationalLifecyclePhase.COMPLETED)
        completed = CompletedRegistryClaim(terminal, tombstone)
        self.claims[(prepared.principal_id, prepared.idempotency_key)] = completed
        self._local_event(terminal, "completed", at)
        return completed

    def mark_recovery_required(
        self, prepared: RegistryAuthorityIntent, at: datetime
    ) -> RecoveryRequiredRegistryClaim:
        self._called("mark_recovery_required")
        return self._mark_recovery(prepared, at)

    def _prepared_or_terminal(
        self, prepared: RegistryAuthorityIntent
    ) -> PreparedRegistryClaim | CompletedRegistryClaim | RecoveryRequiredRegistryClaim:
        stored = self.claims.get((prepared.principal_id, prepared.idempotency_key))
        if stored is None or not _same_intent(stored.intent, prepared):
            raise RuntimeError(BACKEND_SENTINEL)
        return stored

    def _mark_recovery(
        self, prepared: RegistryAuthorityIntent, at: datetime
    ) -> RecoveryRequiredRegistryClaim:
        terminal = prepared.with_phase(OperationalLifecyclePhase.RECOVERY_REQUIRED)
        recovered = RecoveryRequiredRegistryClaim(terminal)
        self.claims[(prepared.principal_id, prepared.idempotency_key)] = recovered
        self._local_event(terminal, "recovery_required", at)
        return recovered

    def _active_blocker(
        self, service_id: UUID
    ) -> PreparedRegistryClaim | RecoveryRequiredRegistryClaim | None:
        for claim in self.claims.values():
            if (
                claim.intent.service_id == service_id
                and claim.intent.phase in {
                    OperationalLifecyclePhase.PREPARED,
                    OperationalLifecyclePhase.RECOVERY_REQUIRED,
                }
            ):
                return claim  # type: ignore[return-value]
        return None

    def _import_collision(self, identity: ImportIdentity | None) -> RegistryIdentityCollision | None:
        if identity is None:
            raise RuntimeError(BACKEND_SENTINEL)
        if identity.service_id in self.tombstones:
            return RegistryIdentityCollision(IdentityCollisionKind.TOMBSTONE)
        if identity.service_id in self.items or identity.service_id in self.import_reservations:
            return RegistryIdentityCollision(IdentityCollisionKind.SERVICE_ID)
        for service in self.items.values():
            if service.principal_id == identity.principal_id and service.domain_name == identity.domain_name:
                return RegistryIdentityCollision(IdentityCollisionKind.DOMAIN)
            if service.storage is not None and service.storage.namespace == identity.logical_namespace:
                return RegistryIdentityCollision(IdentityCollisionKind.NAMESPACE)
        for reservation in self.import_reservations.values():
            if reservation.principal_id == identity.principal_id and reservation.domain_name == identity.domain_name:
                return RegistryIdentityCollision(IdentityCollisionKind.DOMAIN)
            if reservation.logical_namespace == identity.logical_namespace:
                return RegistryIdentityCollision(IdentityCollisionKind.NAMESPACE)
        return None

    def _scrub_prior_service_claims(
        self, service_id: UUID, purge_key: tuple[str, str]
    ) -> None:
        for key, claim in tuple(self.claims.items()):
            if key == purge_key or claim.intent.service_id != service_id:
                continue
            self.tombstoned_claims[key] = TombstonedRegistryClaim(
                key[1], claim.intent.fingerprint, claim.intent.kind.value, service_id
            )
            del self.claims[key]
        for key, (fingerprint, kind, service) in tuple(self.ledger.items()):
            if service.service_id != service_id:
                continue
            self.tombstoned_claims[key] = TombstonedRegistryClaim(
                key[1], fingerprint, kind, service_id
            )
            del self.ledger[key]

    def _local_event(self, intent: RegistryAuthorityIntent, outcome: str, at: datetime) -> None:
        self.local_events.append(
            LocalRegistryEvent(
                intent.kind.value,
                outcome,
                intent.service_id,
                intent.principal_id,
                intent.created_via,
                at,
            )
        )
        retained: list[LocalRegistryEvent] = []
        counts: dict[str, int] = {}
        for event in reversed(self.local_events):
            count = counts.get(event.principal_id, 0)
            if count < 1000:
                retained.append(event)
                counts[event.principal_id] = count + 1
        self.local_events = list(reversed(retained))

    def commit(self) -> None:
        self._called("commit")
        self.state.items = deepcopy(self.items)
        self.state.ledger = deepcopy(self.ledger)
        self.state.events = deepcopy(self.events)
        self.state.claims = deepcopy(self.claims)
        self.state.tombstoned_claims = deepcopy(self.tombstoned_claims)
        self.state.import_reservations = deepcopy(self.import_reservations)
        self.state.receipts = deepcopy(self.receipts)
        self.state.tombstones = deepcopy(self.tombstones)
        self.state.local_events = deepcopy(self.local_events)
        self.state.recovery_states = deepcopy(self.recovery_states)
        self.state.commits += 1
        self.terminal_action = "committed"

    def rollback(self) -> None:
        self._called("rollback")
        if self.state.rollback_raises:
            raise RuntimeError(self.state.error_sentinel)
        self.terminal_action = "rolled_back"

    def close(self) -> None:
        self.close_count += 1
        self._called("close")
        if self.terminal_action is None:
            self.terminal_action = "rolled_back"
        self.closed = True

    def _called(self, name: str) -> None:
        if self.closed:
            raise RuntimeError(BACKEND_SENTINEL)
        if self.terminal_action is not None and name != "close":
            raise RuntimeError(BACKEND_SENTINEL)
        self.calls.append(name)
        if self.state.fail_at == name:
            raise RuntimeError(self.state.error_sentinel)


def _same_intent(left: RegistryAuthorityIntent, right: RegistryAuthorityIntent) -> bool:
    return (
        type(left) is type(right)
        and left.kind == right.kind
        and left.fingerprint == right.fingerprint
    )


def _command_from_operational_intent(intent: OperationalLifecycleIntent):
    from aipcs_mcp.application.operational_lifecycle import (
        ArchiveCommand,
        RestoreCommand,
        ResumeCommand,
        SuspendCommand,
    )

    fields = {
        "principal_id": intent.principal_id,
        "created_via": intent.created_via,
        "service_id": intent.service_id,
        "expected_service_revision": intent.expected_service_revision,
        "idempotency_key": intent.idempotency_key,
    }
    return {
        "suspend": SuspendCommand,
        "resume": ResumeCommand,
        "archive": ArchiveCommand,
        "restore": RestoreCommand,
    }[intent.kind.value](**fields)
