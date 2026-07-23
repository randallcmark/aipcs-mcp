"""SQLite transaction and registry repositories."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
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
from aipcs_mcp.lifecycle import (
    EvolveCommand,
    LifecycleCommand,
    LifecycleIntent,
    LifecyclePhase,
    MaterialiseCommand,
    lifecycle_fingerprint,
    prepare_intent,
)
from aipcs_mcp.storage.errors import (
    StorageBusy,
    StorageMigrationError,
    StorageUnavailable,
)

from .codecs import (
    decode_legacy_result,
    decode_lifecycle_intent,
    decode_result,
    decode_service,
    encode_manifest,
    encode_result,
    encode_service_values,
    encode_time,
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
    def list(self, principal_id: str, limit: int) -> list[Service]:
        self._read()
        rows = self._connection.execute(
            'SELECT * FROM "aipcs_registry_service" WHERE "principal_id"=? '
            'ORDER BY "created_at" ASC, "service_id" ASC LIMIT ?',
            (principal_id, limit),
        ).fetchall()
        return [decode_service(row) for row in rows]

    @_bounded_uow
    def add(self, service: Service) -> None:
        self._write()
        self._connection.execute(
            'INSERT INTO "aipcs_registry_service" VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            encode_service_values(service),
        )

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
            'INSERT INTO "aipcs_registry_mutation"('
            '"principal_id","idempotency_key","fingerprint","service_id","operation_kind",'
            '"phase","created_via","expected_service_revision","expected_schema_version",'
            '"target_manifest_json","result_json","recovery_category") '
            "VALUES (?,?,?,?,?,'completed',NULL,NULL,NULL,NULL,?,NULL)",
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
            'SELECT * FROM "aipcs_registry_mutation" WHERE "principal_id"=? '
            'AND "service_id"=? AND "operation_kind" IN (\'materialise\',\'evolve\') '
            'AND "phase" IN (\'prepared\',\'recovery_required\') LIMIT 1',
            (command.principal_id, str(command.service_id)),
        ).fetchone()
        if blocker is not None:
            intent = decode_lifecycle_intent(blocker)
            if intent.phase is LifecyclePhase.RECOVERY_REQUIRED:
                return RecoveryRequiredLifecycleClaim(intent)
            return OperationInProgress()

        target = service.manifest if type(command) is MaterialiseCommand else command.target_manifest
        if target is None:
            raise ValueError
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
            'UPDATE "aipcs_registry_mutation" SET "phase"=\'completed\',"result_json"=? '
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
            'SELECT * FROM "aipcs_registry_mutation" '
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
        intent = decode_lifecycle_intent(row)
        if intent.phase is LifecyclePhase.PREPARED:
            return PreparedLifecycleClaim(intent)
        if intent.phase is LifecyclePhase.RECOVERY_REQUIRED:
            return RecoveryRequiredLifecycleClaim(intent)
        service = decode_result(row["result_json"], intent.principal_id, str(intent.service_id))
        return CompletedLifecycleClaim(intent, service)

    def _insert_prepared(self, intent: LifecycleIntent) -> None:
        self._connection.execute(
            'INSERT INTO "aipcs_registry_mutation"('
            '"principal_id","idempotency_key","fingerprint","service_id","operation_kind",'
            '"phase","created_via","expected_service_revision","expected_schema_version",'
            '"target_manifest_json","result_json","recovery_category") '
            "VALUES (?,?,?,?,?,'prepared',?,?,?,?,NULL,NULL)",
            (
                intent.principal_id,
                intent.idempotency_key,
                intent.fingerprint,
                str(intent.service_id),
                intent.kind.value,
                intent.created_via,
                intent.expected_service_revision,
                intent.expected_schema_version,
                encode_manifest(intent.target_manifest),
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
        stored = decode_lifecycle_intent(row)
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
            'UPDATE "aipcs_registry_mutation" SET "phase"=\'recovery_required\','
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
