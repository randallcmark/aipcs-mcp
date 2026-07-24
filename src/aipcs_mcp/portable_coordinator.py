"""Private finite coordinator for portable transfer and operational fences."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO
from uuid import UUID

from aipcs_mcp.application.models import Service
from aipcs_mcp.application.operational_lifecycle import (
    ArchiveCommand,
    OperationalLifecycleCommand,
    OperationalLifecycleIntent,
    RestoreCommand,
    ResumeCommand,
    SuspendCommand,
    transition_target_status,
)
from aipcs_mcp.application.portable import (
    ImportBundleRequest,
    PortableBundleContent,
    PortableExecutionResult,
    PortableResultCategory,
    decode_bundle_content,
    encode_member_payload,
    encode_service_payload,
)
from aipcs_mcp.application.portable_codec import PortableBundleEncoder
from aipcs_mcp.application.portable_models import (
    CANONICALIZATION_PROFILE,
    EXPORT_FORMAT_VERSION,
    LIMIT_PROFILE,
    PRODUCER_PACKAGE,
    PortableBundleContractError,
    PortableBundleLimits,
    PortableBundleSummary,
    PortableHeader,
)
from aipcs_mcp.application.portable_store import (
    PortableStoreMember,
    WriteAdmissionFence,
    validate_portable_members,
)
from aipcs_mcp.application.ports import Clock, PortableServiceStore, RegistryUnitOfWork, UowFactory
from aipcs_mcp.application.registry_authority import (
    CompletedRegistryClaim,
    ExportCommand,
    ImportCommand,
    ImportIdentity,
    PortableIntent,
    PreparedRegistryClaim,
    PurgeCommand,
    PurgeTombstone,
    ReceiptKind,
    ReceiptVerification,
    RecoveryRequiredRegistryClaim,
    RegistryAuthorityCommand,
    RegistryClaimConflict,
    RegistryIdentityCollision,
    RegistryOperationInProgress,
    RegistryStaleRevision,
    RegistryUnsupportedTransition,
    StorageBackend,
    TombstonedRegistryClaim,
    TransferReceipt,
)
from aipcs_mcp.contracts import CONTRACT_VERSION, PACKAGE_VERSION
from aipcs_mcp.portable_io import validate_and_spool_bundle
from aipcs_mcp.records import compile_record_specification
from aipcs_mcp.storage.errors import (
    StorageBusy,
    StorageContractError,
    StorageMigrationError,
    StorageUnavailable,
)


@dataclass(frozen=True, slots=True)
class _Admission:
    intent: PortableIntent | OperationalLifecycleIntent
    service: Service | None


class PortableCoordinator:
    """Coordinate one bounded attempt; committed registry evidence drives replay."""

    def __init__(
        self,
        uows: UowFactory,
        clock: Clock,
        receipt_ids: Callable[[], UUID],
        backend: StorageBackend,
        store: PortableServiceStore,
        *,
        producer_package_version: str = PACKAGE_VERSION,
        producer_mcp_contract: str = CONTRACT_VERSION,
    ) -> None:
        if (
            not callable(uows)
            or not callable(getattr(clock, "now", None))
            or not callable(receipt_ids)
            or type(backend) is not StorageBackend
            or not all(
                callable(getattr(store, name, None))
                for name in (
                    "open_snapshot",
                    "stage",
                    "observe",
                    "read_fence",
                    "transition_fence",
                    "purge",
                    "is_absent",
                )
            )
        ):
            raise ValueError("Portable coordinator dependencies are invalid.")
        try:
            PortableHeader(
                EXPORT_FORMAT_VERSION,
                CANONICALIZATION_PROFILE,
                LIMIT_PROFILE,
                PRODUCER_PACKAGE,
                producer_package_version,
                producer_mcp_contract,
                "2026-01-01T00:00:00.000000Z",
                backend.value,
            )
        except Exception:
            raise ValueError("Portable coordinator metadata is invalid.") from None
        self._uows = uows
        self._clock = clock
        self._receipt_ids = receipt_ids
        self._backend = backend
        self._store = store
        self._package_version = producer_package_version
        self._contract_version = producer_mcp_contract

    def export(
        self,
        command: ExportCommand,
        destination: BinaryIO,
        *,
        limits: PortableBundleLimits | None = None,
    ) -> PortableExecutionResult:
        if type(command) is not ExportCommand or not callable(
            getattr(destination, "write", None)
        ):
            return _result(PortableResultCategory.MALFORMED_INPUT)
        admitted = self._admit(command, require_service=True)
        if type(admitted) is PortableExecutionResult:
            return admitted
        if type(admitted.intent) is not PortableIntent or admitted.service is None:
            return _result(PortableResultCategory.INTERNAL_FAILURE)
        intent, service = admitted.intent, admitted.service
        try:
            at = self._clock.now()
            fence: WriteAdmissionFence | None = None
            members: tuple[PortableStoreMember, ...] = ()
            if service.design_state == "materialised":
                if service.storage is None or service.storage.backend != self._backend.value:
                    return self._recover(intent)
                fence = self._store.read_fence(service)
                if fence.state != "closed":
                    return self._recover(intent)
                with self._store.open_snapshot(service, fence) as reader:
                    members = tuple(reader)
                if service.manifest is None:
                    return self._recover(intent)
                members = validate_portable_members(
                    members, compile_record_specification(service.manifest)
                )
            lines, summary = self._encode(service, fence, members, at, limits)
        except PortableBundleContractError:
            return self._recover(intent)
        except Exception as error:
            return _storage_result(error, uncertain=False)

        verified = self._verify_export(command, intent, service, fence)
        if type(verified) is PortableExecutionResult:
            return verified
        try:
            receipt = self._receipt(
                ReceiptKind.EXPORT, service, summary.root_sha256, at
            )
        except Exception:
            return self._recover(intent)
        completed = self._terminal(
            lambda uow: uow.authority.complete_export(intent, receipt),
            post_physical=True,
        )
        if completed.category is not PortableResultCategory.COMPLETED:
            return completed
        actual_receipt = completed.receipt
        if (
            actual_receipt is None
            or actual_receipt.bundle_root_sha256 != summary.root_sha256
        ):
            return completed
        try:
            _write_lines(destination, lines)
        except Exception:
            return PortableExecutionResult(
                PortableResultCategory.OPERATION_UNCERTAIN,
                receipt=actual_receipt,
                summary=summary,
            )
        return PortableExecutionResult(
            PortableResultCategory.COMPLETED,
            receipt=actual_receipt,
            summary=summary,
            artifact_written=True,
        )

    def import_bundle(
        self,
        request: ImportBundleRequest,
        source: BinaryIO,
        *,
        limits: PortableBundleLimits | None = None,
    ) -> PortableExecutionResult:
        if type(request) is not ImportBundleRequest or not callable(
            getattr(source, "readline", None)
        ):
            return _result(PortableResultCategory.MALFORMED_INPUT)
        try:
            with validate_and_spool_bundle(source, limits=limits) as bundle:
                content = decode_bundle_content(
                    bundle.iter_lines(),
                    principal_id=request.principal_id,
                    destination_backend=request.destination_backend,
                )
                summary = bundle.summary
                if request.destination_backend is not self._backend:
                    return PortableExecutionResult(
                        PortableResultCategory.UNSUPPORTED_TRANSITION,
                        summary=summary,
                    )
                if request.dry_run:
                    return PortableExecutionResult(
                        PortableResultCategory.VALIDATED,
                        service=content.service,
                        summary=summary,
                    )
                command = ImportCommand(
                    request.principal_id,
                    request.created_via,
                    ImportIdentity(
                        request.principal_id,
                        content.service.service_id,
                        content.service.domain_name,
                        f"svc_{content.service.service_id.hex}",
                    ),
                    request.destination_backend,
                    summary.root_sha256,
                    request.idempotency_key,
                )
                return self._import_validated(command, content, summary)
        except PortableBundleContractError:
            return _result(PortableResultCategory.MALFORMED_INPUT)
        except Exception:
            return _result(PortableResultCategory.INTERNAL_FAILURE)

    def transition(
        self, command: OperationalLifecycleCommand
    ) -> PortableExecutionResult:
        if type(command) not in {SuspendCommand, ResumeCommand, ArchiveCommand, RestoreCommand}:
            return _result(PortableResultCategory.MALFORMED_INPUT)
        admitted = self._admit(command, require_service=True)
        if type(admitted) is PortableExecutionResult:
            return admitted
        if type(admitted.intent) is not OperationalLifecycleIntent or admitted.service is None:
            return _result(PortableResultCategory.INTERNAL_FAILURE)
        intent, service = admitted.intent, admitted.service
        try:
            target_status = transition_target_status(
                command, service.design_state, service.operational_status
            )
        except Exception:
            return self._recover(intent)
        if service.design_state == "materialised":
            if service.storage is None or service.storage.backend != self._backend.value:
                return self._recover(intent)
            target_fence = "open" if target_status == "active" else "closed"
            try:
                observed = self._store.read_fence(service)
            except Exception as error:
                return _storage_result(error, uncertain=False)
            try:
                changed = self._store.transition_fence(service, observed, target_fence)
                if changed.fence.state != target_fence:
                    return self._recover(intent)
            except Exception as error:
                try:
                    if self._store.read_fence(service).state != target_fence:
                        return _storage_result(error, uncertain=True)
                except Exception:
                    return _storage_result(error, uncertain=True)
        try:
            at = self._clock.now()
        except Exception:
            return _result(PortableResultCategory.OPERATION_UNCERTAIN)
        return self._terminal(
            lambda uow: uow.authority.finalize_operational(intent, at),
            post_physical=service.design_state == "materialised",
        )

    def purge(self, command: PurgeCommand) -> PortableExecutionResult:
        if type(command) is not PurgeCommand:
            return _result(PortableResultCategory.MALFORMED_INPUT)
        admitted = self._admit(command, require_service=True)
        if type(admitted) is PortableExecutionResult:
            return admitted
        if type(admitted.intent) is not PortableIntent or admitted.service is None:
            return _result(PortableResultCategory.INTERNAL_FAILURE)
        intent, service = admitted.intent, admitted.service
        if service.design_state == "materialised":
            if (
                service.storage is None
                or service.storage.backend != self._backend.value
                or service.operational_status != "archived"
            ):
                return self._recover(intent)
            try:
                self._store.purge(service)
            except Exception as error:
                try:
                    if not self._store.is_absent(service):
                        return _storage_result(error, uncertain=True)
                except Exception:
                    return _storage_result(error, uncertain=True)
            try:
                if not self._store.is_absent(service):
                    return _result(PortableResultCategory.OPERATION_UNCERTAIN)
            except Exception as error:
                return _storage_result(error, uncertain=True)
        try:
            at = self._clock.now()
        except Exception:
            return _result(PortableResultCategory.OPERATION_UNCERTAIN)
        return self._terminal(
            lambda uow: uow.authority.finalize_purge(intent, at),
            post_physical=service.design_state == "materialised",
        )

    def _import_validated(
        self,
        command: ImportCommand,
        content: PortableBundleContent,
        summary: PortableBundleSummary,
    ) -> PortableExecutionResult:
        admitted = self._admit(command, require_service=False)
        if type(admitted) is PortableExecutionResult:
            return admitted
        if type(admitted.intent) is not PortableIntent:
            return _result(PortableResultCategory.INTERNAL_FAILURE)
        intent = admitted.intent
        if content.service.design_state == "materialised":
            assert content.export_fence is not None
            try:
                staged = self._store.stage(
                    content.service, content.members, content.export_fence
                )
                if staged.status == "different":
                    return self._recover(intent)
            except Exception as error:
                try:
                    exact = self._store.observe(
                        content.service, content.export_fence, content.members
                    )
                except Exception:
                    exact = False
                if not exact:
                    return _storage_result(error, uncertain=True)
            try:
                if not self._store.observe(
                    content.service, content.export_fence, content.members
                ):
                    return _result(PortableResultCategory.OPERATION_UNCERTAIN)
            except Exception as error:
                return _storage_result(error, uncertain=True)
        try:
            at = self._clock.now()
            receipt = self._receipt(
                ReceiptKind.IMPORT,
                content.service,
                intent.bundle_root_sha256,
                at,
            )
        except Exception:
            return self._recover(intent)
        completed = self._terminal(
            lambda uow: uow.authority.publish_import(
                intent, content.service, receipt
            ),
            post_physical=content.service.design_state == "materialised",
        )
        if completed.category is PortableResultCategory.COMPLETED:
            if completed.service != content.service:
                return _result(PortableResultCategory.INTERNAL_FAILURE)
            return PortableExecutionResult(
                PortableResultCategory.COMPLETED,
                service=completed.service,
                summary=summary,
            )
        return completed

    def _admit(
        self, command: RegistryAuthorityCommand, *, require_service: bool
    ) -> _Admission | PortableExecutionResult:
        try:
            uow = self._uows()
        except Exception as error:
            return _registry_error(error, uncertain=False)
        try:
            outcome = uow.authority.resolve_or_prepare(command)
            if type(outcome) is not PreparedRegistryClaim:
                result = _registry_outcome(outcome)
                _rollback_close(uow)
                return result
            service = (
                uow.services.get(outcome.intent.principal_id, outcome.intent.service_id)
                if require_service
                else None
            )
            if require_service and type(service) is not Service:
                _rollback_close(uow)
                return _result(PortableResultCategory.INTERNAL_FAILURE)
            try:
                uow.commit()
            except Exception:
                _close_only(uow)
                return _result(PortableResultCategory.OPERATION_UNCERTAIN)
            try:
                uow.close()
            except Exception:
                return _result(PortableResultCategory.OPERATION_UNCERTAIN)
            return _Admission(outcome.intent, service)
        except Exception as error:
            _rollback_close(uow)
            return _registry_error(error, uncertain=False)

    def _verify_export(
        self,
        command: ExportCommand,
        intent: PortableIntent,
        service: Service,
        fence: WriteAdmissionFence | None,
    ) -> bool | PortableExecutionResult:
        if fence is not None:
            try:
                if self._store.read_fence(service) != fence:
                    return self._recover(intent)
            except Exception as error:
                return _storage_result(error, uncertain=False)
        try:
            uow = self._uows()
        except Exception as error:
            return _registry_error(error, uncertain=False)
        try:
            outcome = uow.authority.resolve_or_prepare(command)
            current = uow.services.get(command.principal_id, command.service_id)
            _rollback_close(uow)
            if (
                type(outcome) is not PreparedRegistryClaim
                or outcome.intent != intent
                or current != service
            ):
                return self._recover(intent)
            return True
        except Exception as error:
            _rollback_close(uow)
            return _registry_error(error, uncertain=False)

    def _encode(
        self,
        service: Service,
        fence: WriteAdmissionFence | None,
        members: tuple[PortableStoreMember, ...],
        at: datetime,
        limits: PortableBundleLimits | None,
    ) -> tuple[tuple[bytes, ...], PortableBundleSummary]:
        encoder = PortableBundleEncoder(limits)
        header = PortableHeader(
            EXPORT_FORMAT_VERSION,
            CANONICALIZATION_PROFILE,
            LIMIT_PROFILE,
            PRODUCER_PACKAGE,
            self._package_version,
            self._contract_version,
            at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            self._backend.value,
        )
        lines = [encoder.add("header", header.payload())]
        lines.append(encoder.add("service", encode_service_payload(service, fence)))
        for member in members:
            lines.append(encoder.add(member.kind, encode_member_payload(member)))
        trailer, summary = encoder.finish()
        lines.append(trailer)
        return tuple(lines), summary

    def _receipt(
        self,
        kind: ReceiptKind,
        service: Service,
        root: str | None,
        at: datetime,
    ) -> TransferReceipt:
        if root is None:
            raise PortableBundleContractError()
        receipt_id = self._receipt_ids()
        return TransferReceipt(
            receipt_id,
            kind,
            service.principal_id,
            service.service_id,
            root,
            EXPORT_FORMAT_VERSION,
            self._backend,
            service.service_revision,
            service.operational_status,
            ReceiptVerification.VERIFIED,
            at,
        )

    def _recover(
        self, intent: PortableIntent | OperationalLifecycleIntent
    ) -> PortableExecutionResult:
        try:
            at = self._clock.now()
        except Exception:
            return _result(PortableResultCategory.OPERATION_UNCERTAIN)
        return self._terminal(
            lambda uow: uow.authority.mark_recovery_required(intent, at),
            post_physical=True,
        )

    def _terminal(
        self,
        operation: Callable[[RegistryUnitOfWork], object],
        *,
        post_physical: bool,
    ) -> PortableExecutionResult:
        try:
            uow = self._uows()
        except Exception as error:
            return _registry_error(error, uncertain=post_physical)
        try:
            outcome = operation(uow)
            result = _registry_outcome(outcome)
            if result.category not in {
                PortableResultCategory.COMPLETED,
                PortableResultCategory.RECOVERY_REQUIRED,
            }:
                _rollback_close(uow)
                return result
            try:
                uow.commit()
            except Exception:
                _close_only(uow)
                return _result(PortableResultCategory.OPERATION_UNCERTAIN)
            try:
                uow.close()
            except Exception:
                return _result(PortableResultCategory.OPERATION_UNCERTAIN)
            return result
        except Exception as error:
            _rollback_close(uow)
            return _registry_error(error, uncertain=post_physical)


def _registry_outcome(outcome: object) -> PortableExecutionResult:
    if type(outcome) is CompletedRegistryClaim:
        if type(outcome.result) is Service:
            return PortableExecutionResult(
                PortableResultCategory.COMPLETED, service=outcome.result
            )
        if type(outcome.result) is TransferReceipt:
            return PortableExecutionResult(
                PortableResultCategory.COMPLETED, receipt=outcome.result
            )
        if type(outcome.result) is PurgeTombstone:
            return PortableExecutionResult(
                PortableResultCategory.COMPLETED, tombstone=outcome.result
            )
        return _result(PortableResultCategory.COMPLETED)
    if type(outcome) is RecoveryRequiredRegistryClaim:
        return _result(PortableResultCategory.RECOVERY_REQUIRED)
    if type(outcome) is RegistryClaimConflict:
        return _result(PortableResultCategory.CHANGED_FINGERPRINT)
    if type(outcome) is RegistryStaleRevision:
        return _result(PortableResultCategory.STALE_REVISION)
    if type(outcome) is RegistryUnsupportedTransition:
        return _result(PortableResultCategory.UNSUPPORTED_TRANSITION)
    if type(outcome) is RegistryOperationInProgress:
        return _result(PortableResultCategory.OPERATION_IN_PROGRESS)
    if type(outcome) is RegistryIdentityCollision:
        return _result(PortableResultCategory.IDENTITY_COLLISION)
    if type(outcome) is TombstonedRegistryClaim:
        return _result(PortableResultCategory.TOMBSTONED)
    return _result(PortableResultCategory.INTERNAL_FAILURE)


def _result(category: PortableResultCategory) -> PortableExecutionResult:
    return PortableExecutionResult(category)


def _registry_error(error: Exception, *, uncertain: bool) -> PortableExecutionResult:
    if isinstance(error, StorageBusy):
        return _result(PortableResultCategory.STORAGE_BUSY)
    if isinstance(error, StorageUnavailable):
        return _result(
            PortableResultCategory.OPERATION_UNCERTAIN
            if uncertain
            else PortableResultCategory.STORAGE_UNAVAILABLE
        )
    return _result(
        PortableResultCategory.OPERATION_UNCERTAIN
        if uncertain
        else PortableResultCategory.INTERNAL_FAILURE
    )


def _storage_result(error: Exception, *, uncertain: bool) -> PortableExecutionResult:
    if isinstance(error, StorageBusy):
        return _result(PortableResultCategory.STORAGE_BUSY)
    if isinstance(error, StorageUnavailable):
        return _result(
            PortableResultCategory.OPERATION_UNCERTAIN
            if uncertain
            else PortableResultCategory.STORAGE_UNAVAILABLE
        )
    if isinstance(error, (StorageMigrationError, StorageContractError)):
        return _result(
            PortableResultCategory.OPERATION_UNCERTAIN
            if uncertain
            else PortableResultCategory.INTERNAL_FAILURE
        )
    return _result(
        PortableResultCategory.OPERATION_UNCERTAIN
        if uncertain
        else PortableResultCategory.INTERNAL_FAILURE
    )


def _write_lines(destination: BinaryIO, lines: tuple[bytes, ...]) -> None:
    for line in lines:
        written = destination.write(line)
        if type(written) is not int or written != len(line):
            raise OSError
    flush = getattr(destination, "flush", None)
    if callable(flush):
        flush()


def _rollback_close(uow: RegistryUnitOfWork) -> None:
    with suppress(Exception):
        uow.rollback()
    _close_only(uow)


def _close_only(uow: RegistryUnitOfWork) -> None:
    with suppress(Exception):
        uow.close()
