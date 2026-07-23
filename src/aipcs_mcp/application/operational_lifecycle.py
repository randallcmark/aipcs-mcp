"""Pure operational-service lifecycle policy, independent of transport and storage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal
from uuid import UUID

from aipcs_mcp.lifecycle import (
    MAX_CREATED_VIA_LENGTH,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MAX_PRINCIPAL_LENGTH,
    MAX_SERVICE_REVISION,
)

OperationalStatus = Literal["active", "suspended", "archived"]
DesignState = Literal["seeded", "materialised"]


class OperationalLifecycleContractError(ValueError):
    """A malformed operational-lifecycle policy value."""


class OperationalLifecycleKind(StrEnum):
    SUSPEND = "suspend"
    RESUME = "resume"
    ARCHIVE = "archive"
    RESTORE = "restore"


class OperationalLifecyclePhase(StrEnum):
    PREPARED = "prepared"
    COMPLETED = "completed"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True, slots=True)
class SuspendCommand:
    principal_id: str
    created_via: str
    service_id: UUID
    expected_service_revision: int
    idempotency_key: str
    kind: OperationalLifecycleKind = field(default=OperationalLifecycleKind.SUSPEND, init=False)

    def __post_init__(self) -> None:
        _validate_command_fields(
            self.principal_id,
            self.created_via,
            self.service_id,
            self.expected_service_revision,
            self.idempotency_key,
        )


@dataclass(frozen=True, slots=True)
class ResumeCommand:
    principal_id: str
    created_via: str
    service_id: UUID
    expected_service_revision: int
    idempotency_key: str
    kind: OperationalLifecycleKind = field(default=OperationalLifecycleKind.RESUME, init=False)

    def __post_init__(self) -> None:
        _validate_command_fields(
            self.principal_id,
            self.created_via,
            self.service_id,
            self.expected_service_revision,
            self.idempotency_key,
        )


@dataclass(frozen=True, slots=True)
class ArchiveCommand:
    principal_id: str
    created_via: str
    service_id: UUID
    expected_service_revision: int
    idempotency_key: str
    kind: OperationalLifecycleKind = field(default=OperationalLifecycleKind.ARCHIVE, init=False)

    def __post_init__(self) -> None:
        _validate_command_fields(
            self.principal_id,
            self.created_via,
            self.service_id,
            self.expected_service_revision,
            self.idempotency_key,
        )


@dataclass(frozen=True, slots=True)
class RestoreCommand:
    principal_id: str
    created_via: str
    service_id: UUID
    expected_service_revision: int
    idempotency_key: str
    kind: OperationalLifecycleKind = field(default=OperationalLifecycleKind.RESTORE, init=False)

    def __post_init__(self) -> None:
        _validate_command_fields(
            self.principal_id,
            self.created_via,
            self.service_id,
            self.expected_service_revision,
            self.idempotency_key,
        )


type OperationalLifecycleCommand = SuspendCommand | ResumeCommand | ArchiveCommand | RestoreCommand


@dataclass(frozen=True, slots=True)
class OperationalLifecycleIntent:
    """Immutable prepared or terminal evidence for an operational state change."""

    kind: OperationalLifecycleKind
    phase: OperationalLifecyclePhase
    principal_id: str
    created_via: str
    service_id: UUID
    expected_service_revision: int
    target_operational_status: OperationalStatus
    idempotency_key: str
    fingerprint: str

    def __post_init__(self) -> None:
        _validate_kind(self.kind)
        _validate_phase(self.phase)
        _validate_command_fields(
            self.principal_id,
            self.created_via,
            self.service_id,
            self.expected_service_revision,
            self.idempotency_key,
        )
        if (
            type(self.target_operational_status) is not str
            or self.target_operational_status != deterministic_target_status(self.kind)
        ):
            raise OperationalLifecycleContractError(
                "Operational lifecycle target status is invalid."
            )
        _validate_fingerprint(self.fingerprint)
        if self.fingerprint != operational_lifecycle_fingerprint(_command_for_intent(self)):
            raise OperationalLifecycleContractError(
                "Operational lifecycle fingerprint does not match the admitted request."
            )

    def with_phase(self, phase: OperationalLifecyclePhase) -> OperationalLifecycleIntent:
        """Finalize a prepared intent once, without mutating durable evidence."""

        _validate_phase(phase)
        if self.phase is not OperationalLifecyclePhase.PREPARED or phase not in {
            OperationalLifecyclePhase.COMPLETED,
            OperationalLifecyclePhase.RECOVERY_REQUIRED,
        }:
            raise OperationalLifecycleContractError(
                "Operational lifecycle intent phase transition is not monotonic."
            )
        return OperationalLifecycleIntent(
            kind=self.kind,
            phase=phase,
            principal_id=self.principal_id,
            created_via=self.created_via,
            service_id=self.service_id,
            expected_service_revision=self.expected_service_revision,
            target_operational_status=self.target_operational_status,
            idempotency_key=self.idempotency_key,
            fingerprint=self.fingerprint,
        )


def prepare_operational_intent(command: OperationalLifecycleCommand) -> OperationalLifecycleIntent:
    """Bind one validated operational command to immutable prepared evidence."""

    _validate_command(command)
    return OperationalLifecycleIntent(
        kind=command.kind,
        phase=OperationalLifecyclePhase.PREPARED,
        principal_id=command.principal_id,
        created_via=command.created_via,
        service_id=command.service_id,
        expected_service_revision=command.expected_service_revision,
        target_operational_status=deterministic_target_status(command.kind),
        idempotency_key=command.idempotency_key,
        fingerprint=operational_lifecycle_fingerprint(command),
    )


def deterministic_target_status(kind: OperationalLifecycleKind) -> OperationalStatus:
    """Return the status determined by a valid operational action alone."""

    _validate_kind(kind)
    return {
        OperationalLifecycleKind.SUSPEND: "suspended",
        OperationalLifecycleKind.RESUME: "active",
        OperationalLifecycleKind.ARCHIVE: "archived",
        OperationalLifecycleKind.RESTORE: "suspended",
    }[kind]


def transition_target_status(
    command: OperationalLifecycleCommand,
    design_state: DesignState,
    current_operational_status: OperationalStatus,
) -> OperationalStatus:
    """Validate the frozen C0 transition matrix and return its target status."""

    _validate_command(command)
    if design_state not in {"seeded", "materialised"}:
        raise OperationalLifecycleContractError("Service design state is invalid.")
    if current_operational_status not in {"active", "suspended", "archived"}:
        raise OperationalLifecycleContractError("Service operational status is invalid.")
    allowed_sources = {
        OperationalLifecycleKind.SUSPEND: {"active"},
        OperationalLifecycleKind.RESUME: {"suspended"},
        OperationalLifecycleKind.ARCHIVE: {"active", "suspended"},
        OperationalLifecycleKind.RESTORE: {"archived"},
    }
    if current_operational_status not in allowed_sources[command.kind]:
        raise OperationalLifecycleContractError("Operational lifecycle transition is unsupported.")
    return deterministic_target_status(command.kind)


def canonical_operational_lifecycle_payload(command: OperationalLifecycleCommand) -> dict[str, object]:
    """Return the exact logical payload covered by the durable request fingerprint."""

    _validate_command(command)
    return {
        "kind": command.kind.value,
        "principal": command.principal_id,
        "created_via": command.created_via,
        "service_id": str(command.service_id),
        "expected_service_revision": command.expected_service_revision,
        "target_operational_status": deterministic_target_status(command.kind),
    }


def canonical_operational_lifecycle_json(command: OperationalLifecycleCommand) -> str:
    """Return compact ASCII JSON used before SHA-256 hashing."""

    return json.dumps(
        canonical_operational_lifecycle_payload(command),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def operational_lifecycle_fingerprint(command: OperationalLifecycleCommand) -> str:
    """Compute the canonical lowercase SHA-256 fingerprint for one command."""

    return hashlib.sha256(canonical_operational_lifecycle_json(command).encode("utf-8")).hexdigest()


def _command_for_intent(intent: OperationalLifecycleIntent) -> OperationalLifecycleCommand:
    fields = {
        "principal_id": intent.principal_id,
        "created_via": intent.created_via,
        "service_id": intent.service_id,
        "expected_service_revision": intent.expected_service_revision,
        "idempotency_key": intent.idempotency_key,
    }
    command_types = {
        OperationalLifecycleKind.SUSPEND: SuspendCommand,
        OperationalLifecycleKind.RESUME: ResumeCommand,
        OperationalLifecycleKind.ARCHIVE: ArchiveCommand,
        OperationalLifecycleKind.RESTORE: RestoreCommand,
    }
    return command_types[intent.kind](**fields)


def _validate_command(command: object) -> None:
    if type(command) not in {SuspendCommand, ResumeCommand, ArchiveCommand, RestoreCommand}:
        raise OperationalLifecycleContractError("Operational lifecycle command type is invalid.")


def _validate_command_fields(
    principal_id: str,
    created_via: str,
    service_id: UUID,
    expected_service_revision: int,
    idempotency_key: str,
) -> None:
    if not _valid_text(principal_id, MAX_PRINCIPAL_LENGTH):
        raise OperationalLifecycleContractError("Principal id is invalid.")
    if not _valid_text(created_via, MAX_CREATED_VIA_LENGTH):
        raise OperationalLifecycleContractError("Created-via value is invalid.")
    if type(service_id) is not UUID or service_id.int == 0:
        raise OperationalLifecycleContractError("Service id is invalid.")
    if (
        type(expected_service_revision) is not int
        or not 1 <= expected_service_revision < MAX_SERVICE_REVISION
    ):
        raise OperationalLifecycleContractError("Expected service revision is invalid.")
    if not _valid_text(idempotency_key, MAX_IDEMPOTENCY_KEY_LENGTH):
        raise OperationalLifecycleContractError("Idempotency key is invalid.")


def _validate_kind(value: OperationalLifecycleKind) -> None:
    if type(value) is not OperationalLifecycleKind:
        raise OperationalLifecycleContractError("Operational lifecycle kind is invalid.")


def _validate_phase(value: OperationalLifecyclePhase) -> None:
    if type(value) is not OperationalLifecyclePhase:
        raise OperationalLifecycleContractError("Operational lifecycle phase is invalid.")


def _validate_fingerprint(value: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise OperationalLifecycleContractError("Operational lifecycle fingerprint is invalid.")


def _valid_text(value: str, maximum: int) -> bool:
    return type(value) is str and bool(value) and value == value.strip() and len(value) <= maximum
