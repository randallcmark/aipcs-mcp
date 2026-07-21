"""Registry-only lifecycle use cases; deliberately not wired to MCP or CLI."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Never
from uuid import UUID

from aipcs_mcp.contracts import ServiceMetadata
from aipcs_mcp.manifest_v2 import ManifestV2

from .errors import (
    ApplicationError,
    Conflict,
    InternalFailure,
    InvalidCommand,
    InvalidState,
    NotFound,
)
from .models import (
    ApplicationContext,
    AuditEvent,
    DesignCommand,
    MutationClaim,
    SeedCommand,
    Service,
    project,
)
from .ports import Clock, IdProvider, RegistryUnitOfWork, UowFactory

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_MAX_PRINCIPAL_LENGTH = 128
_MAX_CREATED_VIA_LENGTH = 64
_MAX_IDEMPOTENCY_KEY_LENGTH = 128
_MAX_DOMAIN_CLASS_LENGTH = 64
_MAX_INTENT_LENGTH = 1_000


class ServiceApplication:
    """Own initial service lifecycle policy independently of transport and storage."""

    def __init__(self, uows: UowFactory, clock: Clock, ids: IdProvider) -> None:
        self._uows, self._clock, self._ids = uows, clock, ids

    def seed(self, context: ApplicationContext, command: SeedCommand) -> ServiceMetadata:
        self._validate_context(context)
        self._validate_seed(command)
        fingerprint = _fingerprint(context, command)
        uow = self._new_uow()
        try:
            claim = uow.mutations.claim(context.principal_id, command.idempotency_key, fingerprint)
            replay = self._resolve_claim(claim, context.principal_id)
            if replay is not None:
                return self._project(replay)

            existing = uow.services.find_domain(context.principal_id, command.domain_name)
            if existing is not None and existing.principal_id != context.principal_id:
                existing = None
            if existing is not None:
                result = self._project(existing)
                now = self._now()
                uow.audits.append(
                    AuditEvent(
                        action="seed",
                        outcome="duplicate",
                        service_id=existing.service_id,
                        principal_id=context.principal_id,
                        created_via=context.created_via,
                        at=now,
                    )
                )
                uow.mutations.complete(
                    context.principal_id,
                    command.idempotency_key,
                    fingerprint,
                    existing,
                )
                uow.commit()
                return result

            now = self._now()
            service = Service(
                service_id=self._new_service_id(),
                principal_id=context.principal_id,
                domain_name=command.domain_name,
                domain_class=command.domain_class,
                intent_description=command.intent_description,
                created_at=now,
                updated_at=now,
                last_activity_at=now,
            )
            result = self._project(service)
            uow.services.add(service)
            uow.audits.append(
                AuditEvent(
                    action="seed",
                    outcome="created",
                    service_id=service.service_id,
                    principal_id=context.principal_id,
                    created_via=context.created_via,
                    at=now,
                )
            )
            uow.mutations.complete(
                context.principal_id,
                command.idempotency_key,
                fingerprint,
                service,
            )
            uow.commit()
            return result
        except Exception as error:
            self._raise_after_rollback(uow, error)

    def list(self, context: ApplicationContext, limit: int = 100) -> list[ServiceMetadata]:
        self._validate_context(context)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise InvalidCommand()
        try:
            items = self._new_uow().services.list(context.principal_id, limit)
        except Exception as error:
            self._raise_internal(error)
        try:
            scoped = (item for item in items if item.principal_id == context.principal_id)
            ordered = sorted(scoped, key=lambda item: (item.created_at, item.service_id))[:limit]
            return [self._project(item) for item in ordered]
        except ApplicationError:
            raise
        except Exception as error:
            self._raise_internal(error)

    def inspect(self, context: ApplicationContext, service_id: UUID) -> ServiceMetadata:
        self._validate_context(context)
        if not isinstance(service_id, UUID) or service_id.int == 0:
            raise InvalidCommand()
        try:
            service = self._new_uow().services.get(context.principal_id, service_id)
        except Exception as error:
            self._raise_internal(error)
        if service is None or service.principal_id != context.principal_id:
            raise NotFound()
        return self._project(service)

    def design(self, context: ApplicationContext, command: DesignCommand) -> ServiceMetadata:
        self._validate_context(context)
        self._validate_design(command)
        command = replace(command, manifest=self._manifest_snapshot(command.manifest))
        fingerprint = _fingerprint(context, command)
        uow = self._new_uow()
        try:
            claim = uow.mutations.claim(context.principal_id, command.idempotency_key, fingerprint)
            replay = self._resolve_claim(claim, context.principal_id)
            if replay is not None:
                return self._project(replay)

            service = uow.services.get(context.principal_id, command.service_id)
            if service is None or service.principal_id != context.principal_id:
                raise NotFound()
            if (
                service.operational_status != "active"
                or service.design_state != "seeded"
                or service.manifest is not None
            ):
                raise InvalidState()

            now = self._now()
            if now < service.updated_at:
                raise InternalFailure()
            changed = replace(
                service,
                manifest=command.manifest.model_copy(deep=True),
                schema_version=1,
                updated_at=now,
                last_activity_at=now,
            )
            result = self._project(changed)
            uow.services.save(changed)
            uow.audits.append(
                AuditEvent(
                    action="design",
                    outcome="accepted",
                    service_id=changed.service_id,
                    principal_id=context.principal_id,
                    created_via=context.created_via,
                    at=now,
                )
            )
            uow.mutations.complete(
                context.principal_id,
                command.idempotency_key,
                fingerprint,
                changed,
            )
            uow.commit()
            return result
        except Exception as error:
            self._raise_after_rollback(uow, error)

    def _validate_context(self, context: ApplicationContext) -> None:
        if (
            not isinstance(context, ApplicationContext)
            or not self._valid_text(context.principal_id, _MAX_PRINCIPAL_LENGTH)
            or not self._valid_text(context.created_via, _MAX_CREATED_VIA_LENGTH)
        ):
            raise InvalidCommand()

    def _validate_seed(self, command: SeedCommand) -> None:
        if (
            not isinstance(command, SeedCommand)
            or not isinstance(command.domain_name, str)
            or not _NAME.fullmatch(command.domain_name)
            or not self._valid_text(command.domain_class, _MAX_DOMAIN_CLASS_LENGTH)
            or not self._valid_text(command.intent_description, _MAX_INTENT_LENGTH)
            or not self._valid_text(command.idempotency_key, _MAX_IDEMPOTENCY_KEY_LENGTH)
        ):
            raise InvalidCommand()

    def _validate_design(self, command: DesignCommand) -> None:
        if (
            not isinstance(command, DesignCommand)
            or not isinstance(command.service_id, UUID)
            or command.service_id.int == 0
            or not isinstance(command.manifest, ManifestV2)
            or not self._valid_text(command.idempotency_key, _MAX_IDEMPOTENCY_KEY_LENGTH)
            or command.manifest.schema_version != 1
            or command.manifest.migration_history
        ):
            raise InvalidCommand()

    @staticmethod
    def _valid_text(value: object, maximum: int) -> bool:
        return (
            isinstance(value, str)
            and bool(value)
            and value == value.strip()
            and len(value) <= maximum
        )

    @staticmethod
    def _manifest_snapshot(manifest: ManifestV2) -> ManifestV2:
        try:
            payload = manifest.model_dump(mode="json", by_alias=True, warnings="error")
            return ManifestV2.model_validate(payload)
        except Exception:
            raise InvalidCommand() from None

    def _new_uow(self) -> RegistryUnitOfWork:
        try:
            return self._uows()
        except Exception as error:
            self._raise_internal(error)

    def _now(self) -> datetime:
        try:
            value = self._clock.now()
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise InternalFailure()
            return value
        except Exception as error:
            self._raise_internal(error)

    def _new_service_id(self) -> UUID:
        try:
            value = self._ids.new_service_id()
            if not isinstance(value, UUID) or value.int == 0:
                raise InternalFailure()
            return value
        except Exception as error:
            self._raise_internal(error)

    @staticmethod
    def _resolve_claim(claim: MutationClaim, principal_id: str) -> Service | None:
        if claim.status == "new" and claim.result is None:
            return None
        if (
            claim.status == "replay"
            and claim.result is not None
            and claim.result.principal_id == principal_id
        ):
            return claim.result
        if claim.status == "conflict" and claim.result is None:
            raise Conflict()
        raise InternalFailure()

    @staticmethod
    def _project(service: Service) -> ServiceMetadata:
        try:
            return project(service)
        except Exception as error:
            ServiceApplication._raise_internal(error)

    @staticmethod
    def _raise_internal(_error: Exception) -> Never:
        raise InternalFailure() from None

    @classmethod
    def _raise_after_rollback(cls, uow: RegistryUnitOfWork, error: Exception) -> Never:
        with suppress(Exception):
            uow.rollback()
        if isinstance(error, ApplicationError):
            raise error from None
        cls._raise_internal(error)


def _fingerprint(context: ApplicationContext, command: Any) -> str:
    if isinstance(command, SeedCommand):
        payload = {
            "kind": "seed",
            "principal": context.principal_id,
            "created_via": context.created_via,
            "domain_name": command.domain_name,
            "domain_class": command.domain_class,
            "intent": command.intent_description,
        }
    else:
        payload = {
            "kind": "design",
            "principal": context.principal_id,
            "created_via": context.created_via,
            "service_id": str(command.service_id),
            "manifest": command.manifest.model_dump(mode="json", by_alias=True),
        }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()
