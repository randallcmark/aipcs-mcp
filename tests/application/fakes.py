from __future__ import annotations

from copy import deepcopy
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
        previous = self.ledger.get((principal_id, idempotency_key))
        if previous is None:
            return NewClaim()
        if previous[0] == fingerprint:
            return CompletedNonLifecycleClaim(previous[1], deepcopy(previous[2]))
        return ConflictClaim()

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
        self.ledger[(principal_id, idempotency_key)] = (fingerprint, kind, deepcopy(service))

    def append(self, event: AuditEvent) -> None:
        self._called("append")
        self.events.append(deepcopy(event))

    def commit(self) -> None:
        self._called("commit")
        self.state.items = deepcopy(self.items)
        self.state.ledger = deepcopy(self.ledger)
        self.state.events = deepcopy(self.events)
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
