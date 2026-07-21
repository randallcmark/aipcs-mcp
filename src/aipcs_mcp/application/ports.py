"""Narrow registry-only application ports."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from .models import AuditEvent, MutationClaim, Service


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdProvider(Protocol):
    def new_service_id(self) -> UUID: ...


class ServiceRepository(Protocol):
    """Principal-scoped service access; list order is created time then service ID."""

    def find_domain(self, principal_id: str, domain_name: str) -> Service | None: ...
    def get(self, principal_id: str, service_id: UUID) -> Service | None: ...
    def list(self, principal_id: str, limit: int) -> list[Service]: ...
    def add(self, service: Service) -> None: ...
    def save(self, service: Service) -> None: ...


class MutationLedger(Protocol):
    def claim(self, principal_id: str, key: str, fingerprint: str) -> MutationClaim: ...
    def complete(self, principal_id: str, key: str, fingerprint: str, result: Service) -> None: ...


class AuditRepository(Protocol):
    def append(self, event: AuditEvent) -> None: ...


class RegistryUnitOfWork(Protocol):
    services: ServiceRepository
    mutations: MutationLedger
    audits: AuditRepository

    def commit(self) -> None: ...
    def rollback(self) -> None: ...


UowFactory = Callable[[], RegistryUnitOfWork]
