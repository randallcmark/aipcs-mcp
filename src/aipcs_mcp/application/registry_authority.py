"""Pure typed evidence for the shared registry claim namespace.

This module deliberately describes registry evidence only.  It neither opens
storage nor decides how an admitted operation reaches a service store.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from aipcs_mcp.lifecycle import (
    MAX_CREATED_VIA_LENGTH,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MAX_PRINCIPAL_LENGTH,
    MAX_SERVICE_REVISION,
    LifecycleIntent,
    LifecyclePhase,
)

from .models import Service
from .operational_lifecycle import (
    ArchiveCommand,
    OperationalLifecycleCommand,
    OperationalLifecycleIntent,
    OperationalLifecyclePhase,
    RestoreCommand,
    ResumeCommand,
    SuspendCommand,
    prepare_operational_intent,
)

_DOMAIN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NAMESPACE = re.compile(r"^svc_[0-9a-f]{32}$")


class RegistryAuthorityError(ValueError):
    """A malformed or inconsistent registry-authority value."""


class PortableOperationKind(StrEnum):
    EXPORT = "export"
    IMPORT = "import"
    PURGE = "purge"


class ReceiptVerification(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"


class ReceiptKind(StrEnum):
    EXPORT = "export"
    IMPORT = "import"


class StorageBackend(StrEnum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


class PurgeAuthorityKind(StrEnum):
    VERIFIED_RECEIPT = "verified_receipt"
    EXPLICIT_OVERRIDE = "explicit_override"


class IdentityCollisionKind(StrEnum):
    SERVICE_ID = "service_id"
    NAMESPACE = "namespace"
    DOMAIN = "domain"
    TOMBSTONE = "tombstone"


@dataclass(frozen=True, slots=True)
class ImportIdentity:
    """The exact identity imported from a portable bundle; no remapping is allowed."""

    principal_id: str
    service_id: UUID
    domain_name: str
    logical_namespace: str

    def __post_init__(self) -> None:
        _validate_principal(self.principal_id)
        _validate_service_id(self.service_id)
        if type(self.domain_name) is not str or _DOMAIN.fullmatch(self.domain_name) is None:
            raise RegistryAuthorityError("Import domain identity is invalid.")
        if (
            type(self.logical_namespace) is not str
            or _NAMESPACE.fullmatch(self.logical_namespace) is None
            or self.logical_namespace != f"svc_{self.service_id.hex}"
        ):
            raise RegistryAuthorityError("Import namespace identity is invalid.")

    def __repr__(self) -> str:
        return "ImportIdentity(<redacted>)"


@dataclass(frozen=True, slots=True)
class ExportCommand:
    principal_id: str
    created_via: str
    service_id: UUID
    expected_service_revision: int
    idempotency_key: str
    kind: PortableOperationKind = field(default=PortableOperationKind.EXPORT, init=False)

    def __post_init__(self) -> None:
        _validate_common(
            self.principal_id,
            self.created_via,
            self.service_id,
            self.expected_service_revision,
            self.idempotency_key,
        )

    def __repr__(self) -> str:
        return "ExportCommand(<redacted>)"


@dataclass(frozen=True, slots=True)
class ImportCommand:
    principal_id: str
    created_via: str
    identity: ImportIdentity
    destination_backend: StorageBackend
    bundle_root_sha256: str
    idempotency_key: str
    kind: PortableOperationKind = field(default=PortableOperationKind.IMPORT, init=False)

    def __post_init__(self) -> None:
        _validate_principal(self.principal_id)
        _validate_created_via(self.created_via)
        if type(self.identity) is not ImportIdentity or self.identity.principal_id != self.principal_id:
            raise RegistryAuthorityError("Import identity is invalid.")
        if type(self.destination_backend) is not StorageBackend:
            raise RegistryAuthorityError("Import destination backend is invalid.")
        _validate_sha256(self.bundle_root_sha256)
        _validate_idempotency_key(self.idempotency_key)

    def __repr__(self) -> str:
        return "ImportCommand(<redacted>)"


@dataclass(frozen=True, slots=True)
class PurgeAuthority:
    kind: PurgeAuthorityKind
    receipt_id: UUID | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not PurgeAuthorityKind:
            raise RegistryAuthorityError("Purge authority kind is invalid.")
        if self.kind is PurgeAuthorityKind.VERIFIED_RECEIPT:
            _validate_service_id(self.receipt_id)
        elif self.receipt_id is not None:
            raise RegistryAuthorityError("Explicit purge override cannot carry a receipt.")

    def __repr__(self) -> str:
        return "PurgeAuthority(<redacted>)"


@dataclass(frozen=True, slots=True)
class PurgeCommand:
    principal_id: str
    created_via: str
    service_id: UUID
    expected_service_revision: int
    idempotency_key: str
    authority: PurgeAuthority
    kind: PortableOperationKind = field(default=PortableOperationKind.PURGE, init=False)

    def __post_init__(self) -> None:
        _validate_common(
            self.principal_id,
            self.created_via,
            self.service_id,
            self.expected_service_revision,
            self.idempotency_key,
        )
        if type(self.authority) is not PurgeAuthority:
            raise RegistryAuthorityError("Purge authority is invalid.")

    def __repr__(self) -> str:
        return "PurgeCommand(<redacted>)"


type PortableCommand = ExportCommand | ImportCommand | PurgeCommand
type RegistryAuthorityCommand = OperationalLifecycleCommand | PortableCommand
type RegistryAuthorityIntent = OperationalLifecycleIntent | PortableIntent
type ActiveRegistryIntent = RegistryAuthorityIntent | LifecycleIntent


@dataclass(frozen=True, slots=True)
class PortableIntent:
    """Immutable shared-claim evidence for export, import, or purge."""

    kind: PortableOperationKind
    phase: OperationalLifecyclePhase
    principal_id: str
    created_via: str
    service_id: UUID
    expected_service_revision: int | None
    idempotency_key: str
    fingerprint: str
    identity: ImportIdentity | None = None
    bundle_root_sha256: str | None = None
    destination_backend: StorageBackend | None = None
    purge_authority: PurgeAuthority | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not PortableOperationKind:
            raise RegistryAuthorityError("Portable operation kind is invalid.")
        if type(self.phase) is not OperationalLifecyclePhase:
            raise RegistryAuthorityError("Portable operation phase is invalid.")
        _validate_principal(self.principal_id)
        _validate_created_via(self.created_via)
        _validate_service_id(self.service_id)
        _validate_idempotency_key(self.idempotency_key)
        _validate_sha256(self.fingerprint)
        if self.kind is PortableOperationKind.IMPORT:
            if (
                type(self.identity) is not ImportIdentity
                or self.identity.principal_id != self.principal_id
                or self.identity.service_id != self.service_id
                or self.expected_service_revision is not None
                or self.purge_authority is not None
                or self.bundle_root_sha256 is None
                or type(self.destination_backend) is not StorageBackend
            ):
                raise RegistryAuthorityError("Import intent shape is invalid.")
            _validate_sha256(self.bundle_root_sha256)
        else:
            if (
                type(self.expected_service_revision) is not int
                or not 1 <= self.expected_service_revision < MAX_SERVICE_REVISION
                or self.identity is not None
                or self.bundle_root_sha256 is not None
                or self.destination_backend is not None
            ):
                raise RegistryAuthorityError("Portable service intent shape is invalid.")
            if self.kind is PortableOperationKind.PURGE:
                if type(self.purge_authority) is not PurgeAuthority:
                    raise RegistryAuthorityError("Purge intent authority is invalid.")
            elif self.purge_authority is not None:
                raise RegistryAuthorityError("Export intent cannot carry purge authority.")
        if self.fingerprint != portable_fingerprint(_command_for_intent(self)):
            raise RegistryAuthorityError("Portable intent fingerprint does not match the admitted request.")

    def with_phase(self, phase: OperationalLifecyclePhase) -> PortableIntent:
        if (
            type(phase) is not OperationalLifecyclePhase
            or self.phase is not OperationalLifecyclePhase.PREPARED
            or phase not in {OperationalLifecyclePhase.COMPLETED, OperationalLifecyclePhase.RECOVERY_REQUIRED}
        ):
            raise RegistryAuthorityError("Portable intent phase transition is not monotonic.")
        return replace(self, phase=phase)

    def __repr__(self) -> str:
        return "PortableIntent(<redacted>)"


@dataclass(frozen=True, slots=True)
class TransferReceipt:
    receipt_id: UUID
    kind: ReceiptKind
    principal_id: str
    service_id: UUID
    bundle_root_sha256: str
    export_format_version: int
    storage_backend: StorageBackend
    service_revision: int
    operational_status: str
    verification: ReceiptVerification
    created_at: datetime

    def __post_init__(self) -> None:
        _validate_service_id(self.receipt_id)
        if type(self.kind) is not ReceiptKind:
            raise RegistryAuthorityError("Receipt kind is invalid.")
        _validate_principal(self.principal_id)
        _validate_service_id(self.service_id)
        _validate_sha256(self.bundle_root_sha256)
        if type(self.export_format_version) is not int or self.export_format_version != 1:
            raise RegistryAuthorityError("Receipt export format is invalid.")
        if type(self.service_revision) is not int or not 1 <= self.service_revision <= MAX_SERVICE_REVISION:
            raise RegistryAuthorityError("Receipt service revision is invalid.")
        if type(self.storage_backend) is not StorageBackend:
            raise RegistryAuthorityError("Receipt storage backend is invalid.")
        if self.operational_status not in {"active", "suspended", "archived"}:
            raise RegistryAuthorityError("Receipt operational status is invalid.")
        if type(self.verification) is not ReceiptVerification or not _utc(self.created_at):
            raise RegistryAuthorityError("Export receipt is invalid.")

    def __repr__(self) -> str:
        return "TransferReceipt(<redacted>)"


@dataclass(frozen=True, slots=True)
class PurgeTombstone:
    principal_id: str
    service_id: UUID
    logical_namespace: str
    created_via: str
    purged_at: datetime
    authority: PurgeAuthority
    receipt_id: UUID | None = None

    def __post_init__(self) -> None:
        if (
            not _valid_text(self.principal_id, MAX_PRINCIPAL_LENGTH)
            or type(self.service_id) is not UUID
            or self.service_id.int == 0
            or self.logical_namespace != f"svc_{self.service_id.hex}"
            or not _valid_text(self.created_via, MAX_CREATED_VIA_LENGTH)
            or not _utc(self.purged_at)
            or type(self.authority) is not PurgeAuthority
        ):
            raise RegistryAuthorityError("Purge tombstone is invalid.")
        if self.authority.kind is PurgeAuthorityKind.VERIFIED_RECEIPT:
            if self.receipt_id != self.authority.receipt_id:
                raise RegistryAuthorityError("Purge tombstone receipt is invalid.")
        elif self.receipt_id is not None:
            raise RegistryAuthorityError("Purge tombstone override is invalid.")

    def __repr__(self) -> str:
        return "PurgeTombstone(<redacted>)"


@dataclass(frozen=True, slots=True)
class LocalRegistryEvent:
    action: str
    outcome: str
    service_id: UUID
    principal_id: str
    created_via: str
    at: datetime

    def __post_init__(self) -> None:
        if (
            self.action not in {"suspend", "resume", "archive", "restore", "export", "import", "purge"}
            or self.outcome not in {"completed", "recovery_required"}
        ):
            raise RegistryAuthorityError("Local registry audit vocabulary is invalid.")
        _validate_service_id(self.service_id)
        _validate_principal(self.principal_id)
        _validate_created_via(self.created_via)
        if not _utc(self.at):
            raise RegistryAuthorityError("Local registry audit time is invalid.")

    def __repr__(self) -> str:
        return "LocalRegistryEvent(<redacted>)"


@dataclass(frozen=True, slots=True)
class PreparedRegistryClaim:
    intent: RegistryAuthorityIntent

    def __post_init__(self) -> None:
        if type(self.intent) not in {OperationalLifecycleIntent, PortableIntent} or (
            self.intent.phase is not OperationalLifecyclePhase.PREPARED
        ):
            raise RegistryAuthorityError("Prepared registry claim is invalid.")


@dataclass(frozen=True, slots=True)
class CompletedRegistryClaim:
    intent: RegistryAuthorityIntent
    result: Service | TransferReceipt | PurgeTombstone

    def __post_init__(self) -> None:
        if type(self.intent) not in {OperationalLifecycleIntent, PortableIntent} or (
            self.intent.phase is not OperationalLifecyclePhase.COMPLETED
        ):
            raise RegistryAuthorityError("Completed registry claim intent is invalid.")
        if type(self.result) not in {Service, TransferReceipt, PurgeTombstone}:
            raise RegistryAuthorityError("Completed registry claim result is invalid.")
        if type(self.intent) is OperationalLifecycleIntent:
            _validate_service_result(self.intent, self.result)
        elif self.intent.kind is PortableOperationKind.EXPORT:
            if (
                type(self.result) is not TransferReceipt
                or self.result.kind is not ReceiptKind.EXPORT
                or self.result.principal_id != self.intent.principal_id
                or self.result.service_id != self.intent.service_id
                or self.result.verification is not ReceiptVerification.VERIFIED
                or self.result.service_revision != self.intent.expected_service_revision
                or self.result.operational_status not in {"active", "suspended", "archived"}
            ):
                raise RegistryAuthorityError("Export completion result is invalid.")
        elif self.intent.kind is PortableOperationKind.IMPORT:
            _validate_import_result(self.intent, self.result)
        else:
            if (
                type(self.result) is not PurgeTombstone
                or self.result.principal_id != self.intent.principal_id
                or self.result.service_id != self.intent.service_id
            ):
                raise RegistryAuthorityError("Purge completion result is invalid.")

    def __repr__(self) -> str:
        return "CompletedRegistryClaim(<redacted>)"


@dataclass(frozen=True, slots=True)
class RecoveryRequiredRegistryClaim:
    intent: ActiveRegistryIntent

    def __post_init__(self) -> None:
        if type(self.intent) in {OperationalLifecycleIntent, PortableIntent}:
            valid = self.intent.phase is OperationalLifecyclePhase.RECOVERY_REQUIRED
        elif type(self.intent) is LifecycleIntent:
            valid = self.intent.phase is LifecyclePhase.RECOVERY_REQUIRED
        else:
            valid = False
        if not valid:
            raise RegistryAuthorityError("Recovery-required registry claim is invalid.")


@dataclass(frozen=True, slots=True)
class RegistryClaimConflict:
    """A global idempotency key was reused with another fingerprint or kind."""


@dataclass(frozen=True, slots=True)
class RegistryStaleRevision:
    """The named service is absent or no longer has the expected revision."""


@dataclass(frozen=True, slots=True)
class RegistryUnsupportedTransition:
    """The current service state cannot perform the requested operation."""


@dataclass(frozen=True, slots=True)
class RegistryOperationInProgress:
    intent: ActiveRegistryIntent

    def __post_init__(self) -> None:
        if type(self.intent) in {OperationalLifecycleIntent, PortableIntent}:
            valid = self.intent.phase is OperationalLifecyclePhase.PREPARED
        elif type(self.intent) is LifecycleIntent:
            valid = self.intent.phase is LifecyclePhase.PREPARED
        else:
            valid = False
        if not valid:
            raise RegistryAuthorityError("In-progress registry claim is invalid.")


@dataclass(frozen=True, slots=True)
class RegistryIdentityCollision:
    kind: IdentityCollisionKind

    def __post_init__(self) -> None:
        if type(self.kind) is not IdentityCollisionKind:
            raise RegistryAuthorityError("Registry identity collision is invalid.")


@dataclass(frozen=True, slots=True)
class TombstonedRegistryClaim:
    """The only replay-safe evidence retained after a service is purged."""

    idempotency_key: str
    fingerprint: str
    operation_kind: str
    service_id: UUID

    def __post_init__(self) -> None:
        _validate_idempotency_key(self.idempotency_key)
        _validate_sha256(self.fingerprint)
        if self.operation_kind not in {
            "legacy", "seed", "design", "materialise", "evolve",
            "suspend", "resume", "archive", "restore", "export", "import", "purge",
        }:
            raise RegistryAuthorityError("Tombstoned claim kind is invalid.")
        _validate_service_id(self.service_id)

    def __repr__(self) -> str:
        return "TombstonedRegistryClaim(<redacted>)"


type RegistryAuthorityOutcome = (
    PreparedRegistryClaim
    | CompletedRegistryClaim
    | RecoveryRequiredRegistryClaim
    | RegistryClaimConflict
    | RegistryStaleRevision
    | RegistryUnsupportedTransition
    | RegistryOperationInProgress
    | RegistryIdentityCollision
    | TombstonedRegistryClaim
)


def prepare_registry_intent(command: RegistryAuthorityCommand) -> RegistryAuthorityIntent:
    if type(command) in {ExportCommand, ImportCommand, PurgeCommand}:
        return _portable_intent(command)
    if type(command) not in {SuspendCommand, ResumeCommand, ArchiveCommand, RestoreCommand}:
        raise RegistryAuthorityError("Registry command is invalid.")
    try:
        return prepare_operational_intent(command)
    except Exception as error:
        raise RegistryAuthorityError("Registry command is invalid.") from error


def portable_fingerprint(command: PortableCommand) -> str:
    return hashlib.sha256(_canonical_portable_json(command).encode("utf-8")).hexdigest()


def _portable_intent(command: PortableCommand) -> PortableIntent:
    if type(command) is ExportCommand:
        return PortableIntent(
            command.kind, OperationalLifecyclePhase.PREPARED, command.principal_id, command.created_via,
            command.service_id, command.expected_service_revision, command.idempotency_key,
            portable_fingerprint(command),
        )
    if type(command) is ImportCommand:
        return PortableIntent(
            command.kind, OperationalLifecyclePhase.PREPARED, command.principal_id, command.created_via,
            command.identity.service_id, None, command.idempotency_key, portable_fingerprint(command),
            identity=command.identity, bundle_root_sha256=command.bundle_root_sha256,
            destination_backend=command.destination_backend,
        )
    if type(command) is PurgeCommand:
        return PortableIntent(
            command.kind, OperationalLifecyclePhase.PREPARED, command.principal_id, command.created_via,
            command.service_id, command.expected_service_revision, command.idempotency_key,
            portable_fingerprint(command), purge_authority=command.authority,
        )
    raise RegistryAuthorityError("Portable command is invalid.")


def _command_for_intent(intent: PortableIntent) -> PortableCommand:
    if intent.kind is PortableOperationKind.EXPORT:
        return ExportCommand(intent.principal_id, intent.created_via, intent.service_id, intent.expected_service_revision, intent.idempotency_key)  # type: ignore[arg-type]
    if intent.kind is PortableOperationKind.IMPORT:
        return ImportCommand(intent.principal_id, intent.created_via, intent.identity, intent.destination_backend, intent.bundle_root_sha256, intent.idempotency_key)  # type: ignore[arg-type]
    return PurgeCommand(intent.principal_id, intent.created_via, intent.service_id, intent.expected_service_revision, intent.idempotency_key, intent.purge_authority)  # type: ignore[arg-type]


def _canonical_portable_json(command: PortableCommand) -> str:
    value: dict[str, object] = {
        "kind": command.kind.value,
        "principal": command.principal_id,
        "created_via": command.created_via,
        "service_id": str(command.identity.service_id) if type(command) is ImportCommand else str(command.service_id),
    }
    if type(command) is ImportCommand:
        value["domain_name"] = command.identity.domain_name
        value["logical_namespace"] = command.identity.logical_namespace
        value["destination_backend"] = command.destination_backend.value
        value["bundle_root_sha256"] = command.bundle_root_sha256
    else:
        value["expected_service_revision"] = command.expected_service_revision
    if type(command) is PurgeCommand:
        value["purge_authority"] = command.authority.kind.value
        if command.authority.receipt_id is not None:
            value["receipt_id"] = str(command.authority.receipt_id)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _validate_service_result(intent: OperationalLifecycleIntent, result: object) -> None:
    if (
        type(result) is not Service
        or result.principal_id != intent.principal_id
        or result.service_id != intent.service_id
        or result.service_revision != intent.expected_service_revision + 1
        or result.operational_status != intent.target_operational_status
    ):
        raise RegistryAuthorityError("Operational completion result is invalid.")


def _validate_import_result(intent: PortableIntent, result: object) -> None:
    if (
        type(result) is not Service
        or intent.identity is None
        or result.principal_id != intent.identity.principal_id
        or result.service_id != intent.identity.service_id
        or result.domain_name != intent.identity.domain_name
        or (result.storage is not None and result.storage.namespace != intent.identity.logical_namespace)
    ):
        raise RegistryAuthorityError("Import completion result is invalid.")


def _validate_common(principal: str, via: str, service_id: UUID, revision: int, key: str) -> None:
    _validate_principal(principal)
    _validate_created_via(via)
    _validate_service_id(service_id)
    if type(revision) is not int or not 1 <= revision < MAX_SERVICE_REVISION:
        raise RegistryAuthorityError("Expected service revision is invalid.")
    _validate_idempotency_key(key)


def _validate_principal(value: object) -> None:
    if not _valid_text(value, MAX_PRINCIPAL_LENGTH):
        raise RegistryAuthorityError("Principal id is invalid.")


def _validate_created_via(value: object) -> None:
    if not _valid_text(value, MAX_CREATED_VIA_LENGTH):
        raise RegistryAuthorityError("Created-via value is invalid.")


def _validate_idempotency_key(value: object) -> None:
    if not _valid_text(value, MAX_IDEMPOTENCY_KEY_LENGTH):
        raise RegistryAuthorityError("Idempotency key is invalid.")


def _validate_service_id(value: object) -> None:
    if type(value) is not UUID or value.int == 0:
        raise RegistryAuthorityError("Service id is invalid.")


def _validate_sha256(value: object) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise RegistryAuthorityError("Digest is invalid.")


def _valid_text(value: object, maximum: int) -> bool:
    return type(value) is str and bool(value) and value == value.strip() and len(value) <= maximum


def _utc(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is UTC and value.utcoffset() == timedelta(0)
