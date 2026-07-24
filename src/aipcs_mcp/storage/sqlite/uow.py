"""SQLite transaction and registry repositories."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from functools import wraps
from uuid import UUID

from aipcs_mcp.application.models import (
    AuditEvent,
    CompletedLifecycleClaim,
    CompletedNonLifecycleClaim,
    ConflictClaim,
    EvolveCompletion,
    LifecycleRegistryOutcome,
    MaterialiseCompletion,
    NewClaim,
    NonLifecycleKind,
    NonLifecycleRegistryOutcome,
    OperationInProgress,
    PreparedLifecycleClaim,
    RecoveryRequiredLifecycleClaim,
    Service,
    ServiceSaveResult,
    StaleRevision,
    UnsupportedTransition,
)
from aipcs_mcp.application.operational_lifecycle import (
    ArchiveCommand,
    OperationalLifecycleIntent,
    OperationalLifecyclePhase,
    RestoreCommand,
    ResumeCommand,
    SuspendCommand,
    transition_target_status,
)
from aipcs_mcp.application.registry_authority import (
    CompletedRegistryClaim,
    IdentityCollisionKind,
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
    StorageBackend,
    TombstonedRegistryClaim,
    TransferReceipt,
    prepare_registry_intent,
)
from aipcs_mcp.lifecycle import (
    EvolveCommand,
    LifecycleCommand,
    LifecycleIntent,
    LifecycleKind,
    LifecyclePhase,
    MaterialiseCommand,
    RecoveryState,
    lifecycle_fingerprint,
    prepare_intent,
)
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.relational import RelationalContractError, classify_transition, compile_manifest
from aipcs_mcp.storage.codecs import (
    decode_legacy_result,
    decode_result,
    decode_service,
    encode_manifest,
    encode_result,
    encode_service_values,
    encode_time,
)
from aipcs_mcp.storage.errors import (
    StorageBusy,
    StorageMigrationError,
    StorageUnavailable,
)
from aipcs_mcp.storage.registry_authority_codecs import (
    decode_registry_authority_intent,
    decode_registry_authority_result,
    encode_registry_authority_intent,
    encode_registry_authority_result,
    validate_registry_authority_claim_row,
)

from .connection import set_query_only
from .location import AnchoredLocation
from .result_codes import is_sqlite_busy


def _bounded_uow[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return method(*args, **kwargs)
        except StorageBusy:
            failure: Exception = StorageBusy()
        except StorageUnavailable:
            failure = StorageUnavailable()
        except StorageMigrationError:
            failure = StorageMigrationError()
        except Exception as error:
            failure = StorageBusy() if is_sqlite_busy(error) else StorageMigrationError()
        raise failure from None

    return wrapped


class SQLiteRegistryUnitOfWork:
    def __init__(self, connection: sqlite3.Connection, location: AnchoredLocation) -> None:
        if not isinstance(location, AnchoredLocation):
            raise StorageUnavailable()
        self._connection = connection
        self._location = location
        self._closed = False
        self._terminal = False
        self._write_transaction = False
        self.services = self
        self.mutations = self
        self.audits = self
        self.authority = self

    def _open(self) -> None:
        if self._closed or self._terminal:
            raise StorageMigrationError()

    def _verify_live(self) -> None:
        self._location.verify_live_database_identity()
        self._location.verify_live_sidecars(allow_journal=False)

    def _read(self) -> None:
        self._open()
        if not self._connection.in_transaction:
            self._verify_live()
            self._connection.execute("BEGIN")
            self._verify_live()

    def _write(self) -> None:
        self._open()
        if self._connection.in_transaction and not self._write_transaction:
            # A read-only UoW owns a stable deferred snapshot. SQLite cannot
            # safely upgrade that snapshot in place, so release it before
            # acquiring the writer slot; repository write methods then
            # re-read any required state under BEGIN IMMEDIATE.
            self._verify_live()
            self._connection.rollback()
            self._verify_live()
        if not self._connection.in_transaction:
            self._verify_live()
            set_query_only(self._connection, False)
            try:
                self._connection.execute("BEGIN IMMEDIATE")
            except Exception:
                self._location.adopt_live_sidecars(allow_journal=False)
                raise
            self._location.adopt_live_sidecars(allow_journal=False)
            self._verify_live()
            self._write_transaction = True

    @_bounded_uow
    def find_domain(self, principal_id: str, domain_name: str) -> Service | None:
        self._read()
        row = self._connection.execute(
            'SELECT * FROM "aipcs_registry_service" WHERE "principal_id"=? AND "domain_name"=?',
            (principal_id, domain_name),
        ).fetchone()
        return decode_service(row) if row else None

    @_bounded_uow
    def get(self, principal_id: str, service_id: UUID) -> Service | None:
        self._read()
        row = self._connection.execute(
            'SELECT * FROM "aipcs_registry_service" WHERE "principal_id"=? AND "service_id"=?',
            (principal_id, str(service_id)),
        ).fetchone()
        return decode_service(row) if row else None

    @_bounded_uow
    def count(self, principal_id: str) -> int:
        self._read()
        row = self._connection.execute(
            'SELECT COUNT(*) FROM "aipcs_registry_service" WHERE "principal_id"=?',
            (principal_id,),
        ).fetchone()
        if row is None or type(row[0]) is not int or row[0] < 0:
            raise ValueError
        return row[0]

    @_bounded_uow
    def list(self, principal_id: str, limit: int) -> list[Service]:
        self._read()
        rows = self._connection.execute(
            'SELECT * FROM "aipcs_registry_service" WHERE "principal_id"=? '
            'ORDER BY "created_at" ASC, "service_id" ASC LIMIT ?',
            (principal_id, limit),
        ).fetchall()
        return [decode_service(row) for row in rows]

    @_bounded_uow
    def recovery_state(self, principal_id: str, service_id: UUID) -> RecoveryState:
        """Read the one active lifecycle aggregate without exposing operation evidence."""

        self._read()
        rows = self._connection.execute(
            'SELECT "phase" FROM "aipcs_registry_claim" WHERE "principal_id"=? '
            'AND "service_id"=? AND "phase" IN (\'prepared\',\'recovery_required\')',
            (principal_id, str(service_id)),
        ).fetchall()
        if not rows:
            return RecoveryState.CLEAR
        if len(rows) != 1:
            raise ValueError
        if rows[0]["phase"] == "prepared":
            return RecoveryState.PENDING
        if rows[0]["phase"] == "recovery_required":
            return RecoveryState.RECOVERY_REQUIRED
        raise ValueError

    @_bounded_uow
    def add(self, service: Service) -> None:
        self._write()
        self._connection.execute(
            'INSERT INTO "aipcs_registry_service" VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            encode_service_values(service),
        )
        self._insert_live_identity(service)

    @_bounded_uow
    def save(
        self, service: Service, expected_service_revision: int
    ) -> ServiceSaveResult:
        self._write()
        if (
            type(expected_service_revision) is not int
            or not 1 <= expected_service_revision < (1 << 63) - 1
            or service.service_revision != expected_service_revision + 1
        ):
            raise ValueError
        values = encode_service_values(service)
        cursor = self._connection.execute(
            'UPDATE "aipcs_registry_service" SET "domain_name"=?,"domain_class"=?,'
            '"intent_description"=?,"created_at"=?,"updated_at"=?,"last_activity_at"=?,'
            '"manifest_json"=?,"schema_version"=?,"design_state"=?,"operational_status"=?,'
            '"service_revision"=?,"materialised_at"=?,"storage_backend"=?,'
            '"storage_namespace"=? WHERE "service_id"=? AND "principal_id"=? '
            'AND "service_revision"=?',
            values[2:] + values[:2] + (expected_service_revision,),
        )
        if cursor.rowcount == 1:
            self._update_live_identity(service)
            return "saved"
        if cursor.rowcount == 0:
            return "stale_revision"
        raise ValueError

    @_bounded_uow
    def resolve_non_lifecycle(
        self,
        kind: NonLifecycleKind,
        principal_id: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> NonLifecycleRegistryOutcome:
        self._write()
        if kind not in {"seed", "design"}:
            raise ValueError
        row = self._claim_row(principal_id, idempotency_key)
        if row is None:
            return NewClaim()
        if row["fingerprint"] != fingerprint:
            return ConflictClaim()
        operation_kind = row["operation_kind"]
        if operation_kind not in {"legacy", kind} or row["phase"] != "completed":
            return ConflictClaim()
        decoder = decode_legacy_result if operation_kind == "legacy" else decode_result
        service = decoder(row["result_json"], principal_id, row["service_id"])
        return CompletedNonLifecycleClaim(operation_kind, service)

    @_bounded_uow
    def complete_non_lifecycle(
        self,
        kind: NonLifecycleKind,
        principal_id: str,
        idempotency_key: str,
        fingerprint: str,
        service: Service,
    ) -> None:
        self._write()
        if kind not in {"seed", "design"} or service.principal_id != principal_id:
            raise ValueError
        self._connection.execute(
            'INSERT INTO "aipcs_registry_claim"('
            '"principal_id","idempotency_key","fingerprint","service_id","operation_kind",'
            '"phase","created_via","intent_json","result_json","recovery_category") '
            "VALUES (?,?,?,?,?,'completed',NULL,NULL,?,NULL)",
            (
                principal_id,
                idempotency_key,
                fingerprint,
                str(service.service_id),
                kind,
                encode_result(service),
            ),
        )

    @_bounded_uow
    def resolve_or_admit(self, command: LifecycleCommand) -> LifecycleRegistryOutcome:
        self._write()
        if type(command) not in {MaterialiseCommand, EvolveCommand}:
            raise ValueError
        fingerprint = lifecycle_fingerprint(command)
        row = self._claim_row(command.principal_id, command.idempotency_key)
        if row is not None:
            return self._existing_lifecycle_claim(row, command, fingerprint)

        service = self._service(command.principal_id, command.service_id)
        if service is None:
            return StaleRevision()
        if (
            service.service_revision != command.expected_service_revision
            or service.schema_version != command.expected_schema_version
        ):
            return StaleRevision()
        if not self._supported_lifecycle_state(service, command):
            return UnsupportedTransition()
        if service.service_revision >= (1 << 63) - 1:
            return UnsupportedTransition()

        blocker = self._connection.execute(
            'SELECT * FROM "aipcs_registry_claim" WHERE "principal_id"=? '
            'AND "service_id"=? AND "phase" IN (\'prepared\',\'recovery_required\') LIMIT 1',
            (command.principal_id, str(command.service_id)),
        ).fetchone()
        if blocker is not None:
            if blocker["operation_kind"] in {"materialise", "evolve"}:
                intent = _decode_legacy_lifecycle_intent(blocker)
                if intent.phase is LifecyclePhase.RECOVERY_REQUIRED:
                    return RecoveryRequiredLifecycleClaim(intent)
            return OperationInProgress()

        target = service.manifest if type(command) is MaterialiseCommand else command.target_manifest
        if target is None:
            raise ValueError
        try:
            if type(command) is MaterialiseCommand:
                compile_manifest(target)
            else:
                classify_transition(service.manifest, target)
        except RelationalContractError:
            return UnsupportedTransition()
        intent = prepare_intent(command, target)
        self._insert_prepared(intent)
        return PreparedLifecycleClaim(intent)

    @_bounded_uow
    def finalize_completed(
        self, completion: MaterialiseCompletion | EvolveCompletion
    ) -> CompletedLifecycleClaim | RecoveryRequiredLifecycleClaim:
        self._write()
        if type(completion) not in {MaterialiseCompletion, EvolveCompletion}:
            raise ValueError
        intent = completion.prepared_intent
        existing = self._terminal_or_prepared(intent)
        if type(existing) in {CompletedLifecycleClaim, RecoveryRequiredLifecycleClaim}:
            return existing

        service = self._service(intent.principal_id, intent.service_id)
        if service is not None and completion.at < service.updated_at:
            raise ValueError
        if service is None or not self._finalization_preconditions(service, completion):
            return self._mark_recovery_required(intent, completion.at)
        terminal = self._terminal_service(service, completion)
        if self.save(terminal, intent.expected_service_revision) != "saved":
            return self._mark_recovery_required(intent, completion.at)

        terminal_intent = intent.with_phase(LifecyclePhase.COMPLETED)
        cursor = self._connection.execute(
            'UPDATE "aipcs_registry_claim" SET "phase"=\'completed\',"result_json"=? '
            'WHERE "principal_id"=? AND "idempotency_key"=? AND "fingerprint"=? '
            'AND "phase"=\'prepared\'',
            (
                encode_result(terminal),
                intent.principal_id,
                intent.idempotency_key,
                intent.fingerprint,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError
        self._append_terminal(intent, "completed", completion.at)
        return CompletedLifecycleClaim(terminal_intent, terminal)

    @_bounded_uow
    def finalize_recovery_required(
        self, prepared_intent: LifecycleIntent, at: datetime
    ) -> CompletedLifecycleClaim | RecoveryRequiredLifecycleClaim:
        self._write()
        encode_time(at)
        existing = self._terminal_or_prepared(prepared_intent)
        if type(existing) in {CompletedLifecycleClaim, RecoveryRequiredLifecycleClaim}:
            return existing
        return self._mark_recovery_required(prepared_intent, at)

    def _claim_row(self, principal_id: str, idempotency_key: str) -> sqlite3.Row | None:
        return self._connection.execute(
            'SELECT * FROM "aipcs_registry_claim" '
            'WHERE "principal_id"=? AND "idempotency_key"=?',
            (principal_id, idempotency_key),
        ).fetchone()

    def _service(self, principal_id: str, service_id: UUID) -> Service | None:
        row = self._connection.execute(
            'SELECT * FROM "aipcs_registry_service" '
            'WHERE "principal_id"=? AND "service_id"=?',
            (principal_id, str(service_id)),
        ).fetchone()
        return None if row is None else decode_service(row)

    @staticmethod
    def _supported_lifecycle_state(service: Service, command: LifecycleCommand) -> bool:
        if service.operational_status != "active" or service.manifest is None:
            return False
        if type(command) is MaterialiseCommand:
            return (
                service.design_state == "seeded"
                and service.schema_version == 1
                and service.materialised_at is None
                and service.storage is None
            )
        return (
            service.design_state == "materialised"
            and service.materialised_at is not None
            and service.storage is not None
        )

    def _existing_lifecycle_claim(
        self, row: sqlite3.Row, command: LifecycleCommand, fingerprint: str
    ) -> LifecycleRegistryOutcome:
        if row["fingerprint"] != fingerprint or row["operation_kind"] != command.kind.value:
            return ConflictClaim()
        intent = _decode_legacy_lifecycle_intent(row)
        if intent.phase is LifecyclePhase.PREPARED:
            return PreparedLifecycleClaim(intent)
        if intent.phase is LifecyclePhase.RECOVERY_REQUIRED:
            return RecoveryRequiredLifecycleClaim(intent)
        service = decode_result(row["result_json"], intent.principal_id, str(intent.service_id))
        return CompletedLifecycleClaim(intent, service)

    def _insert_prepared(self, intent: LifecycleIntent) -> None:
        self._connection.execute(
            'INSERT INTO "aipcs_registry_claim"('
            '"principal_id","idempotency_key","fingerprint","service_id","operation_kind",'
            '"phase","created_via","intent_json","result_json","recovery_category") '
            "VALUES (?,?,?,?,?,'prepared',?,?,NULL,NULL)",
            (
                intent.principal_id,
                intent.idempotency_key,
                intent.fingerprint,
                str(intent.service_id),
                intent.kind.value,
                intent.created_via,
                _encode_legacy_lifecycle_intent(intent),
            ),
        )

    def _terminal_or_prepared(
        self, prepared_intent: LifecycleIntent
    ) -> PreparedLifecycleClaim | CompletedLifecycleClaim | RecoveryRequiredLifecycleClaim:
        if (
            type(prepared_intent) is not LifecycleIntent
            or prepared_intent.phase is not LifecyclePhase.PREPARED
        ):
            raise ValueError
        row = self._claim_row(prepared_intent.principal_id, prepared_intent.idempotency_key)
        if row is None or row["fingerprint"] != prepared_intent.fingerprint:
            raise ValueError
        stored = _decode_legacy_lifecycle_intent(row)
        if stored.kind is not prepared_intent.kind:
            raise ValueError
        if stored.phase is LifecyclePhase.PREPARED:
            if stored != prepared_intent:
                raise ValueError
            return PreparedLifecycleClaim(stored)
        if stored.phase is LifecyclePhase.RECOVERY_REQUIRED:
            return RecoveryRequiredLifecycleClaim(stored)
        service = decode_result(
            row["result_json"],
            prepared_intent.principal_id,
            str(prepared_intent.service_id),
        )
        return CompletedLifecycleClaim(stored, service)

    @staticmethod
    def _finalization_preconditions(
        service: Service, completion: MaterialiseCompletion | EvolveCompletion
    ) -> bool:
        intent = completion.prepared_intent
        if (
            service.principal_id != intent.principal_id
            or service.service_id != intent.service_id
            or service.operational_status != "active"
            or service.service_revision != intent.expected_service_revision
            or service.schema_version != intent.expected_schema_version
            or service.manifest is None
            or completion.at < service.updated_at
        ):
            return False
        if type(completion) is MaterialiseCompletion:
            return (
                service.design_state == "seeded"
                and service.manifest == intent.target_manifest
                and service.schema_version == 1
                and service.materialised_at is None
                and service.storage is None
            )
        return (
            service.design_state == "materialised"
            and service.materialised_at is not None
            and service.storage is not None
            and intent.target_manifest.schema_version == service.schema_version + 1
        )

    @staticmethod
    def _terminal_service(
        service: Service, completion: MaterialiseCompletion | EvolveCompletion
    ) -> Service:
        intent = completion.prepared_intent
        common = {
            "updated_at": completion.at,
            "last_activity_at": completion.at,
            "service_revision": intent.expected_service_revision + 1,
        }
        if type(completion) is MaterialiseCompletion:
            return replace(
                service,
                design_state="materialised",
                materialised_at=completion.at,
                storage=completion.storage,
                **common,
            )
        target = intent.target_manifest
        return replace(
            service,
            manifest=target,
            schema_version=target.schema_version,
            **common,
        )

    def _mark_recovery_required(
        self, intent: LifecycleIntent, at: datetime
    ) -> RecoveryRequiredLifecycleClaim:
        encode_time(at)
        cursor = self._connection.execute(
            'UPDATE "aipcs_registry_claim" SET "phase"=\'recovery_required\','
            '"recovery_category"=\'recovery_required\' WHERE "principal_id"=? '
            'AND "idempotency_key"=? AND "fingerprint"=? AND "phase"=\'prepared\'',
            (
                intent.principal_id,
                intent.idempotency_key,
                intent.fingerprint,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError
        terminal = intent.with_phase(LifecyclePhase.RECOVERY_REQUIRED)
        self._append_terminal(intent, "recovery_required", at)
        return RecoveryRequiredLifecycleClaim(terminal)

    def _append_terminal(self, intent: LifecycleIntent, outcome: str, at: datetime) -> None:
        self.append(
            AuditEvent(
                action=intent.kind.value,
                outcome=outcome,
                service_id=intent.service_id,
                principal_id=intent.principal_id,
                created_via=intent.created_via,
                at=at,
            )
        )

    @_bounded_uow
    def resolve_or_prepare(self, command: RegistryAuthorityCommand) -> RegistryAuthorityOutcome:
        self._write()
        intent = prepare_registry_intent(command)
        row = self._claim_row(intent.principal_id, intent.idempotency_key)
        if row is not None:
            return self._existing_authority_claim(row, intent)

        if type(intent) is PortableIntent and intent.kind is PortableOperationKind.IMPORT:
            if intent.destination_backend is not StorageBackend.SQLITE:
                return RegistryUnsupportedTransition()
            collision = self._import_collision(intent)
            if collision is not None:
                return collision
            self._insert_authority_prepared(intent)
            assert intent.identity is not None
            self._connection.execute(
                'INSERT INTO "aipcs_registry_identity"('
                '"service_id","principal_id","identity_state","domain_name",'
                '"storage_backend","storage_namespace","claim_idempotency_key","lifecycle_at") '
                "VALUES (?,?,'import_prepared',?,?,?,?,?)",
                (
                    str(intent.service_id), intent.principal_id, intent.identity.domain_name,
                    intent.destination_backend.value if intent.destination_backend is not None else None,
                    intent.identity.logical_namespace, intent.idempotency_key,
                    encode_time(datetime.now(UTC)),
                ),
            )
            # The reservation timestamp is local metadata only; deterministic
            # operation evidence remains wholly inside the typed intent.
            return PreparedRegistryClaim(intent)

        service = self._service(intent.principal_id, intent.service_id)
        if service is None or intent.expected_service_revision is None:
            return RegistryStaleRevision()
        if service.service_revision != intent.expected_service_revision:
            return RegistryStaleRevision()
        blocker = self._authority_blocker(intent.principal_id, intent.service_id)
        if blocker is not None:
            return blocker
        if type(intent) is OperationalLifecycleIntent:
            try:
                transition_target_status(_operational_command(intent), service.design_state, service.operational_status)
            except Exception:
                return RegistryUnsupportedTransition()
        elif intent.kind is PortableOperationKind.EXPORT:
            if service.design_state == "materialised" and service.operational_status == "active":
                return RegistryUnsupportedTransition()
        elif intent.kind is PortableOperationKind.PURGE:
            if service.operational_status != "archived" or not self._purge_receipt_is_valid(intent):
                return RegistryUnsupportedTransition()
        self._insert_authority_prepared(intent)
        return PreparedRegistryClaim(intent)

    @_bounded_uow
    def finalize_operational(
        self, prepared: RegistryAuthorityIntent, at: datetime
    ) -> RegistryAuthorityOutcome:
        self._write()
        if type(prepared) is not OperationalLifecycleIntent:
            raise ValueError
        existing = self._authority_prepared_or_terminal(prepared)
        if type(existing) is not PreparedRegistryClaim:
            return existing
        service = self._service(prepared.principal_id, prepared.service_id)
        try:
            if (
                service is None or service.service_revision != prepared.expected_service_revision
                or at < service.updated_at
            ):
                raise ValueError
            target = transition_target_status(
                _operational_command(prepared), service.design_state, service.operational_status
            )
            terminal_service = replace(
                service, operational_status=target, updated_at=at, last_activity_at=at,
                service_revision=service.service_revision + 1,
            )
            if self.save(terminal_service, prepared.expected_service_revision) != "saved":
                raise ValueError
        except Exception:
            return self._mark_authority_recovery(prepared, at)
        return self._complete_authority(prepared, terminal_service, at)

    @_bounded_uow
    def complete_export(
        self, prepared: RegistryAuthorityIntent, receipt: TransferReceipt
    ) -> RegistryAuthorityOutcome:
        self._write()
        if type(prepared) is not PortableIntent or prepared.kind is not PortableOperationKind.EXPORT:
            raise ValueError
        existing = self._authority_prepared_or_terminal(prepared)
        if type(existing) is not PreparedRegistryClaim:
            return existing
        service = self._service(prepared.principal_id, prepared.service_id)
        if (
            service is None or service.service_revision != prepared.expected_service_revision
            or receipt.kind is not ReceiptKind.EXPORT
            or receipt.verification is not ReceiptVerification.VERIFIED
            or receipt.storage_backend is not StorageBackend.SQLITE
            or receipt.principal_id != prepared.principal_id or receipt.service_id != prepared.service_id
            or receipt.service_revision != prepared.expected_service_revision
            or receipt.operational_status != service.operational_status
            or (service.storage is not None and receipt.storage_backend.value != service.storage.backend)
            or self._receipt_exists(receipt.receipt_id)
        ):
            return self._mark_authority_recovery(prepared, receipt.created_at)
        self._insert_receipt(prepared, receipt)
        return self._complete_authority(prepared, receipt, receipt.created_at)

    @_bounded_uow
    def publish_import(
        self, prepared: RegistryAuthorityIntent, service: Service, receipt: TransferReceipt
    ) -> RegistryAuthorityOutcome:
        self._write()
        if type(prepared) is not PortableIntent or prepared.kind is not PortableOperationKind.IMPORT:
            raise ValueError
        existing = self._authority_prepared_or_terminal(prepared)
        if type(existing) is not PreparedRegistryClaim:
            return existing
        identity = prepared.identity
        reservation = self._connection.execute(
            'SELECT * FROM "aipcs_registry_identity" WHERE "service_id"=?', (str(prepared.service_id),)
        ).fetchone()
        if (
            identity is None or reservation is None
            or reservation["identity_state"] != "import_prepared"
            or reservation["principal_id"] != identity.principal_id
            or reservation["domain_name"] != identity.domain_name
            or reservation["storage_namespace"] != identity.logical_namespace
            or reservation["claim_idempotency_key"] != prepared.idempotency_key
            or self._service(identity.principal_id, identity.service_id) is not None
            or service.principal_id != identity.principal_id or service.service_id != identity.service_id
            or service.domain_name != identity.domain_name
            or (service.storage is not None and service.storage.namespace != identity.logical_namespace)
            or (service.storage is not None and service.storage.backend != prepared.destination_backend.value)
            or receipt.kind is not ReceiptKind.IMPORT or receipt.verification is not ReceiptVerification.VERIFIED
            or prepared.destination_backend is not StorageBackend.SQLITE
            or receipt.storage_backend is not StorageBackend.SQLITE
            or receipt.principal_id != identity.principal_id or receipt.service_id != identity.service_id
            or receipt.bundle_root_sha256 != prepared.bundle_root_sha256
            or receipt.storage_backend is not prepared.destination_backend
            or receipt.service_revision != service.service_revision
            or receipt.operational_status != service.operational_status
            or self._receipt_exists(receipt.receipt_id)
        ):
            return self._mark_authority_recovery(prepared, receipt.created_at)
        inserted = self._connection.execute(
            'INSERT INTO "aipcs_registry_service" VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            encode_service_values(service),
        )
        if inserted.rowcount != 1:
            raise ValueError
        published = self._connection.execute(
            'UPDATE "aipcs_registry_identity" SET "identity_state"=\'live\','
            '"storage_backend"=?,"claim_idempotency_key"=NULL,"lifecycle_at"=NULL '
            'WHERE "service_id"=? AND "principal_id"=? AND "identity_state"=\'import_prepared\' '
            'AND "claim_idempotency_key"=?',
            (
                service.storage.backend if service.storage is not None else None,
                str(service.service_id),
                service.principal_id,
                prepared.idempotency_key,
            ),
        )
        if published.rowcount != 1:
            raise ValueError
        self._insert_receipt(prepared, receipt)
        return self._complete_authority(prepared, service, receipt.created_at)

    @_bounded_uow
    def finalize_purge(self, prepared: RegistryAuthorityIntent, at: datetime) -> RegistryAuthorityOutcome:
        self._write()
        if type(prepared) is not PortableIntent or prepared.kind is not PortableOperationKind.PURGE:
            raise ValueError
        existing = self._authority_prepared_or_terminal(prepared)
        if type(existing) is not PreparedRegistryClaim:
            return existing
        service = self._service(prepared.principal_id, prepared.service_id)
        if (
            service is None or service.service_revision != prepared.expected_service_revision
            or service.operational_status != "archived" or at < service.updated_at
            or not self._purge_receipt_is_valid(prepared)
        ):
            return self._mark_authority_recovery(prepared, at)
        assert prepared.purge_authority is not None
        tombstone = PurgeTombstone(
            service.principal_id, service.service_id, f"svc_{service.service_id.hex}", prepared.created_via,
            at, prepared.purge_authority, prepared.purge_authority.receipt_id,
        )
        self._connection.execute(
            'UPDATE "aipcs_registry_claim" SET "phase"=\'tombstoned\',"created_via"=NULL,'
            '"intent_json"=NULL,"result_json"=NULL,"recovery_category"=NULL '
            'WHERE "principal_id"=? AND "service_id"=? AND "idempotency_key"<>?',
            (prepared.principal_id, str(prepared.service_id), prepared.idempotency_key),
        )
        removed = self._connection.execute(
            'DELETE FROM "aipcs_registry_service" WHERE "principal_id"=? AND "service_id"=?',
            (prepared.principal_id, str(prepared.service_id)),
        )
        if removed.rowcount != 1:
            raise ValueError
        tombstoned = self._connection.execute(
            'UPDATE "aipcs_registry_identity" SET "identity_state"=\'tombstoned\','
            '"domain_name"=NULL,"storage_backend"=NULL,"claim_idempotency_key"=?,"lifecycle_at"=? '
            'WHERE "service_id"=? AND "principal_id"=? AND "identity_state"=\'live\'',
            (
                prepared.idempotency_key,
                encode_time(at),
                str(prepared.service_id),
                prepared.principal_id,
            ),
        )
        if tombstoned.rowcount != 1:
            raise ValueError
        return self._complete_authority(prepared, tombstone, at)

    @_bounded_uow
    def mark_recovery_required(
        self, prepared: RegistryAuthorityIntent, at: datetime
    ) -> RegistryAuthorityOutcome:
        self._write()
        existing = self._authority_prepared_or_terminal(prepared)
        if type(existing) is not PreparedRegistryClaim:
            return existing
        return self._mark_authority_recovery(prepared, at)

    def _insert_live_identity(self, service: Service) -> None:
        self._connection.execute(
            'INSERT INTO "aipcs_registry_identity"('
            '"service_id","principal_id","identity_state","domain_name",'
            '"storage_backend","storage_namespace","claim_idempotency_key","lifecycle_at") '
            "VALUES (?,?,'live',?,?,?,NULL,NULL)",
            (
                str(service.service_id), service.principal_id, service.domain_name,
                service.storage.backend if service.storage is not None else None,
                f"svc_{service.service_id.hex}",
            ),
        )

    def _update_live_identity(self, service: Service) -> None:
        cursor = self._connection.execute(
            'UPDATE "aipcs_registry_identity" SET "domain_name"=?,"storage_backend"=? '
            'WHERE "service_id"=? AND "principal_id"=? AND "identity_state"=\'live\'',
            (
                service.domain_name, service.storage.backend if service.storage is not None else None,
                str(service.service_id), service.principal_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError

    def _existing_authority_claim(
        self, row: sqlite3.Row, intent: RegistryAuthorityIntent
    ) -> RegistryAuthorityOutcome:
        if row["operation_kind"] in {"legacy", "seed", "design", "materialise", "evolve"}:
            return RegistryClaimConflict()
        if row["phase"] == "tombstoned":
            value = validate_registry_authority_claim_row(dict(row))
            if type(value) is not TombstonedRegistryClaim:
                raise ValueError
            if (
                value.fingerprint == intent.fingerprint and value.operation_kind == intent.kind.value
                and value.service_id == intent.service_id
            ):
                return value
            return RegistryClaimConflict()
        stored = decode_registry_authority_intent(row["intent_json"])
        if (
            type(stored) is not type(intent) or stored.kind != intent.kind
            or stored.fingerprint != intent.fingerprint
        ):
            return RegistryClaimConflict()
        value = validate_registry_authority_claim_row(dict(row))
        if stored.phase is OperationalLifecyclePhase.PREPARED:
            if value is not stored and value != stored:
                raise ValueError
            return PreparedRegistryClaim(stored)
        if stored.phase is OperationalLifecyclePhase.RECOVERY_REQUIRED:
            if value is not stored and value != stored:
                raise ValueError
            return RecoveryRequiredRegistryClaim(stored)
        result = decode_registry_authority_result(row["result_json"], stored)
        return CompletedRegistryClaim(stored, result)

    def _authority_prepared_or_terminal(
        self, prepared: RegistryAuthorityIntent
    ) -> PreparedRegistryClaim | CompletedRegistryClaim | RecoveryRequiredRegistryClaim:
        if prepared.phase is not OperationalLifecyclePhase.PREPARED:
            raise ValueError
        row = self._claim_row(prepared.principal_id, prepared.idempotency_key)
        if row is None:
            raise ValueError
        current = self._existing_authority_claim(row, prepared)
        if type(current) is RegistryClaimConflict or type(current) is TombstonedRegistryClaim:
            raise ValueError
        return current

    def _authority_blocker(
        self, principal_id: str, service_id: UUID
    ) -> RegistryAuthorityOutcome | None:
        row = self._connection.execute(
            'SELECT * FROM "aipcs_registry_claim" WHERE "principal_id"=? AND "service_id"=? '
            'AND "phase" IN (\'prepared\',\'recovery_required\') LIMIT 1',
            (principal_id, str(service_id)),
        ).fetchone()
        if row is None:
            return None
        if row["operation_kind"] in {"materialise", "evolve"}:
            stored_lifecycle = _decode_legacy_lifecycle_intent(row)
            if stored_lifecycle.phase is LifecyclePhase.RECOVERY_REQUIRED:
                return RecoveryRequiredRegistryClaim(stored_lifecycle)
            return RegistryOperationInProgress(stored_lifecycle)
        stored = decode_registry_authority_intent(row["intent_json"])
        if stored.phase is OperationalLifecyclePhase.RECOVERY_REQUIRED:
            return RecoveryRequiredRegistryClaim(stored)
        return RegistryOperationInProgress(stored)

    def _insert_authority_prepared(self, intent: RegistryAuthorityIntent) -> None:
        self._connection.execute(
            'INSERT INTO "aipcs_registry_claim"('
            '"principal_id","idempotency_key","fingerprint","service_id","operation_kind",'
            '"phase","created_via","intent_json","result_json","recovery_category") '
            "VALUES (?,?,?,?,?,'prepared',?,?,NULL,NULL)",
            (
                intent.principal_id, intent.idempotency_key, intent.fingerprint,
                str(intent.service_id), intent.kind.value, intent.created_via,
                encode_registry_authority_intent(intent),
            ),
        )

    def _complete_authority(
        self, prepared: RegistryAuthorityIntent, result: object, at: datetime
    ) -> CompletedRegistryClaim:
        terminal = prepared.with_phase(OperationalLifecyclePhase.COMPLETED)
        cursor = self._connection.execute(
            'UPDATE "aipcs_registry_claim" SET "phase"=\'completed\',"intent_json"=?,"result_json"=? '
            'WHERE "principal_id"=? AND "idempotency_key"=? AND "fingerprint"=? '
            'AND "phase"=\'prepared\'',
            (
                encode_registry_authority_intent(terminal), encode_registry_authority_result(result), prepared.principal_id,
                prepared.idempotency_key, prepared.fingerprint,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError
        completed = CompletedRegistryClaim(terminal, result)
        self._append_local_event(terminal, "completed", at)
        return completed

    def _mark_authority_recovery(
        self, prepared: RegistryAuthorityIntent, at: datetime
    ) -> RecoveryRequiredRegistryClaim:
        terminal = prepared.with_phase(OperationalLifecyclePhase.RECOVERY_REQUIRED)
        cursor = self._connection.execute(
            'UPDATE "aipcs_registry_claim" SET "phase"=\'recovery_required\','
            '"intent_json"=?,"recovery_category"=\'recovery_required\' WHERE "principal_id"=? '
            'AND "idempotency_key"=? AND "fingerprint"=? AND "phase"=\'prepared\'',
            (encode_registry_authority_intent(terminal), prepared.principal_id,
             prepared.idempotency_key, prepared.fingerprint),
        )
        if cursor.rowcount != 1:
            raise ValueError
        self._append_local_event(terminal, "recovery_required", at)
        return RecoveryRequiredRegistryClaim(terminal)

    def _append_local_event(
        self, intent: RegistryAuthorityIntent, outcome: str, at: datetime
    ) -> None:
        event = LocalRegistryEvent(
            intent.kind.value, outcome, intent.service_id, intent.principal_id, intent.created_via, at
        )
        self._connection.execute(
            'INSERT INTO "aipcs_registry_audit"('
            '"action","outcome","service_id","principal_id","created_via","at") '
            'VALUES (?,?,?,?,?,?)',
            (event.action, event.outcome, str(event.service_id), event.principal_id,
             event.created_via, encode_time(event.at)),
        )
        self._connection.execute(
            'DELETE FROM "aipcs_registry_audit" AS "old" '
            'WHERE "old"."principal_id"=? AND ('
            'SELECT count(*) FROM "aipcs_registry_audit" AS "newer" '
            'WHERE "newer"."principal_id"="old"."principal_id" '
            'AND "newer"."audit_id">"old"."audit_id")>=1000',
            (event.principal_id,),
        )

    def _import_collision(self, intent: PortableIntent) -> RegistryIdentityCollision | None:
        assert intent.identity is not None
        by_service = self._connection.execute(
            'SELECT "identity_state" FROM "aipcs_registry_identity" WHERE "service_id"=?',
            (str(intent.service_id),),
        ).fetchone()
        if by_service is not None:
            kind = IdentityCollisionKind.TOMBSTONE if by_service[0] == "tombstoned" else IdentityCollisionKind.SERVICE_ID
            return RegistryIdentityCollision(kind)
        by_domain = self._connection.execute(
            'SELECT 1 FROM "aipcs_registry_identity" WHERE "principal_id"=? AND "domain_name"=? '
            'AND "identity_state" IN (\'live\',\'import_prepared\')',
            (intent.identity.principal_id, intent.identity.domain_name),
        ).fetchone()
        if by_domain is not None:
            return RegistryIdentityCollision(IdentityCollisionKind.DOMAIN)
        return None

    def _receipt_exists(self, receipt_id: UUID) -> bool:
        return self._connection.execute(
            'SELECT 1 FROM "aipcs_registry_receipt" WHERE "receipt_id"=?', (str(receipt_id),)
        ).fetchone() is not None

    def _insert_receipt(self, intent: PortableIntent, receipt: TransferReceipt) -> None:
        self._connection.execute(
            'INSERT INTO "aipcs_registry_receipt"('
            '"receipt_id","principal_id","idempotency_key","service_id","receipt_kind",'
            '"bundle_root_sha256","export_format_version","storage_backend","service_revision",'
            '"operational_status","verification","created_at") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                str(receipt.receipt_id), receipt.principal_id, intent.idempotency_key,
                str(receipt.service_id), receipt.kind.value, receipt.bundle_root_sha256,
                receipt.export_format_version, receipt.storage_backend.value, receipt.service_revision,
                receipt.operational_status, receipt.verification.value, encode_time(receipt.created_at),
            ),
        )

    def _purge_receipt_is_valid(self, intent: PortableIntent) -> bool:
        if intent.purge_authority is None:
            return False
        if intent.purge_authority.kind is PurgeAuthorityKind.EXPLICIT_OVERRIDE:
            return True
        receipt_id = intent.purge_authority.receipt_id
        if receipt_id is None:
            return False
        row = self._connection.execute(
            'SELECT * FROM "aipcs_registry_receipt" WHERE "receipt_id"=?', (str(receipt_id),)
        ).fetchone()
        return row is not None and (
            row["receipt_kind"] == "export" and row["verification"] == "verified"
            and row["principal_id"] == intent.principal_id and row["service_id"] == str(intent.service_id)
            and row["service_revision"] == intent.expected_service_revision
            and row["operational_status"] == "archived"
        )

    @_bounded_uow
    def append(self, event: AuditEvent) -> None:
        self._write()
        self._connection.execute(
            'INSERT INTO "aipcs_registry_audit"('
            '"action","outcome","service_id","principal_id","created_via","at") '
            "VALUES (?,?,?,?,?,?)",
            (
                event.action,
                event.outcome,
                str(event.service_id),
                event.principal_id,
                event.created_via,
                encode_time(event.at),
            ),
        )
        self._connection.execute(
            'DELETE FROM "aipcs_registry_audit" AS "old" '
            'WHERE "old"."principal_id"=? AND ('
            'SELECT count(*) FROM "aipcs_registry_audit" AS "newer" '
            'WHERE "newer"."principal_id"="old"."principal_id" '
            'AND "newer"."audit_id">"old"."audit_id")>=1000',
            (event.principal_id,),
        )

    @_bounded_uow
    def commit(self) -> None:
        self._open()
        self._verify_live()
        self._connection.commit()
        self._verify_live()
        self._terminal = True

    @_bounded_uow
    def rollback(self) -> None:
        if self._closed or self._terminal:
            return
        self._verify_live()
        self._connection.rollback()
        self._verify_live()
        self._terminal = True

    @_bounded_uow
    def close(self) -> None:
        if self._closed:
            return
        failure: Exception | None = None
        try:
            if not self._terminal and self._connection.in_transaction:
                self._verify_live()
                self._connection.rollback()
                self._verify_live()
            self._verify_live()
        except StorageUnavailable:
            failure = StorageUnavailable()
        except Exception as error:
            failure = StorageBusy() if is_sqlite_busy(error) else StorageMigrationError()
        try:
            self._connection.close()
        except Exception as error:
            if failure is None:
                failure = StorageBusy() if is_sqlite_busy(error) else StorageMigrationError()
        try:
            self._location.verify_database_identity()
            self._location.verify_closed_sidecars(allow_journal=False)
        except Exception:
            failure = StorageUnavailable()
        try:
            self._location.close()
        except Exception:
            if failure is None:
                failure = StorageUnavailable()
        finally:
            self._closed = True
            self._terminal = True
        if failure is not None:
            raise failure


def _encode_legacy_lifecycle_intent(intent: LifecycleIntent) -> str:
    """Preserve R3 lifecycle evidence inside the R4 shared-claim envelope."""

    value = {
        "created_via": intent.created_via,
        "expected_schema_version": intent.expected_schema_version,
        "expected_service_revision": intent.expected_service_revision,
        "kind": intent.kind.value,
        "principal": intent.principal_id,
        "service_id": str(intent.service_id),
        "target_manifest": json.loads(encode_manifest(intent.target_manifest)),
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _decode_legacy_lifecycle_intent(row: sqlite3.Row) -> LifecycleIntent:
    try:
        if row["operation_kind"] not in {"materialise", "evolve"}:
            raise ValueError
        raw = json.loads(row["intent_json"])
        if not isinstance(raw, dict) or set(raw) != {
            "created_via", "expected_schema_version", "expected_service_revision",
            "kind", "principal", "service_id", "target_manifest",
        }:
            raise ValueError
        if json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True) != row["intent_json"]:
            raise ValueError
        intent = LifecycleIntent(
            kind=LifecycleKind(raw["kind"]), phase=LifecyclePhase(row["phase"]),
            principal_id=raw["principal"], created_via=raw["created_via"],
            service_id=UUID(raw["service_id"]),
            expected_service_revision=raw["expected_service_revision"],
            expected_schema_version=raw["expected_schema_version"],
            idempotency_key=row["idempotency_key"], fingerprint=row["fingerprint"],
            target_manifest=ManifestV2.model_validate(raw["target_manifest"]),
        )
        if (
            intent.principal_id != row["principal_id"] or str(intent.service_id) != row["service_id"]
            or intent.kind.value != row["operation_kind"]
        ):
            raise ValueError
        return intent
    except Exception:
        raise StorageMigrationError() from None


def _operational_command(intent: OperationalLifecycleIntent):
    command_types = {
        "suspend": SuspendCommand,
        "resume": ResumeCommand,
        "archive": ArchiveCommand,
        "restore": RestoreCommand,
    }
    return command_types[intent.kind.value](
        intent.principal_id, intent.created_via, intent.service_id,
        intent.expected_service_revision, intent.idempotency_key,
    )
