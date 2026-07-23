"""Registry-only lifecycle use cases; deliberately not wired to MCP or CLI."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from aipcs_mcp.contracts import ServiceMetadata
from aipcs_mcp.lifecycle import MAX_SERVICE_REVISION, RecoveryState
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
    CompletedNonLifecycleClaim,
    ConflictClaim,
    DesignCommand,
    NewClaim,
    NonLifecycleKind,
    NonLifecycleRegistryOutcome,
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
_SAFE_APPLICATION_ERROR_TYPES = (
    Conflict,
    InternalFailure,
    InvalidCommand,
    InvalidState,
    NotFound,
)


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
            claim = uow.mutations.resolve_non_lifecycle(
                "seed", context.principal_id, command.idempotency_key, fingerprint
            )
            replay = self._resolve_non_lifecycle_claim(claim, "seed", context.principal_id)
            if replay is not None:
                result = self._project(
                    replay,
                    self._recovery_state(uow, context.principal_id, replay.service_id),
                )
            else:
                existing = uow.services.find_domain(context.principal_id, command.domain_name)
                if existing is not None and existing.principal_id != context.principal_id:
                    existing = None
                if existing is not None:
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
                    uow.mutations.complete_non_lifecycle(
                        "seed",
                        context.principal_id,
                        command.idempotency_key,
                        fingerprint,
                        existing,
                    )
                    result = self._project(
                        existing,
                        self._recovery_state(uow, context.principal_id, existing.service_id),
                    )
                    uow.commit()
                else:
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
                    uow.mutations.complete_non_lifecycle(
                        "seed",
                        context.principal_id,
                        command.idempotency_key,
                        fingerprint,
                        service,
                    )
                    result = self._project(
                        service,
                        self._recovery_state(uow, context.principal_id, service.service_id),
                    )
                    uow.commit()
        except Exception as error:
            failure = self._failure_after_rollback(uow, error)
        else:
            failure = self._close_failure(uow)
            if failure is None:
                return result
        raise failure from None

    def list(self, context: ApplicationContext, limit: int = 100) -> list[ServiceMetadata]:
        self._validate_context(context)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise InvalidCommand()
        uow = self._new_uow()
        try:
            items = uow.services.list(context.principal_id, limit)
            scoped = (item for item in items if item.principal_id == context.principal_id)
            ordered = sorted(scoped, key=lambda item: (item.created_at, item.service_id))[:limit]
            result = [
                self._project(
                    item,
                    self._recovery_state(uow, context.principal_id, item.service_id),
                )
                for item in ordered
            ]
        except Exception as error:
            failure = self._failure_after_close(uow, error)
        else:
            failure = self._close_failure(uow)
        if failure is not None:
            raise failure from None
        return result

    def inspect(self, context: ApplicationContext, service_id: UUID) -> ServiceMetadata:
        self._validate_context(context)
        if not isinstance(service_id, UUID) or service_id.int == 0:
            raise InvalidCommand()
        uow = self._new_uow()
        try:
            service = uow.services.get(context.principal_id, service_id)
            if service is not None and service.principal_id == context.principal_id:
                result = self._project(
                    service,
                    self._recovery_state(uow, context.principal_id, service.service_id),
                )
            else:
                result = None
        except Exception as error:
            failure = self._failure_after_close(uow, error)
        else:
            failure = self._close_failure(uow)
        if failure is not None:
            raise failure from None
        if result is None:
            raise NotFound()
        return result

    def design(self, context: ApplicationContext, command: DesignCommand) -> ServiceMetadata:
        self._validate_context(context)
        self._validate_design(command)
        command = replace(command, manifest=self._manifest_snapshot(command.manifest))
        fingerprint = _fingerprint(context, command)
        uow = self._new_uow()
        try:
            claim = uow.mutations.resolve_non_lifecycle(
                "design", context.principal_id, command.idempotency_key, fingerprint
            )
            replay = self._resolve_non_lifecycle_claim(claim, "design", context.principal_id)
            if replay is not None:
                result = self._project(
                    replay,
                    self._recovery_state(uow, context.principal_id, replay.service_id),
                )
            else:
                service = uow.services.get(context.principal_id, command.service_id)
                if service is None or service.principal_id != context.principal_id:
                    raise NotFound()
                if (
                    service.operational_status != "active"
                    or service.design_state != "seeded"
                    or service.manifest is not None
                ):
                    raise InvalidState()
                if service.service_revision >= MAX_SERVICE_REVISION:
                    raise InternalFailure()

                now = self._now()
                if now < service.updated_at:
                    raise InternalFailure()
                changed = replace(
                    service,
                    manifest=command.manifest.model_copy(deep=True),
                    schema_version=1,
                    updated_at=now,
                    last_activity_at=now,
                    service_revision=service.service_revision + 1,
                )
                save_result = uow.services.save(changed, service.service_revision)
                if save_result == "stale_revision":
                    self._reread_after_stale_design(uow, context.principal_id, command.service_id)
                if save_result != "saved":
                    raise InternalFailure()
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
                uow.mutations.complete_non_lifecycle(
                    "design",
                    context.principal_id,
                    command.idempotency_key,
                    fingerprint,
                    changed,
                )
                result = self._project(
                    changed,
                    self._recovery_state(uow, context.principal_id, changed.service_id),
                )
                uow.commit()
        except Exception as error:
            failure = self._failure_after_rollback(uow, error)
        else:
            failure = self._close_failure(uow)
            if failure is None:
                return result
        raise failure from None

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
            result = ManifestV2.model_validate(payload)
        except Exception:
            failure = InvalidCommand()
        else:
            return result
        raise failure from None

    def _new_uow(self) -> RegistryUnitOfWork:
        try:
            result = self._uows()
        except Exception:
            failure = InternalFailure()
        else:
            return result
        raise failure from None

    def _now(self) -> datetime:
        try:
            value = self._clock.now()
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise InternalFailure()
        except Exception:
            failure = InternalFailure()
        else:
            return value
        raise failure from None

    def _new_service_id(self) -> UUID:
        try:
            value = self._ids.new_service_id()
            if not isinstance(value, UUID) or value.int == 0:
                raise InternalFailure()
        except Exception:
            failure = InternalFailure()
        else:
            return value
        raise failure from None

    @staticmethod
    def _resolve_non_lifecycle_claim(
        claim: NonLifecycleRegistryOutcome,
        kind: NonLifecycleKind,
        principal_id: str,
    ) -> Service | None:
        if type(claim) is NewClaim:
            return None
        if type(claim) is CompletedNonLifecycleClaim:
            if claim.operation_kind not in {"legacy", kind} or claim.service.principal_id != principal_id:
                raise InternalFailure()
            return claim.service
        if type(claim) is ConflictClaim:
            raise Conflict()
        raise InternalFailure()

    @staticmethod
    def _reread_after_stale_design(
        uow: RegistryUnitOfWork, principal_id: str, service_id: UUID
    ) -> None:
        """Never retry a zero-row CAS update; re-read before returning current semantics."""

        current = uow.services.get(principal_id, service_id)
        if current is None or current.principal_id != principal_id:
            raise NotFound()
        raise InvalidState()

    @staticmethod
    def _project(service: Service, recovery_state: RecoveryState) -> ServiceMetadata:
        try:
            result = project(service, recovery_state)
        except Exception:
            failure = InternalFailure()
        else:
            return result
        raise failure from None

    @staticmethod
    def _recovery_state(
        uow: RegistryUnitOfWork, principal_id: str, service_id: UUID
    ) -> RecoveryState:
        try:
            return uow.mutations.recovery_state(principal_id, service_id)
        except Exception:
            raise InternalFailure() from None

    @staticmethod
    def _close_failure(uow: RegistryUnitOfWork) -> InternalFailure | None:
        try:
            uow.close()
        except Exception:
            return InternalFailure()
        return None

    @classmethod
    def _failure_after_close(
        cls, uow: RegistryUnitOfWork, error: Exception
    ) -> ApplicationError:
        with suppress(Exception):
            uow.close()
        return cls._safe_application_error(error)

    @classmethod
    def _failure_after_rollback(
        cls, uow: RegistryUnitOfWork, error: Exception
    ) -> ApplicationError:
        with suppress(Exception):
            uow.rollback()
        with suppress(Exception):
            uow.close()
        return cls._safe_application_error(error)

    @staticmethod
    def _safe_application_error(error: Exception) -> ApplicationError:
        for error_type in _SAFE_APPLICATION_ERROR_TYPES:
            if type(error) is error_type:
                return error_type()
        return InternalFailure()


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
