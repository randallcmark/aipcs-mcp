"""PostgreSQL transaction and registry repositories."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
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
from aipcs_mcp.lifecycle import (
    EvolveCommand,
    LifecycleCommand,
    LifecycleIntent,
    LifecyclePhase,
    MaterialiseCommand,
    RecoveryState,
    lifecycle_fingerprint,
    prepare_intent,
)
from aipcs_mcp.relational import RelationalContractError, classify_transition, compile_manifest
from aipcs_mcp.storage.codecs import (
    decode_legacy_result,
    decode_lifecycle_intent,
    decode_result,
    decode_service,
    encode_manifest,
    encode_result,
    encode_service_values,
    encode_time,
)
from aipcs_mcp.storage.errors import StorageBusy, StorageMigrationError, StorageUnavailable

from .registry_inspection import canonical_row, canonical_rows
from .result_codes import is_postgresql_busy

_SCHEMA = '"aipcs_registry"'
_SERVICE = f'{_SCHEMA}."aipcs_registry_service"'
_MUTATION = f'{_SCHEMA}."aipcs_registry_mutation"'
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
            f'SELECT * FROM {_MUTATION} WHERE "principal_id"=%s AND "service_id"=%s '
            'AND "operation_kind" IN (\'materialise\',\'evolve\') '
            'AND "phase" IN (\'prepared\',\'recovery_required\')',
            (principal_id, str(service_id)),
        )
        rows = canonical_rows(cursor, cursor.fetchall())
        if not rows:
            return RecoveryState.CLEAR
        if len(rows) != 1:
            raise StorageMigrationError()
        intent = decode_lifecycle_intent(rows[0])
        if intent.phase is LifecyclePhase.PREPARED:
            return RecoveryState.PENDING
        if intent.phase is LifecyclePhase.RECOVERY_REQUIRED:
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
        self._lock("non-lifecycle", principal_id, idempotency_key)
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
        self._execute(
            f'INSERT INTO {_MUTATION}('
            '"principal_id","idempotency_key","fingerprint","service_id","operation_kind",'
            '"phase","created_via","expected_service_revision","expected_schema_version",'
            '"target_manifest_json","result_json","recovery_category") '
            "VALUES (%s,%s,%s,%s,%s,'completed',NULL,NULL,NULL,NULL,%s,NULL)",
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
            f'SELECT * FROM {_MUTATION} WHERE "principal_id"=%s AND "service_id"=%s '
            'AND "operation_kind" IN (\'materialise\',\'evolve\') '
            'AND "phase" IN (\'prepared\',\'recovery_required\') LIMIT 1',
            (command.principal_id, str(command.service_id)),
        )
        blocker = cursor.fetchone()
        if blocker is not None:
            intent = decode_lifecycle_intent(canonical_row(cursor, blocker))
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
            f'UPDATE {_MUTATION} SET "phase"=\'completed\',"result_json"=%s '
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
            f'SELECT * FROM {_MUTATION} '
            'WHERE "principal_id"=%s AND "idempotency_key"=%s',
            (principal_id, idempotency_key),
        )
        row = cursor.fetchone()
        return None if row is None else canonical_row(cursor, row)

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
        intent = decode_lifecycle_intent(row)
        if intent.phase is LifecyclePhase.PREPARED:
            return PreparedLifecycleClaim(intent)
        if intent.phase is LifecyclePhase.RECOVERY_REQUIRED:
            return RecoveryRequiredLifecycleClaim(intent)
        service = decode_result(row["result_json"], intent.principal_id, str(intent.service_id))
        return CompletedLifecycleClaim(intent, service)

    def _insert_prepared(self, intent: LifecycleIntent) -> None:
        self._execute(
            f'INSERT INTO {_MUTATION}('
            '"principal_id","idempotency_key","fingerprint","service_id","operation_kind",'
            '"phase","created_via","expected_service_revision","expected_schema_version",'
            '"target_manifest_json","result_json","recovery_category") '
            "VALUES (%s,%s,%s,%s,%s,'prepared',%s,%s,%s,%s,NULL,NULL)",
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
        cursor = self._execute(
            f'UPDATE {_MUTATION} SET "phase"=\'recovery_required\','
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


def _is_connection_error(error: BaseException) -> bool:
    state = getattr(error, "sqlstate", None)
    return type(state) is str and state.startswith("08")
