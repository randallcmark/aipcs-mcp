"""PostgreSQL transaction and registry repositories."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from functools import wraps
from typing import Any
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
from aipcs_mcp.storage.errors import StorageBusy, StorageMigrationError, StorageUnavailable
from aipcs_mcp.storage.registry_authority_codecs import (
    decode_registry_authority_intent,
    decode_registry_authority_result,
    encode_registry_authority_intent,
    encode_registry_authority_result,
    validate_registry_authority_claim_row,
)

from .registry_inspection import canonical_row, canonical_rows
from .result_codes import is_postgresql_busy

_SCHEMA = '"aipcs_registry"'
_SERVICE = f'{_SCHEMA}."aipcs_registry_service"'
_CLAIM = f'{_SCHEMA}."aipcs_registry_claim"'
_IDENTITY = f'{_SCHEMA}."aipcs_registry_identity"'
_RECEIPT = f'{_SCHEMA}."aipcs_registry_receipt"'
_AUDIT = f'{_SCHEMA}."aipcs_registry_audit"'


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
            owner = args[0] if args else None
            commit_started = (
                isinstance(owner, PostgreSQLRegistryUnitOfWork) and owner._commit_started
            )
            if commit_started:
                failure = StorageMigrationError()
            elif is_postgresql_busy(error):
                failure = StorageBusy()
            elif _is_connection_error(error):
                failure = StorageUnavailable()
            else:
                failure = StorageMigrationError()
        raise failure from None

    return wrapped


class PostgreSQLRegistryUnitOfWork:
    """One independent PostgreSQL connection and transaction."""

    def __init__(self, connection: object) -> None:
        if not callable(getattr(connection, "execute", None)):
            raise StorageUnavailable()
        self._connection = connection
        self._closed = False
        self._terminal = False
        self._transaction_started = False
        self._commit_started = False
        self.services = self
        self.mutations = self
        self.audits = self
        self.authority = self

    def __repr__(self) -> str:
        return "PostgreSQLRegistryUnitOfWork(<redacted>)"

    def _open(self) -> None:
        if self._closed or self._terminal:
            raise StorageMigrationError()

    def _read(self) -> None:
        self._open()
        if not self._transaction_started:
            self._execute("BEGIN TRANSACTION ISOLATION LEVEL READ COMMITTED")
            self._transaction_started = True

    def _write(self) -> None:
        self._read()

    @_bounded_uow
    def find_domain(self, principal_id: str, domain_name: str) -> Service | None:
        self._read()
        cursor = self._execute(
            f'SELECT * FROM {_SERVICE} WHERE "principal_id"=%s AND "domain_name"=%s',
            (principal_id, domain_name),
        )
        row = cursor.fetchone()
        return decode_service(canonical_row(cursor, row)) if row is not None else None

    @_bounded_uow
    def get(self, principal_id: str, service_id: UUID) -> Service | None:
        self._read()
        cursor = self._execute(
            f'SELECT * FROM {_SERVICE} WHERE "principal_id"=%s AND "service_id"=%s',
            (principal_id, str(service_id)),
        )
        row = cursor.fetchone()
        return decode_service(canonical_row(cursor, row)) if row is not None else None

    @_bounded_uow
    def count(self, principal_id: str) -> int:
        self._read()
        row = self._execute(
            f'SELECT COUNT(*) FROM {_SERVICE} WHERE "principal_id"=%s',
            (principal_id,),
        ).fetchone()
        if (
            not isinstance(row, (tuple, list))
            or len(row) != 1
            or type(row[0]) is not int
            or row[0] < 0
        ):
            raise StorageMigrationError()
        return row[0]

    @_bounded_uow
    def list(self, principal_id: str, limit: int) -> list[Service]:
        self._read()
        cursor = self._execute(
            f'SELECT * FROM {_SERVICE} WHERE "principal_id"=%s '
            'ORDER BY "created_at" ASC,"service_id" ASC LIMIT %s',
            (principal_id, limit),
        )
        return [decode_service(row) for row in canonical_rows(cursor, cursor.fetchall())]

    @_bounded_uow
    def recovery_state(self, principal_id: str, service_id: UUID) -> RecoveryState:
        self._read()
        cursor = self._execute(
            f'SELECT * FROM {_CLAIM} WHERE "principal_id"=%s AND "service_id"=%s '
            'AND "operation_kind" IN (\'materialise\',\'evolve\',\'suspend\',\'resume\','
            '\'archive\',\'restore\',\'export\',\'import\',\'purge\') '
            'AND "phase" IN (\'prepared\',\'recovery_required\')',
            (principal_id, str(service_id)),
        )
        rows = canonical_rows(cursor, cursor.fetchall())
        if not rows:
            return RecoveryState.CLEAR
        if len(rows) != 1:
            raise StorageMigrationError()
        row = rows[0]
        if row["operation_kind"] in {"materialise", "evolve"}:
            phase = _decode_legacy_lifecycle_intent(row).phase.value
        else:
            phase = decode_registry_authority_intent(
                _canonical_claim_row(row)["intent_json"]
            ).phase.value
        if phase == "prepared":
            return RecoveryState.PENDING
        if phase == "recovery_required":
            return RecoveryState.RECOVERY_REQUIRED
        raise StorageMigrationError()

    @_bounded_uow
    def add(self, service: Service) -> None:
        self._write()
        self._execute(
            f"INSERT INTO {_SERVICE} VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            encode_service_values(service),
        )
        self._insert_live_identity(service)

    @_bounded_uow
    def save(self, service: Service, expected_service_revision: int) -> ServiceSaveResult:
        self._write()
        if (
            type(expected_service_revision) is not int
            or not 1 <= expected_service_revision < (1 << 63) - 1
            or service.service_revision != expected_service_revision + 1
        ):
            raise ValueError
        values = encode_service_values(service)
        cursor = self._execute(
            f'UPDATE {_SERVICE} SET "domain_name"=%s,"domain_class"=%s,'
            '"intent_description"=%s,"created_at"=%s,"updated_at"=%s,'
            '"last_activity_at"=%s,"manifest_json"=%s,"schema_version"=%s,'
            '"design_state"=%s,"operational_status"=%s,"service_revision"=%s,'
            '"materialised_at"=%s,"storage_backend"=%s,"storage_namespace"=%s '
            'WHERE "service_id"=%s AND "principal_id"=%s AND "service_revision"=%s',
            values[2:] + values[:2] + (expected_service_revision,),
        )
        if cursor.rowcount == 1:
            self._update_live_identity(service)
            return "saved"
        if cursor.rowcount == 0:
            return "stale_revision"
        raise StorageMigrationError()

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
        self._lock("claim", principal_id, idempotency_key)
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
        self._lock("claim", principal_id, idempotency_key)
        self._execute(
            f'INSERT INTO {_CLAIM}('
            '"principal_id","idempotency_key","fingerprint","service_id","operation_kind",'
            '"phase","created_via","intent_json","result_json","recovery_category") '
            "VALUES (%s,%s,%s,%s,%s,'completed',NULL,NULL,%s::jsonb,NULL)",
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
        self._lock("claim", command.principal_id, command.idempotency_key)
        self._lock("lifecycle", command.principal_id, str(command.service_id))
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

        cursor = self._execute(
            f'SELECT * FROM {_CLAIM} WHERE "principal_id"=%s AND "service_id"=%s '
            'AND "operation_kind" IN (\'materialise\',\'evolve\',\'suspend\',\'resume\','
            '\'archive\',\'restore\',\'export\',\'import\',\'purge\') '
            'AND "phase" IN (\'prepared\',\'recovery_required\') LIMIT 1',
            (command.principal_id, str(command.service_id)),
        )
        blocker = cursor.fetchone()
        if blocker is not None:
            row = _canonical_claim_row(canonical_row(cursor, blocker))
            if row["operation_kind"] not in {"materialise", "evolve"}:
                decode_registry_authority_intent(row["intent_json"])
                return OperationInProgress()
            intent = _decode_legacy_lifecycle_intent(row)
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
        self._lock("claim", intent.principal_id, intent.idempotency_key)
        self._lock("lifecycle", intent.principal_id, str(intent.service_id))
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
        cursor = self._execute(
            f'UPDATE {_CLAIM} SET "phase"=\'completed\',"result_json"=%s::jsonb '
            'WHERE "principal_id"=%s AND "idempotency_key"=%s AND "fingerprint"=%s '
            'AND "phase"=\'prepared\'',
            (
                encode_result(terminal),
                intent.principal_id,
                intent.idempotency_key,
                intent.fingerprint,
            ),
        )
        if cursor.rowcount != 1:
            raise StorageMigrationError()
        self._append_terminal(intent, "completed", completion.at)
        return CompletedLifecycleClaim(terminal_intent, terminal)

    @_bounded_uow
    def finalize_recovery_required(
        self, prepared_intent: LifecycleIntent, at: datetime
    ) -> CompletedLifecycleClaim | RecoveryRequiredLifecycleClaim:
        self._write()
        encode_time(at)
        self._lock("claim", prepared_intent.principal_id, prepared_intent.idempotency_key)
        self._lock(
            "lifecycle",
            prepared_intent.principal_id,
            str(prepared_intent.service_id),
        )
        existing = self._terminal_or_prepared(prepared_intent)
        if type(existing) in {CompletedLifecycleClaim, RecoveryRequiredLifecycleClaim}:
            return existing
        return self._mark_recovery_required(prepared_intent, at)

    def _claim_row(self, principal_id: str, idempotency_key: str) -> dict[str, Any] | None:
        cursor = self._execute(
            f'SELECT * FROM {_CLAIM} '
            'WHERE "principal_id"=%s AND "idempotency_key"=%s',
            (principal_id, idempotency_key),
        )
        row = cursor.fetchone()
        return None if row is None else _canonical_claim_row(canonical_row(cursor, row))

    def _service(self, principal_id: str, service_id: UUID) -> Service | None:
        cursor = self._execute(
            f'SELECT * FROM {_SERVICE} WHERE "principal_id"=%s AND "service_id"=%s',
            (principal_id, str(service_id)),
        )
        row = cursor.fetchone()
        return None if row is None else decode_service(canonical_row(cursor, row))

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
        self,
        row: Mapping[str, Any],
        command: LifecycleCommand,
        fingerprint: str,
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
        self._execute(
            f'INSERT INTO {_CLAIM}('
            '"principal_id","idempotency_key","fingerprint","service_id","operation_kind",'
            '"phase","created_via","intent_json","result_json","recovery_category") '
            "VALUES (%s,%s,%s,%s,%s,'prepared',%s,%s::jsonb,NULL,NULL)",
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
        cursor = self._execute(
            f'UPDATE {_CLAIM} SET "phase"=\'recovery_required\','
            '"recovery_category"=\'recovery_required\' WHERE "principal_id"=%s '
            'AND "idempotency_key"=%s AND "fingerprint"=%s AND "phase"=\'prepared\'',
            (intent.principal_id, intent.idempotency_key, intent.fingerprint),
        )
        if cursor.rowcount != 1:
            raise StorageMigrationError()
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
        self._lock("claim", intent.principal_id, intent.idempotency_key)
        # Service-scoped locks serialize state transitions; import additionally
        # locks its global identity and principal/domain allocation below.
        self._lock("service", intent.principal_id, str(intent.service_id))
        row = self._claim_row(intent.principal_id, intent.idempotency_key)
        if row is not None:
            return self._existing_authority_claim(row, intent)
        if type(intent) is PortableIntent and intent.kind is PortableOperationKind.IMPORT:
            if intent.destination_backend is not StorageBackend.POSTGRESQL:
                return RegistryUnsupportedTransition()
            if intent.identity is None:
                raise StorageMigrationError()
            self._lock("identity", "service", str(intent.service_id))
            self._lock("identity", intent.identity.principal_id, intent.identity.domain_name)
            collision = self._import_collision(intent)
            if collision is not None:
                return collision
            self._insert_authority_prepared(intent)
            self._execute(
                f'INSERT INTO {_IDENTITY}("service_id","principal_id","domain_name",'
                '"storage_backend","storage_namespace","identity_state",'
                '"claim_idempotency_key","lifecycle_at") '
                "VALUES (%s,%s,%s,%s,%s,'import_prepared',%s,%s)",
                (str(intent.service_id), intent.principal_id, intent.identity.domain_name,
                 intent.destination_backend.value if intent.destination_backend is not None else None,
                 intent.identity.logical_namespace, intent.idempotency_key, encode_time(datetime.now(UTC))),
            )
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
    def finalize_operational(self, prepared: RegistryAuthorityIntent, at: datetime) -> RegistryAuthorityOutcome:
        self._write()
        if type(prepared) is not OperationalLifecycleIntent:
            raise ValueError
        self._lock("claim", prepared.principal_id, prepared.idempotency_key)
        self._lock("service", prepared.principal_id, str(prepared.service_id))
        existing = self._authority_prepared_or_terminal(prepared)
        if type(existing) is not PreparedRegistryClaim:
            return existing
        service = self._service(prepared.principal_id, prepared.service_id)
        try:
            if service is None or service.service_revision != prepared.expected_service_revision or at < service.updated_at:
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
    def complete_export(self, prepared: RegistryAuthorityIntent, receipt: TransferReceipt) -> RegistryAuthorityOutcome:
        self._write()
        if type(prepared) is not PortableIntent or prepared.kind is not PortableOperationKind.EXPORT:
            raise ValueError
        self._lock("claim", prepared.principal_id, prepared.idempotency_key)
        self._lock("service", prepared.principal_id, str(prepared.service_id))
        existing = self._authority_prepared_or_terminal(prepared)
        if type(existing) is not PreparedRegistryClaim:
            return existing
        service = self._service(prepared.principal_id, prepared.service_id)
        if (
            service is None or service.service_revision != prepared.expected_service_revision
            or receipt.kind is not ReceiptKind.EXPORT or receipt.verification is not ReceiptVerification.VERIFIED
            or receipt.storage_backend is not StorageBackend.POSTGRESQL
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
        self._lock("claim", prepared.principal_id, prepared.idempotency_key)
        self._lock("service", prepared.principal_id, str(prepared.service_id))
        existing = self._authority_prepared_or_terminal(prepared)
        if type(existing) is not PreparedRegistryClaim:
            return existing
        identity = prepared.identity
        cursor = self._execute(f'SELECT * FROM {_IDENTITY} WHERE "service_id"=%s', (str(prepared.service_id),))
        raw = cursor.fetchone()
        reservation = None if raw is None else canonical_row(cursor, raw)
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
            or (
                service.storage is not None
                and (
                    prepared.destination_backend is None
                    or service.storage.backend != prepared.destination_backend.value
                )
            )
            or receipt.kind is not ReceiptKind.IMPORT or receipt.verification is not ReceiptVerification.VERIFIED
            or receipt.principal_id != identity.principal_id or receipt.service_id != identity.service_id
            or receipt.bundle_root_sha256 != prepared.bundle_root_sha256
            or receipt.storage_backend is not prepared.destination_backend
            or prepared.destination_backend is not StorageBackend.POSTGRESQL
            or receipt.service_revision != service.service_revision or receipt.operational_status != service.operational_status
            or self._receipt_exists(receipt.receipt_id)
        ):
            return self._mark_authority_recovery(prepared, receipt.created_at)
        cursor = self._execute(
            f"INSERT INTO {_SERVICE} VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            encode_service_values(service),
        )
        if cursor.rowcount != 1:
            raise StorageMigrationError()
        cursor = self._execute(
            f'UPDATE {_IDENTITY} SET "identity_state"=\'live\',"storage_backend"=%s,'
            '"claim_idempotency_key"=NULL,"lifecycle_at"=NULL '
            'WHERE "service_id"=%s AND "identity_state"=\'import_prepared\'',
            (service.storage.backend if service.storage is not None else None, str(service.service_id)),
        )
        if cursor.rowcount != 1:
            raise StorageMigrationError()
        self._insert_receipt(prepared, receipt)
        return self._complete_authority(prepared, service, receipt.created_at)

    @_bounded_uow
    def finalize_purge(self, prepared: RegistryAuthorityIntent, at: datetime) -> RegistryAuthorityOutcome:
        self._write()
        if type(prepared) is not PortableIntent or prepared.kind is not PortableOperationKind.PURGE:
            raise ValueError
        self._lock("claim", prepared.principal_id, prepared.idempotency_key)
        self._lock("service", prepared.principal_id, str(prepared.service_id))
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
        if prepared.purge_authority is None:
            return self._mark_authority_recovery(prepared, at)
        tombstone = PurgeTombstone(
            service.principal_id, service.service_id, f"svc_{service.service_id.hex}", prepared.created_via,
            at, prepared.purge_authority, prepared.purge_authority.receipt_id,
        )
        self._execute(
            f'DELETE FROM {_RECEIPT} WHERE "principal_id"=%s AND "service_id"=%s',
            (prepared.principal_id, str(prepared.service_id)),
        )
        self._execute(
            f'UPDATE {_CLAIM} SET "phase"=\'tombstoned\',"created_via"=NULL,'
            '"intent_json"=NULL,"result_json"=NULL,"recovery_category"=NULL '
            'WHERE "principal_id"=%s AND "service_id"=%s AND "idempotency_key"<>%s',
            (prepared.principal_id, str(prepared.service_id), prepared.idempotency_key),
        )
        cursor = self._execute(
            f'DELETE FROM {_SERVICE} WHERE "principal_id"=%s AND "service_id"=%s',
            (prepared.principal_id, str(prepared.service_id)),
        )
        if cursor.rowcount != 1:
            raise StorageMigrationError()
        cursor = self._execute(
            f'UPDATE {_IDENTITY} SET "identity_state"=\'tombstoned\',"domain_name"=NULL,'
            '"storage_backend"=NULL,"claim_idempotency_key"=%s,"lifecycle_at"=%s '
            'WHERE "service_id"=%s',
            (prepared.idempotency_key, encode_time(at), str(prepared.service_id)),
        )
        if cursor.rowcount != 1:
            raise StorageMigrationError()
        return self._complete_authority(prepared, tombstone, at)

    @_bounded_uow
    def mark_recovery_required(
        self, prepared: RegistryAuthorityIntent, at: datetime
    ) -> RegistryAuthorityOutcome:
        self._write()
        self._lock("claim", prepared.principal_id, prepared.idempotency_key)
        self._lock("service", prepared.principal_id, str(prepared.service_id))
        existing = self._authority_prepared_or_terminal(prepared)
        if type(existing) is not PreparedRegistryClaim:
            return existing
        return self._mark_authority_recovery(prepared, at)

    def _insert_live_identity(self, service: Service) -> None:
        self._execute(
            f'INSERT INTO {_IDENTITY}("service_id","principal_id","domain_name",'
            '"storage_backend","storage_namespace","identity_state",'
            '"claim_idempotency_key","lifecycle_at") '
            "VALUES (%s,%s,%s,%s,%s,'live',NULL,NULL)",
            (str(service.service_id), service.principal_id, service.domain_name,
             service.storage.backend if service.storage is not None else None,
             f"svc_{service.service_id.hex}"),
        )

    def _update_live_identity(self, service: Service) -> None:
        cursor = self._execute(
            f'UPDATE {_IDENTITY} SET "domain_name"=%s,"storage_backend"=%s '
            'WHERE "service_id"=%s AND "principal_id"=%s AND "identity_state"=\'live\'',
            (service.domain_name, service.storage.backend if service.storage is not None else None,
             str(service.service_id), service.principal_id),
        )
        if cursor.rowcount != 1:
            raise StorageMigrationError()

    def _existing_authority_claim(
        self, row: Mapping[str, Any], intent: RegistryAuthorityIntent
    ) -> RegistryAuthorityOutcome:
        if row["operation_kind"] in {"legacy", "seed", "design", "materialise", "evolve"}:
            return RegistryClaimConflict()
        row = _canonical_claim_row(row)
        if row["phase"] == "tombstoned":
            value = validate_registry_authority_claim_row(row)
            if type(value) is not TombstonedRegistryClaim:
                raise StorageMigrationError()
            if value.fingerprint == intent.fingerprint and value.operation_kind == intent.kind.value and value.service_id == intent.service_id:
                return value
            return RegistryClaimConflict()
        stored = decode_registry_authority_intent(row["intent_json"])
        if type(stored) is not type(intent) or stored.kind != intent.kind or stored.fingerprint != intent.fingerprint:
            return RegistryClaimConflict()
        value = validate_registry_authority_claim_row(row)
        if stored.phase is OperationalLifecyclePhase.PREPARED:
            if value != stored:
                raise StorageMigrationError()
            return PreparedRegistryClaim(stored)
        if stored.phase is OperationalLifecyclePhase.RECOVERY_REQUIRED:
            if value != stored:
                raise StorageMigrationError()
            return RecoveryRequiredRegistryClaim(stored)
        return CompletedRegistryClaim(stored, decode_registry_authority_result(row["result_json"], stored))

    def _authority_prepared_or_terminal(
        self, prepared: RegistryAuthorityIntent
    ) -> PreparedRegistryClaim | CompletedRegistryClaim | RecoveryRequiredRegistryClaim:
        if prepared.phase is not OperationalLifecyclePhase.PREPARED:
            raise ValueError
        row = self._claim_row(prepared.principal_id, prepared.idempotency_key)
        if row is None:
            raise ValueError
        current = self._existing_authority_claim(row, prepared)
        if type(current) in {RegistryClaimConflict, TombstonedRegistryClaim}:
            raise ValueError
        return current

    def _authority_blocker(self, principal_id: str, service_id: UUID) -> RegistryAuthorityOutcome | None:
        cursor = self._execute(
            f'SELECT * FROM {_CLAIM} WHERE "principal_id"=%s AND "service_id"=%s '
            'AND "operation_kind" IN (\'materialise\',\'evolve\',\'suspend\',\'resume\','
            '\'archive\',\'restore\',\'export\',\'import\',\'purge\') '
            'AND "phase" IN (\'prepared\',\'recovery_required\') LIMIT 1',
            (principal_id, str(service_id)),
        )
        raw = cursor.fetchone()
        if raw is None:
            return None
        row = _canonical_claim_row(canonical_row(cursor, raw))
        if row["operation_kind"] in {"materialise", "evolve"}:
            legacy = _decode_legacy_lifecycle_intent(row)
            if legacy.phase is LifecyclePhase.RECOVERY_REQUIRED:
                return RecoveryRequiredRegistryClaim(legacy)
            return RegistryOperationInProgress(legacy)
        stored = decode_registry_authority_intent(row["intent_json"])
        if stored.phase is OperationalLifecyclePhase.RECOVERY_REQUIRED:
            return RecoveryRequiredRegistryClaim(stored)
        return RegistryOperationInProgress(stored)

    def _insert_authority_prepared(self, intent: RegistryAuthorityIntent) -> None:
        self._execute(
            f'INSERT INTO {_CLAIM}("principal_id","idempotency_key","fingerprint","service_id",'
            '"operation_kind","phase","created_via","intent_json","result_json","recovery_category") '
            "VALUES (%s,%s,%s,%s,%s,'prepared',%s,%s::jsonb,NULL,NULL)",
            (intent.principal_id, intent.idempotency_key, intent.fingerprint, str(intent.service_id),
             intent.kind.value, intent.created_via, encode_registry_authority_intent(intent)),
        )

    def _complete_authority(
        self, prepared: RegistryAuthorityIntent, result: object, at: datetime
    ) -> CompletedRegistryClaim:
        terminal = prepared.with_phase(OperationalLifecyclePhase.COMPLETED)
        cursor = self._execute(
            f'UPDATE {_CLAIM} SET "phase"=\'completed\',"intent_json"=%s::jsonb,'
            '"result_json"=%s::jsonb '
            'WHERE "principal_id"=%s AND "idempotency_key"=%s AND "fingerprint"=%s '
            'AND "phase"=\'prepared\'',
            (encode_registry_authority_intent(terminal), encode_registry_authority_result(result),
             prepared.principal_id,
             prepared.idempotency_key, prepared.fingerprint),
        )
        if cursor.rowcount != 1:
            raise StorageMigrationError()
        completed = CompletedRegistryClaim(terminal, result)
        self._append_local_event(terminal, "completed", at)
        return completed

    def _mark_authority_recovery(
        self, prepared: RegistryAuthorityIntent, at: datetime
    ) -> RecoveryRequiredRegistryClaim:
        terminal = prepared.with_phase(OperationalLifecyclePhase.RECOVERY_REQUIRED)
        cursor = self._execute(
            f'UPDATE {_CLAIM} SET "phase"=\'recovery_required\',"intent_json"=%s::jsonb,'
            '"recovery_category"=\'recovery_required\' '
            'WHERE "principal_id"=%s AND "idempotency_key"=%s AND "fingerprint"=%s AND "phase"=\'prepared\'',
            (encode_registry_authority_intent(terminal), prepared.principal_id,
             prepared.idempotency_key, prepared.fingerprint),
        )
        if cursor.rowcount != 1:
            raise StorageMigrationError()
        self._append_local_event(terminal, "recovery_required", at)
        return RecoveryRequiredRegistryClaim(terminal)

    def _append_local_event(self, intent: RegistryAuthorityIntent, outcome: str, at: datetime) -> None:
        event = LocalRegistryEvent(intent.kind.value, outcome, intent.service_id, intent.principal_id, intent.created_via, at)
        self.append(AuditEvent(event.action, event.outcome, event.service_id, event.principal_id, event.created_via, event.at))

    def _import_collision(self, intent: PortableIntent) -> RegistryIdentityCollision | None:
        if intent.identity is None:
            raise StorageMigrationError()
        cursor = self._execute(f'SELECT "identity_state" FROM {_IDENTITY} WHERE "service_id"=%s', (str(intent.service_id),))
        by_service = cursor.fetchone()
        if by_service is not None:
            state = by_service[0]
            return RegistryIdentityCollision(IdentityCollisionKind.TOMBSTONE if state == "tombstoned" else IdentityCollisionKind.SERVICE_ID)
        cursor = self._execute(
            f'SELECT 1 FROM {_IDENTITY} WHERE "principal_id"=%s AND "domain_name"=%s '
            'AND "identity_state" IN (\'live\',\'import_prepared\')',
            (intent.identity.principal_id, intent.identity.domain_name),
        )
        return RegistryIdentityCollision(IdentityCollisionKind.DOMAIN) if cursor.fetchone() is not None else None

    def _receipt_exists(self, receipt_id: UUID) -> bool:
        return self._execute(f'SELECT 1 FROM {_RECEIPT} WHERE "receipt_id"=%s', (str(receipt_id),)).fetchone() is not None

    def _insert_receipt(self, intent: PortableIntent, receipt: TransferReceipt) -> None:
        self._execute(
            f'INSERT INTO {_RECEIPT}("receipt_id","principal_id","idempotency_key","service_id",'
            '"receipt_kind","bundle_root_sha256","export_format_version","storage_backend",'
            '"service_revision","operational_status","verification","created_at") '
            'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (str(receipt.receipt_id), receipt.principal_id, intent.idempotency_key, str(receipt.service_id),
             receipt.kind.value, receipt.bundle_root_sha256, receipt.export_format_version,
             receipt.storage_backend.value, receipt.service_revision, receipt.operational_status,
             receipt.verification.value, encode_time(receipt.created_at)),
        )

    def _purge_receipt_is_valid(self, intent: PortableIntent) -> bool:
        if intent.purge_authority is None:
            return False
        if intent.purge_authority.kind is PurgeAuthorityKind.EXPLICIT_OVERRIDE:
            return True
        receipt_id = intent.purge_authority.receipt_id
        if receipt_id is None:
            return False
        cursor = self._execute(f'SELECT * FROM {_RECEIPT} WHERE "receipt_id"=%s', (str(receipt_id),))
        raw = cursor.fetchone()
        if raw is None:
            return False
        row = canonical_row(cursor, raw)
        return (
            row["receipt_kind"] == "export" and row["verification"] == "verified"
            and row["principal_id"] == intent.principal_id and str(row["service_id"]) == str(intent.service_id)
            and row["service_revision"] == intent.expected_service_revision and row["operational_status"] == "archived"
        )

    @_bounded_uow
    def append(self, event: AuditEvent) -> None:
        self._write()
        self._execute(
            f'INSERT INTO {_AUDIT}('
            '"action","outcome","service_id","principal_id","created_via","at") '
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (
                event.action,
                event.outcome,
                str(event.service_id),
                event.principal_id,
                event.created_via,
                encode_time(event.at),
            ),
        )
        self._execute(
            f'DELETE FROM {_AUDIT} AS "old" WHERE "old"."principal_id"=%s AND ('
            f'SELECT count(*) FROM {_AUDIT} AS "newer" '
            'WHERE "newer"."principal_id"="old"."principal_id" '
            'AND "newer"."audit_id">"old"."audit_id")>=1000',
            (event.principal_id,),
        )

    @_bounded_uow
    def commit(self) -> None:
        self._open()
        commit = getattr(self._connection, "commit", None)
        if not callable(commit):
            raise StorageMigrationError()
        self._commit_started = True
        commit()
        self._terminal = True

    @_bounded_uow
    def rollback(self) -> None:
        if self._closed or self._terminal:
            return
        rollback = getattr(self._connection, "rollback", None)
        if not callable(rollback):
            raise StorageMigrationError()
        rollback()
        self._terminal = True

    @_bounded_uow
    def close(self) -> None:
        if self._closed:
            return
        failure: Exception | None = None
        try:
            if not self._terminal and self._transaction_started:
                rollback = getattr(self._connection, "rollback", None)
                if not callable(rollback):
                    raise StorageMigrationError()
                rollback()
        except Exception as error:
            failure = error
        try:
            close = getattr(self._connection, "close", None)
            if not callable(close):
                raise StorageMigrationError()
            close()
        except Exception as error:
            if failure is None:
                failure = error
        finally:
            self._closed = True
            self._terminal = True
        if failure is not None:
            raise failure

    def _lock(self, scope: str, first: str, second: str) -> None:
        self._execute(
            "SELECT pg_catalog.pg_advisory_xact_lock("
            "pg_catalog.hashtext(%s),pg_catalog.hashtext(%s))",
            (f"aipcs.registry.{scope}.{first}", second),
        ).fetchone()

    def _execute(self, sql: str, params: tuple[object, ...] = ()) -> Any:
        execute = getattr(self._connection, "execute", None)
        if not callable(execute):
            raise StorageMigrationError()
        return execute(sql, params)


def _canonical_json(value: object) -> str:
    """Recover the exact codec representation from PostgreSQL jsonb values."""

    if type(value) is str:
        value = json.loads(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _canonical_claim_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in ("intent_json", "result_json"):
        if result.get(key) is not None:
            result[key] = _canonical_json(result[key])
    return result


def _encode_legacy_lifecycle_intent(intent: LifecycleIntent) -> str:
    """Keep R1 lifecycle evidence valid after the claim-ledger migration."""

    value = {
        "created_via": intent.created_via,
        "expected_schema_version": intent.expected_schema_version,
        "expected_service_revision": intent.expected_service_revision,
        "kind": intent.kind.value,
        "principal": intent.principal_id,
        "service_id": str(intent.service_id),
        "target_manifest": json.loads(encode_manifest(intent.target_manifest)),
    }
    return _canonical_json(value)


def _decode_legacy_lifecycle_intent(row: Mapping[str, Any]) -> LifecycleIntent:
    try:
        if row["operation_kind"] not in {"materialise", "evolve"}:
            raise ValueError
        raw = json.loads(_canonical_json(row["intent_json"]))
        if set(raw) != {
            "created_via", "expected_schema_version", "expected_service_revision",
            "kind", "principal", "service_id", "target_manifest",
        }:
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
            intent.principal_id != row["principal_id"]
            or str(intent.service_id) != str(row["service_id"])
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


def _is_connection_error(error: BaseException) -> bool:
    state = getattr(error, "sqlstate", None)
    return type(state) is str and state.startswith("08")
