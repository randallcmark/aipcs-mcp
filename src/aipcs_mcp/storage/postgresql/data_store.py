"""Private PostgreSQL CRUD/history adapter for an already materialised service.

The adapter composes CRUD/history and topology in one caller-owned transaction
boundary. Discovery and runtime composition remain separate slices.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from aipcs_mcp.numeric_domain import is_exact_number, is_signed_64_integer
from aipcs_mcp.records import (
    AssignBranchRecordsCommand,
    BranchAssignmentOutcome,
    BranchMutationOutcome,
    BranchPage,
    CreateBranchCommand,
    CreateRecordCommand,
    DataFailure,
    DeleteRecordCommand,
    DeleteRecordOutcome,
    FrozenJsonObject,
    GetRecordQuery,
    HistoryPage,
    ListBranchesQuery,
    ListRecordsQuery,
    MaintenanceQuery,
    MaintenanceResult,
    RecordContractError,
    RecordField,
    RecordHistoryEvent,
    RecordHistoryQuery,
    RecordMutationOutcome,
    RecordPage,
    RecordSpecification,
    RecordValue,
    SearchRecordsQuery,
    ServiceSummary,
    SummaryQuery,
    UpdateBranchCommand,
    UpdateRecordCommand,
)
from aipcs_mcp.storage.contracts import ServiceStoreLocator
from aipcs_mcp.storage.errors import (
    StorageBusy,
    StorageContractError,
    StorageMigrationError,
    StorageUnavailable,
)
from aipcs_mcp.storage.write_admission import (
    FENCE_FINGERPRINT,
    FENCE_IDEMPOTENCY_KEY,
    FENCE_OPERATION_KIND,
    INITIAL_FENCE,
    decode_fence,
)

from . import discovery
from .connection import PostgreSQLConnectionPolicy, PostgreSQLDsn, connect_postgresql
from .data_core import (
    canonical_json,
    canonical_json_value,
    cursor,
    cursor_record,
    failure,
    fingerprint,
    qualified,
    quote,
)
from .domain_schema import PostgreSQLDomainSchemaStore
from .service_store import PostgreSQLServiceStoreCatalog, _acquire_advisory_lock
from .service_store import _inspect as inspect_service_foundation
from .service_store_migrations import HISTORY, MUTATION
from .topology import (
    PostgreSQLBranchTopology,
    observe_assignment_command_replay,
    observe_create_branch_replay,
    observe_update_branch_replay,
)

_MAX_REVISION = 2**63 - 1
_PRINCIPAL_MAX = 128
_CREATED_VIA_MAX = 128


class ConnectionFactory(Protocol):
    def __call__(self, dsn: PostgreSQLDsn, policy: PostgreSQLConnectionPolicy) -> object: ...


class _UncertainMutation:
    pass


class _ConstraintViolation(Exception):
    pass


class PostgreSQLMaterialisedDataStore:
    """CRUD/history adapter; caller composition supplies no physical location."""

    def __init__(
        self,
        dsn: PostgreSQLDsn,
        *,
        connect_timeout_seconds: int = 10,
        lock_timeout_ms: int = 5_000,
        statement_timeout_ms: int = 30_000,
        connection_factory: ConnectionFactory = connect_postgresql,
        service_store_catalog: PostgreSQLServiceStoreCatalog | None = None,
        domain_schema_store: PostgreSQLDomainSchemaStore | None = None,
        clock: Callable[[], datetime] | None = None,
        record_ids: Callable[[], UUID] | None = None,
        branch_ids: Callable[[], UUID] | None = None,
    ) -> None:
        if type(dsn) is not PostgreSQLDsn or not callable(connection_factory):
            raise StorageUnavailable()
        if any(
            type(x) is not int
            for x in (connect_timeout_seconds, lock_timeout_ms, statement_timeout_ms)
        ):
            raise StorageUnavailable()
        if (
            clock is not None
            and not callable(clock)
            or record_ids is not None
            and not callable(record_ids)
            or branch_ids is not None
            and not callable(branch_ids)
        ):
            raise StorageUnavailable()
        self._dsn, self._factory = dsn, connection_factory
        self._timeouts = (connect_timeout_seconds, lock_timeout_ms, statement_timeout_ms)
        self._catalog = service_store_catalog or PostgreSQLServiceStoreCatalog(
            dsn,
            connect_timeout_seconds=connect_timeout_seconds,
            lock_timeout_ms=lock_timeout_ms,
            statement_timeout_ms=statement_timeout_ms,
            connection_factory=connection_factory,
        )
        self._domain = domain_schema_store or PostgreSQLDomainSchemaStore(
            dsn,
            connect_timeout_seconds=connect_timeout_seconds,
            lock_timeout_ms=lock_timeout_ms,
            statement_timeout_ms=statement_timeout_ms,
            connection_factory=connection_factory,
            service_store_catalog=self._catalog,
        )
        self._clock, self._record_ids = clock or (lambda: datetime.now(UTC)), record_ids or uuid4
        self._topology = PostgreSQLBranchTopology(clock=self._clock, branch_ids=branch_ids)

    def __repr__(self) -> str:
        return "PostgreSQLMaterialisedDataStore(<redacted>)"

    def create_record(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        command: CreateRecordCommand,
    ) -> RecordMutationOutcome | DataFailure:
        checked = self._admit(locator, principal_id, created_via, specification)
        if isinstance(checked, DataFailure) or type(command) is not CreateRecordCommand:
            return checked if isinstance(checked, DataFailure) else DataFailure("validation_failed")
        try:
            fields = _normalise(
                checked,
                command.entity_name,
                checked.validate_create(command.entity_name, command.record),
            )
        except Exception:
            return DataFailure("validation_failed")
        mark = fingerprint(
            {
                "operation": "create_record",
                "created_via": created_via,
                "entity_name": command.entity_name,
                "record": fields.to_dict(),
            }
        )

        def op(connection: object) -> RecordMutationOutcome | DataFailure:
            replay = _record_replay(
                connection,
                locator.namespace,
                principal_id,
                command.idempotency_key,
                "create_record",
                mark,
                checked,
            )
            if replay is not None:
                return replay
            now, record_id = _now(self._clock), _new_id(self._record_ids)
            value = RecordValue(command.entity_name, record_id, 1, now, now, created_via, fields)
            _require_references(
                connection, locator.namespace, principal_id, checked, command.entity_name, fields
            )
            _insert(connection, locator.namespace, principal_id, checked, value)
            _history(
                connection, locator.namespace, principal_id, value, "create", None, fields, now
            )
            result = RecordMutationOutcome(value, False)
            _complete(
                connection,
                locator.namespace,
                principal_id,
                command.idempotency_key,
                "create_record",
                mark,
                _record_result(value),
                now,
            )
            return result

        return self._mutate(
            locator,
            checked,
            principal_id,
            command.idempotency_key,
            op,
            lambda c: _observe_record(
                c,
                locator.namespace,
                principal_id,
                command.idempotency_key,
                "create_record",
                mark,
                checked,
            ),
        )

    def get_record(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        query: GetRecordQuery,
    ) -> RecordValue | DataFailure:
        checked = self._admit(locator, principal_id, created_via, specification)
        if (
            isinstance(checked, DataFailure)
            or type(query) is not GetRecordQuery
            or checked.entity(query.entity_name) is None
        ):
            return checked if isinstance(checked, DataFailure) else DataFailure("validation_failed")
        return self._read(
            locator,
            checked,
            lambda c: (
                _select(
                    c, locator.namespace, principal_id, checked, query.entity_name, query.record_id
                )
                or DataFailure("not_found")
            ),
        )

    def list_records(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        query: ListRecordsQuery,
    ) -> RecordPage | DataFailure:
        checked = self._admit(locator, principal_id, created_via, specification)
        if (
            isinstance(checked, DataFailure)
            or type(query) is not ListRecordsQuery
            or checked.entity(query.entity_name) is None
        ):
            return checked if isinstance(checked, DataFailure) else DataFailure("validation_failed")
        identity = fingerprint(
            {
                "kind": "list",
                "p": principal_id,
                "v": checked.schema_version,
                "e": query.entity_name,
                "b": str(query.branch_id) if query.branch_id else None,
                "r": query.branch_role,
            }
        )
        try:
            position = cursor_record(query.page.cursor, identity)
        except RecordContractError:
            return DataFailure("validation_failed")
        return self._read(
            locator,
            checked,
            lambda c: _page(
                c,
                locator.namespace,
                principal_id,
                checked,
                query.entity_name,
                (),
                query.branch_id,
                query.branch_role,
                query.page.limit,
                position,
                identity,
            ),
        )

    def search_records(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        query: SearchRecordsQuery,
    ) -> RecordPage | DataFailure:
        checked = self._admit(locator, principal_id, created_via, specification)
        if isinstance(checked, DataFailure) or type(query) is not SearchRecordsQuery:
            return checked if isinstance(checked, DataFailure) else DataFailure("validation_failed")
        try:
            filters = tuple(
                checked.validate_filter(query.entity_name, item.field_name, item.value)
                for item in query.filters
            )
            if filters != query.filters:
                raise ValueError
        except Exception:
            return DataFailure("validation_failed")
        identity = fingerprint(
            {
                "kind": "search",
                "p": principal_id,
                "v": checked.schema_version,
                "e": query.entity_name,
                "b": str(query.branch_id) if query.branch_id else None,
                "r": query.branch_role,
                "f": [(f.field_name, f.mode, f.value) for f in filters],
            }
        )
        try:
            position = cursor_record(query.page.cursor, identity)
        except RecordContractError:
            return DataFailure("validation_failed")
        return self._read(
            locator,
            checked,
            lambda c: _page(
                c,
                locator.namespace,
                principal_id,
                checked,
                query.entity_name,
                filters,
                query.branch_id,
                query.branch_role,
                query.page.limit,
                position,
                identity,
            ),
        )

    def update_record(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        command: UpdateRecordCommand,
    ) -> RecordMutationOutcome | DataFailure:
        checked = self._admit(locator, principal_id, created_via, specification)
        if (
            isinstance(checked, DataFailure)
            or type(command) is not UpdateRecordCommand
            or command.expected_record_version >= _MAX_REVISION
        ):
            return checked if isinstance(checked, DataFailure) else DataFailure("validation_failed")
        try:
            updates = checked.validate_updates(command.entity_name, command.updates)
        except Exception:
            return DataFailure("validation_failed")
        mark = fingerprint(
            {
                "operation": "update_record",
                "created_via": created_via,
                "entity_name": command.entity_name,
                "record_id": str(command.record_id),
                "expected_record_version": command.expected_record_version,
                "updates": updates.to_dict(),
            }
        )

        def op(connection: object) -> RecordMutationOutcome | DataFailure:
            replay = _record_replay(
                connection,
                locator.namespace,
                principal_id,
                command.idempotency_key,
                "update_record",
                mark,
                checked,
            )
            if replay is not None:
                return replay
            before = _select(
                connection,
                locator.namespace,
                principal_id,
                checked,
                command.entity_name,
                command.record_id,
            )
            if before is None:
                return DataFailure("not_found")
            if before.record_version != command.expected_record_version:
                return DataFailure("stale_record_version")
            merged = before.fields.to_dict()
            merged.update(updates.to_dict())
            try:
                fields = _normalise(
                    checked,
                    command.entity_name,
                    checked.validate_create(command.entity_name, merged),
                )
            except Exception:
                return DataFailure("validation_failed")
            _require_references(
                connection, locator.namespace, principal_id, checked, command.entity_name, fields
            )
            now = _now(self._clock)
            after = RecordValue(
                before.entity_name,
                before.record_id,
                before.record_version + 1,
                before.created_at,
                now,
                before.created_via,
                fields,
            )
            if not _update(
                connection, locator.namespace, principal_id, checked, after, before.record_version
            ):
                return DataFailure("stale_record_version")
            _history(
                connection,
                locator.namespace,
                principal_id,
                after,
                "update",
                before.fields,
                fields,
                now,
            )
            result = RecordMutationOutcome(after, False)
            _complete(
                connection,
                locator.namespace,
                principal_id,
                command.idempotency_key,
                "update_record",
                mark,
                _record_result(after),
                now,
            )
            return result

        return self._mutate(
            locator,
            checked,
            principal_id,
            command.idempotency_key,
            op,
            lambda c: _observe_record(
                c,
                locator.namespace,
                principal_id,
                command.idempotency_key,
                "update_record",
                mark,
                checked,
            ),
        )

    def delete_record(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        command: DeleteRecordCommand,
    ) -> DeleteRecordOutcome | DataFailure:
        checked = self._admit(locator, principal_id, created_via, specification)
        if (
            isinstance(checked, DataFailure)
            or type(command) is not DeleteRecordCommand
            or command.expected_record_version >= _MAX_REVISION
        ):
            return checked if isinstance(checked, DataFailure) else DataFailure("validation_failed")
        mark = fingerprint(
            {
                "operation": "delete_record",
                "created_via": created_via,
                "entity_name": command.entity_name,
                "record_id": str(command.record_id),
                "expected_record_version": command.expected_record_version,
            }
        )

        def op(connection: object) -> DeleteRecordOutcome | DataFailure:
            replay = _delete_replay(
                connection,
                locator.namespace,
                principal_id,
                command.idempotency_key,
                mark,
                checked,
                command.entity_name,
            )
            if replay is not None:
                return replay
            before = _select(
                connection,
                locator.namespace,
                principal_id,
                checked,
                command.entity_name,
                command.record_id,
            )
            if before is None:
                return DataFailure("not_found")
            if before.record_version != command.expected_record_version:
                return DataFailure("stale_record_version")
            now = _now(self._clock)
            terminal = RecordValue(
                before.entity_name,
                before.record_id,
                before.record_version + 1,
                before.created_at,
                now,
                before.created_via,
                before.fields,
            )
            _execute(
                connection,
                f"DELETE FROM {qualified(locator.namespace, '__aipcs_record_branch')} WHERE principal_id=%s AND entity_name=%s AND record_id=%s",
                (principal_id, command.entity_name, command.record_id),
            )
            deleted = _execute(
                connection,
                f"DELETE FROM {qualified(locator.namespace, command.entity_name)} WHERE id=%s AND owner_id=%s AND record_version=%s",
                (command.record_id, principal_id, before.record_version),
            ).rowcount
            if deleted != 1:
                return DataFailure("stale_record_version")
            _history(
                connection,
                locator.namespace,
                principal_id,
                terminal,
                "delete",
                before.fields,
                None,
                now,
            )
            result = DeleteRecordOutcome(command.record_id, terminal.record_version, False)
            _complete(
                connection,
                locator.namespace,
                principal_id,
                command.idempotency_key,
                "delete_record",
                mark,
                _delete_result(result, command.entity_name),
                now,
            )
            return result

        return self._mutate(
            locator,
            checked,
            principal_id,
            command.idempotency_key,
            op,
            lambda c: _observe_delete(
                c,
                locator.namespace,
                principal_id,
                command.idempotency_key,
                mark,
                checked,
                command.entity_name,
            ),
        )

    def record_history(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        query: RecordHistoryQuery,
    ) -> HistoryPage | DataFailure:
        checked = self._admit(locator, principal_id, created_via, specification)
        if (
            isinstance(checked, DataFailure)
            or type(query) is not RecordHistoryQuery
            or checked.entity(query.entity_name) is None
        ):
            return checked if isinstance(checked, DataFailure) else DataFailure("validation_failed")
        identity = fingerprint(
            {
                "kind": "history",
                "p": principal_id,
                "v": checked.schema_version,
                "e": query.entity_name,
                "id": str(query.record_id) if query.record_id else None,
            }
        )
        try:
            position = cursor_record(query.page.cursor, identity)
        except RecordContractError:
            return DataFailure("validation_failed")

        def operation(c: object) -> HistoryPage:
            where, args = ["principal_id=%s", "entity_name=%s"], [principal_id, query.entity_name]
            if position is not None:
                where.append("event_sequence > %s")
                args.append(position.int)
            if query.record_id is not None:
                where.append("record_id=%s")
                args.append(query.record_id)
            rows = _rows(
                _execute(
                    c,
                    f"SELECT event_sequence,entity_name,record_id,record_version,event_kind,before_json,after_json,occurred_at FROM {qualified(locator.namespace, HISTORY)} WHERE {' AND '.join(where)} ORDER BY event_sequence LIMIT %s",
                    (*args, query.page.limit + 1),
                )
            )
            events = tuple(_history_event(row, checked) for row in rows[: query.page.limit])
            next_value = (
                cursor(identity, UUID(int=int(rows[query.page.limit - 1]["event_sequence"])))
                if len(rows) > query.page.limit
                else None
            )
            return HistoryPage(events, next_value)

        return self._read(locator, checked, operation)

    def create_branch(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        command: CreateBranchCommand,
    ) -> BranchMutationOutcome | DataFailure:
        checked = self._admit(locator, principal_id, created_via, specification)
        if isinstance(checked, DataFailure) or type(command) is not CreateBranchCommand:
            return checked if isinstance(checked, DataFailure) else DataFailure("validation_failed")
        return self._mutate(
            locator,
            checked,
            principal_id,
            command.idempotency_key,
            lambda connection: self._topology.create_branch(
                connection, locator.namespace, principal_id, created_via, command
            ),
            lambda connection: observe_create_branch_replay(
                connection, locator.namespace, principal_id, created_via, command
            ),
        )

    def list_branches(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        query: ListBranchesQuery,
    ) -> BranchPage | DataFailure:
        checked = self._admit(locator, principal_id, created_via, specification)
        if isinstance(checked, DataFailure) or type(query) is not ListBranchesQuery:
            return checked if isinstance(checked, DataFailure) else DataFailure("validation_failed")
        return self._read(
            locator,
            checked,
            lambda connection: self._topology.list_branches(
                connection, locator.namespace, principal_id, query
            ),
        )

    def update_branch(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        command: UpdateBranchCommand,
    ) -> BranchMutationOutcome | DataFailure:
        checked = self._admit(locator, principal_id, created_via, specification)
        if isinstance(checked, DataFailure) or type(command) is not UpdateBranchCommand:
            return checked if isinstance(checked, DataFailure) else DataFailure("validation_failed")
        return self._mutate(
            locator,
            checked,
            principal_id,
            command.idempotency_key,
            lambda connection: self._topology.update_branch(
                connection, locator.namespace, principal_id, created_via, command
            ),
            lambda connection: observe_update_branch_replay(
                connection, locator.namespace, principal_id, created_via, command
            ),
        )

    def assign_branch_records(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        command: AssignBranchRecordsCommand,
    ) -> BranchAssignmentOutcome | DataFailure:
        checked = self._admit(locator, principal_id, created_via, specification)
        if isinstance(checked, DataFailure) or type(command) is not AssignBranchRecordsCommand:
            return checked if isinstance(checked, DataFailure) else DataFailure("validation_failed")
        return self._mutate(
            locator,
            checked,
            principal_id,
            command.idempotency_key,
            lambda connection: self._topology.assign_records(
                connection, locator.namespace, principal_id, created_via, checked, command
            ),
            lambda connection: observe_assignment_command_replay(
                connection, locator.namespace, principal_id, created_via, command
            ),
        )

    def service_summary(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        query: SummaryQuery,
    ) -> ServiceSummary | DataFailure:
        checked = self._admit(locator, principal_id, created_via, specification)
        if isinstance(checked, DataFailure) or type(query) is not SummaryQuery:
            return checked if isinstance(checked, DataFailure) else DataFailure("validation_failed")
        if set(name for name, _size in query.sample_sizes) - {
            entity.name for entity in checked.entities
        }:
            return DataFailure("validation_failed")
        return self._read(
            locator,
            checked,
            lambda connection: discovery.service_summary(
                connection, locator.namespace, principal_id, checked, query
            ),
        )

    def maintenance_scan(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        query: MaintenanceQuery,
    ) -> MaintenanceResult | DataFailure:
        checked = self._admit(locator, principal_id, created_via, specification)
        if isinstance(checked, DataFailure) or type(query) is not MaintenanceQuery:
            return checked if isinstance(checked, DataFailure) else DataFailure("validation_failed")
        if query.entity_name is not None and checked.entity(query.entity_name) is None:
            return DataFailure("validation_failed")
        try:
            now = _now(self._clock)
        except StorageUnavailable:
            return DataFailure("storage_unavailable")
        return self._read(
            locator,
            checked,
            lambda connection: discovery.maintenance_scan(
                connection, locator.namespace, principal_id, checked, query, now=now
            ),
        )

    @staticmethod
    def _admit(
        locator: object, principal: object, created_via: object, specification: object
    ) -> RecordSpecification | DataFailure:
        try:
            if (
                type(locator) is not ServiceStoreLocator
                or locator.backend != "postgresql"
                or not _text(principal, _PRINCIPAL_MAX)
                or not _text(created_via, _CREATED_VIA_MAX)
            ):
                raise ValueError
            if type(specification) is not RecordSpecification:
                raise ValueError
            return specification
        except Exception:
            return DataFailure("validation_failed")

    def _connect(self, namespace: str) -> object:
        a, b, c = self._timeouts
        return self._factory(
            self._dsn,
            PostgreSQLConnectionPolicy(
                schema=namespace,
                connect_timeout_seconds=a,
                lock_timeout_ms=b,
                statement_timeout_ms=c,
            ),
        )

    def _ready(self, locator: ServiceStoreLocator, specification: RecordSpecification) -> None:
        if self._catalog.inspect_migration(locator).status != "ready":
            raise StorageMigrationError()
        # The domain store owns full physical conformance at materialisation;
        # operation admission proves the current schema still contains each
        # public entity/field before any data SQL is issued.

    def _read(
        self,
        locator: ServiceStoreLocator,
        specification: RecordSpecification,
        operation: Callable[[object], Any],
    ) -> Any:
        connection: object | None = None
        try:
            self._ready(locator, specification)
            connection = self._connect(locator.namespace)
            _execute(connection, "BEGIN READ ONLY")
            _require_service_foundation(connection, locator.namespace)
            _require_domain(connection, locator.namespace, specification)
            result = operation(connection)
            _rollback(connection)
            return result
        except StorageMigrationError:
            return DataFailure("storage_migration_required")
        except StorageUnavailable:
            return DataFailure("storage_unavailable")
        except Exception as error:
            return _data_failure(error, False)
        finally:
            _close(connection)

    def _mutate(
        self,
        locator: ServiceStoreLocator,
        specification: RecordSpecification,
        principal_id: str,
        idempotency_key: str,
        operation: Callable[[object], Any],
        observer: Callable[[object], Any],
    ) -> Any:
        first = self._mutate_once(locator, specification, principal_id, idempotency_key, operation)
        if not isinstance(first, _UncertainMutation):
            return first
        seen = self._observe(locator, specification, observer)
        if seen is not None:
            return seen
        second = self._mutate_once(locator, specification, principal_id, idempotency_key, operation)
        return (
            self._observe(locator, specification, observer) or DataFailure("operation_uncertain")
            if isinstance(second, _UncertainMutation)
            else second
        )

    def _mutate_once(
        self,
        locator: ServiceStoreLocator,
        specification: RecordSpecification,
        principal_id: str,
        idempotency_key: str,
        operation: Callable[[object], Any],
    ) -> Any:
        connection: object | None = None
        commit_started = False
        try:
            self._ready(locator, specification)
            connection = self._connect(locator.namespace)
            _execute(connection, "BEGIN")
            _acquire_advisory_lock(connection, locator.namespace)
            _require_service_foundation(connection, locator.namespace)
            _require_domain(connection, locator.namespace, specification)
            if not _write_admission_open(connection, locator.namespace):
                _rollback(connection)
                return DataFailure("write_admission_closed")
            _lock_idempotency(connection, locator.namespace, principal_id, idempotency_key)
            result = operation(connection)
            if isinstance(result, DataFailure):
                _rollback(connection)
                return result
            commit_started = True
            _commit(connection)
            return result
        except _ConstraintViolation:
            _rollback(connection)
            return DataFailure("constraint_violation")
        except StorageMigrationError:
            _rollback(connection)
            return DataFailure("storage_migration_required")
        except StorageUnavailable:
            _rollback(connection)
            return DataFailure("storage_unavailable")
        except Exception as error:
            _rollback(connection)
            if commit_started:
                return _UncertainMutation()
            return _data_failure(error, False)
        finally:
            _close(connection)

    def _observe(
        self,
        locator: ServiceStoreLocator,
        specification: RecordSpecification,
        observer: Callable[[object], Any],
    ) -> Any | None:
        connection: object | None = None
        try:
            self._ready(locator, specification)
            connection = self._connect(locator.namespace)
            _execute(connection, "BEGIN READ ONLY")
            _require_service_foundation(connection, locator.namespace)
            _require_domain(connection, locator.namespace, specification)
            value = observer(connection)
            _rollback(connection)
            return value
        except Exception:
            return None
        finally:
            _close(connection)


def _page(
    c: object,
    namespace: str,
    principal: str,
    spec: RecordSpecification,
    entity: str,
    filters: tuple,
    branch_id: UUID | None,
    branch_role: str | None,
    limit: int,
    position: UUID | None,
    identity: str,
) -> RecordPage:
    clauses, params = ["r.owner_id=%s", "r.id > %s"], [principal, position or UUID(int=0)]
    joins = ""
    if branch_id is not None:
        joins = f" JOIN {qualified(namespace, '__aipcs_record_branch')} AS b ON b.principal_id=r.owner_id AND b.entity_name=%s AND b.record_id=r.id"
        params.insert(0, entity)
        clauses.extend(("b.branch_id=%s", "b.role=%s"))
        params.extend((branch_id, branch_role))
    for item in filters:
        field = spec.field(entity, item.field_name)
        if field is None:
            raise StorageContractError()
        if item.mode == "membership":
            clauses.append(f"r.{quote(item.field_name)} @> %s::jsonb")
            params.append(canonical_json([item.value]))
        else:
            clauses.append(f"r.{quote(item.field_name)}=%s")
            params.append(_encode(field, item.value))
    rows = _rows(
        _execute(
            c,
            f"SELECT r.* FROM {qualified(namespace, entity)} AS r{joins} WHERE {' AND '.join(clauses)} ORDER BY r.id LIMIT %s",
            (*params, limit + 1),
        )
    )
    values = tuple(_record(row, principal, spec, entity) for row in rows[:limit])
    return RecordPage(values, cursor(identity, values[-1].record_id) if len(rows) > limit else None)


def _select(
    c: object,
    namespace: str,
    principal: str,
    spec: RecordSpecification,
    entity: str,
    record_id: UUID,
) -> RecordValue | None:
    rows = _rows(
        _execute(
            c,
            f"SELECT * FROM {qualified(namespace, entity)} WHERE id=%s AND owner_id=%s",
            (record_id, principal),
        )
    )
    return None if not rows else _record(rows[0], principal, spec, entity)


def _insert(
    c: object, namespace: str, principal: str, spec: RecordSpecification, value: RecordValue
) -> None:
    entity = spec.entity(value.entity_name)
    if entity is None:
        raise StorageContractError()
    fields = value.fields.to_dict()
    names = ("id", "owner_id", "created_at", "updated_at", "created_via", "record_version") + tuple(
        f.name for f in entity.domain_fields
    )
    values = (
        value.record_id,
        principal,
        value.created_at,
        value.updated_at,
        value.created_via,
        value.record_version,
    ) + tuple(_encode(f, fields.get(f.name)) for f in entity.domain_fields)
    _execute(
        c,
        f"INSERT INTO {qualified(namespace, entity.name)} ({','.join(quote(x) for x in names)}) VALUES ({','.join('%s' for _ in names)})",
        values,
    )


def _update(
    c: object,
    namespace: str,
    principal: str,
    spec: RecordSpecification,
    value: RecordValue,
    expected: int,
) -> bool:
    entity = spec.entity(value.entity_name)
    if entity is None:
        raise StorageContractError()
    fields = value.fields.to_dict()
    assignments = ["updated_at=%s", "record_version=%s"] + [
        f"{quote(f.name)}=%s" for f in entity.domain_fields
    ]
    params = (
        value.updated_at,
        value.record_version,
        *(_encode(f, fields.get(f.name)) for f in entity.domain_fields),
        value.record_id,
        principal,
        expected,
    )
    return (
        _execute(
            c,
            f"UPDATE {qualified(namespace, entity.name)} SET {','.join(assignments)} WHERE id=%s AND owner_id=%s AND record_version=%s",
            params,
        ).rowcount
        == 1
    )


def _record(
    row: Mapping[str, object], principal: str, spec: RecordSpecification, entity_name: str
) -> RecordValue:
    try:
        entity = spec.entity(entity_name)
        if entity is None or row["owner_id"] != principal:
            raise ValueError
        fields = _normalise(
            spec,
            entity_name,
            spec.validate_create(
                entity_name, {f.name: _decode(f, row[f.name]) for f in entity.domain_fields}
            ),
        )
        return RecordValue(
            entity_name,
            _uuid(row["id"]),
            _revision(row["record_version"]),
            _time(row["created_at"]),
            _time(row["updated_at"]),
            _string(row["created_via"]),
            fields,
        )
    except Exception:
        raise StorageUnavailable() from None


def _history(
    c: object,
    namespace: str,
    principal: str,
    value: RecordValue,
    kind: str,
    before: FrozenJsonObject | None,
    after: FrozenJsonObject | None,
    occurred: datetime,
) -> None:
    _execute(
        c,
        f"INSERT INTO {qualified(namespace, HISTORY)} (principal_id,entity_name,record_id,record_version,event_kind,before_json,after_json,occurred_at) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)",
        (
            principal,
            value.entity_name,
            value.record_id,
            value.record_version,
            kind,
            None if before is None else canonical_json(before.to_dict()),
            None if after is None else canonical_json(after.to_dict()),
            occurred,
        ),
    )


def _history_event(row: Mapping[str, object], spec: RecordSpecification) -> RecordHistoryEvent:
    try:
        entity = _string(row["entity_name"])
        before = _snapshot(row["before_json"], spec, entity)
        after = _snapshot(row["after_json"], spec, entity)
        return RecordHistoryEvent(
            entity,
            _uuid(row["record_id"]),
            _revision(row["record_version"]),
            _string(row["event_kind"]),
            _time(row["occurred_at"]),
            before,
            after,
        )  # type: ignore[arg-type]
    except Exception:
        raise StorageUnavailable() from None


def _snapshot(value: object, spec: RecordSpecification, entity: str) -> FrozenJsonObject | None:
    if value is None:
        return None
    raw = canonical_json_value(value)
    if type(raw) is not dict:
        raise StorageUnavailable()
    return _normalise(spec, entity, spec.validate_create(entity, raw))


def _mutation(c: object, namespace: str, principal: str, key: str) -> Mapping[str, object] | None:
    rows = _rows(
        _execute(
            c,
            f"SELECT operation_kind,fingerprint,result_json FROM {qualified(namespace, MUTATION)} WHERE principal_id=%s AND idempotency_key=%s",
            (principal, key),
        )
    )
    return None if not rows else rows[0]


def _record_replay(
    c: object,
    namespace: str,
    principal: str,
    key: str,
    kind: str,
    mark: str,
    spec: RecordSpecification,
) -> RecordMutationOutcome | DataFailure | None:
    row = _mutation(c, namespace, principal, key)
    if row is None:
        return None
    if row["operation_kind"] != kind or row["fingerprint"] != mark:
        return DataFailure("changed_fingerprint")
    return RecordMutationOutcome(_record_result_decode(row["result_json"], spec), True)


def _observe_record(
    c: object,
    namespace: str,
    principal: str,
    key: str,
    kind: str,
    mark: str,
    spec: RecordSpecification,
) -> RecordMutationOutcome | None:
    replay = _record_replay(c, namespace, principal, key, kind, mark, spec)
    return replay if isinstance(replay, RecordMutationOutcome) else None


def _delete_replay(
    c: object,
    namespace: str,
    principal: str,
    key: str,
    mark: str,
    spec: RecordSpecification,
    entity: str,
) -> DeleteRecordOutcome | DataFailure | None:
    row = _mutation(c, namespace, principal, key)
    if row is None:
        return None
    if row["operation_kind"] != "delete_record" or row["fingerprint"] != mark:
        return DataFailure("changed_fingerprint")
    return _delete_result_decode(row["result_json"], spec, entity)


def _observe_delete(
    c: object,
    namespace: str,
    principal: str,
    key: str,
    mark: str,
    spec: RecordSpecification,
    entity: str,
) -> DeleteRecordOutcome | None:
    replay = _delete_replay(c, namespace, principal, key, mark, spec, entity)
    return replay if isinstance(replay, DeleteRecordOutcome) else None


def _complete(
    c: object,
    namespace: str,
    principal: str,
    key: str,
    kind: str,
    mark: str,
    result: str,
    now: datetime,
) -> None:
    _execute(
        c,
        f"INSERT INTO {qualified(namespace, MUTATION)} (principal_id,idempotency_key,operation_kind,fingerprint,result_json,completed_at) VALUES (%s,%s,%s,%s,%s::jsonb,%s)",
        (principal, key, kind, mark, result, now),
    )


def _record_result(value: RecordValue) -> str:
    return canonical_json(
        {
            "kind": "record",
            "record": {
                "entity_name": value.entity_name,
                "id": str(value.record_id),
                "record_version": value.record_version,
                "created_at": value.created_at.isoformat().replace("+00:00", "Z"),
                "updated_at": value.updated_at.isoformat().replace("+00:00", "Z"),
                "created_via": value.created_via,
                "fields": value.fields.to_dict(),
            },
        }
    )


def _record_result_decode(value: object, spec: RecordSpecification) -> RecordValue:
    try:
        raw = canonical_json_value(value)
        record = raw["record"]
        if type(raw) is not dict or raw.get("kind") != "record" or type(record) is not dict:
            raise ValueError
        fields = _normalise(
            spec,
            record["entity_name"],
            spec.validate_create(record["entity_name"], record["fields"]),
        )
        return RecordValue(
            record["entity_name"],
            _uuid(record["id"]),
            _revision(record["record_version"]),
            _time(record["created_at"]),
            _time(record["updated_at"]),
            _string(record["created_via"]),
            fields,
        )
    except Exception:
        raise StorageUnavailable() from None


def _delete_result(value: DeleteRecordOutcome, entity: str) -> str:
    return canonical_json(
        {
            "kind": "delete",
            "entity_name": entity,
            "id": str(value.deleted_record_id),
            "record_version": value.deleted_record_version,
        }
    )


def _delete_result_decode(
    value: object, spec: RecordSpecification, entity: str
) -> DeleteRecordOutcome:
    try:
        raw = canonical_json_value(value)
        if (
            type(raw) is not dict
            or raw
            != {
                "kind": "delete",
                "entity_name": entity,
                "id": raw.get("id"),
                "record_version": raw.get("record_version"),
            }
            or spec.entity(entity) is None
        ):
            raise ValueError
        return DeleteRecordOutcome(_uuid(raw["id"]), _revision(raw["record_version"]), True)
    except Exception:
        raise StorageUnavailable() from None


def _require_references(
    c: object,
    namespace: str,
    principal: str,
    spec: RecordSpecification,
    entity: str,
    fields: FrozenJsonObject,
) -> None:
    values = fields.to_dict()
    for rel in spec.relationships:
        if rel.source_entity != entity or values.get(rel.source_field) is None:
            continue
        rows = _rows(
            _execute(
                c,
                f"SELECT 1 FROM {qualified(namespace, rel.target_entity)} WHERE {quote(rel.target_field)}=%s AND owner_id=%s LIMIT 1",
                (values[rel.source_field], principal),
            )
        )
        if not rows:
            raise _ConstraintViolation()


def _require_service_foundation(connection: object, namespace: str) -> None:
    """Verify the exact R1 foundation on this operation's own snapshot."""

    if inspect_service_foundation(connection, namespace).status != "ready":
        raise StorageMigrationError()


def _write_admission_open(connection: object, namespace: str) -> bool:
    rows = _rows(
        _execute(
            connection,
            f"SELECT idempotency_key,fingerprint,result_json "
            f"FROM {qualified(namespace, MUTATION)} "
            "WHERE operation_kind=%s",
            (FENCE_OPERATION_KIND,),
        )
    )
    if not rows:
        return INITIAL_FENCE.state == "open"
    if len(rows) != 1 or (
        rows[0]["idempotency_key"],
        rows[0]["fingerprint"],
    ) != (FENCE_IDEMPOTENCY_KEY, FENCE_FINGERPRINT):
        raise StorageMigrationError()
    return decode_fence(rows[0]["result_json"]).state == "open"


def _lock_idempotency(
    connection: object, namespace: str, principal_id: str, idempotency_key: str
) -> None:
    """Serialize only one mutation/replay identity for this transaction.

    The server derives the 64-bit advisory key from one length-delimited,
    bound identity. The namespace prevents cross-service contention; principal
    and key prevent unrelated requests in the same service from sharing a lock.
    """

    _execute(
        connection,
        "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s, 0))",
        (f"aipcs:{namespace}\x1f{principal_id}\x1f{idempotency_key}",),
    )


_POSTGRES_TYPES = {
    "string": "text",
    "integer": "bigint",
    "number": "double precision",
    "boolean": "boolean",
    "datetime": "timestamp with time zone",
    "uuid": "uuid",
    "string_list": "jsonb",
}


def _require_domain(c: object, namespace: str, spec: RecordSpecification) -> None:
    """Bounded, schema-qualified admission check before every data operation.

    This is deliberately not a second DDL inspector: the domain adapter owns
    exact layout conformance.  It does ensure a stale/missing/evolved domain
    cannot be treated as a data store merely because foundation tables exist.
    """
    rows = _rows(
        _execute(
            c,
            """SELECT cls.relname, attr.attname,
               pg_catalog.format_type(attr.atttypid, attr.atttypmod), attr.attnotnull,
               attr.attidentity, attr.atthasdef, coll.collname
        FROM pg_catalog.pg_class AS cls
        JOIN pg_catalog.pg_namespace AS ns ON ns.oid=cls.relnamespace
        JOIN pg_catalog.pg_attribute AS attr ON attr.attrelid=cls.oid
        LEFT JOIN pg_catalog.pg_collation AS coll ON coll.oid=attr.attcollation
        WHERE ns.nspname=%s AND cls.relkind='r' AND cls.relname NOT LIKE '__aipcs_%%'
          AND attr.attnum>0 AND NOT attr.attisdropped
        ORDER BY cls.relname,attr.attnum""",
            (namespace,),
        )
    )
    actual: dict[str, list[tuple[str, str, bool, str, bool, str | None]]] = {}
    for row in rows:
        name, field, data_type = row.get("relname"), row.get("attname"), row.get("format_type")
        not_null, identity, has_default, collation = (
            row.get("attnotnull"),
            row.get("attidentity"),
            row.get("atthasdef"),
            row.get("collname"),
        )
        if (
            type(name) is not str
            or type(field) is not str
            or type(data_type) is not str
            or type(not_null) is not bool
            or type(identity) is not str
            or type(has_default) is not bool
            or collation is not None
            and type(collation) is not str
        ):
            raise StorageMigrationError()
        actual.setdefault(name, []).append(
            (field, data_type, not_null, identity, has_default, collation)
        )
    expected = {
        entity.name: [
            (
                field.name,
                _POSTGRES_TYPES[field.logical_type],
                field.required,
                "",
                False,
                "C" if field.logical_type == "string" else None,
            )
            for field in entity.fields
        ]
        for entity in spec.entities
    }
    actual = {name: sorted(columns) for name, columns in actual.items()}
    expected = {name: sorted(columns) for name, columns in expected.items()}
    if actual != expected:
        raise StorageMigrationError()
    # PostgreSQL 18 projects NOT NULL attributes into pg_constraint as
    # contype='n'. The exact column comparison above remains authoritative for
    # attnotnull; admission accepts only domain-owned constraint kinds here.
    constraints = _rows(
        _execute(
            c,
            """SELECT con.conname AS name, source.relname AS source_name, con.contype AS kind, con.condeferrable AS deferrable,
                       con.condeferred AS deferred, con.confupdtype AS update_action, con.confdeltype AS delete_action,
                       ARRAY(SELECT attr.attname FROM unnest(con.conkey) WITH ORDINALITY AS key(attnum, ordinal)
                             JOIN pg_catalog.pg_attribute AS attr ON attr.attrelid=con.conrelid AND attr.attnum=key.attnum
                             ORDER BY key.ordinal) AS source_key, target.relname AS target_name,
                       ARRAY(SELECT attr.attname FROM unnest(con.confkey) WITH ORDINALITY AS key(attnum, ordinal)
                             JOIN pg_catalog.pg_attribute AS attr ON attr.attrelid=con.confrelid AND attr.attnum=key.attnum
                             ORDER BY key.ordinal) AS target_key
                FROM pg_catalog.pg_constraint AS con
                JOIN pg_catalog.pg_class AS source ON source.oid=con.conrelid
                JOIN pg_catalog.pg_namespace AS ns ON ns.oid=source.relnamespace
                LEFT JOIN pg_catalog.pg_class AS target ON target.oid=con.confrelid
                WHERE ns.nspname=%s AND source.relname NOT LIKE '__aipcs_%%'
                  AND con.contype IN ('c', 'p', 'u', 'f')
                ORDER BY con.conname""",
            (namespace,),
        )
    )
    expected_constraints: set[tuple[object, ...]] = set()
    for entity in spec.entities:
        expected_constraints.add(
            (None, entity.name, "p", False, False, " ", " ", ("id",), None, ())
        )
    for relationship in spec.relationships:
        expected_constraints.add(
            (
                relationship.name,
                relationship.source_entity,
                "f",
                False,
                False,
                "r",
                "r",
                (relationship.source_field,),
                relationship.target_entity,
                (relationship.target_field,),
            )
        )
    actual_constraints: set[tuple[object, ...]] = set()
    for row in constraints:
        key, foreign_key = row.get("source_key"), row.get("target_key")
        name = row.get("name")
        source = row.get("source_name")
        kind = row.get("kind")
        deferrable = row.get("deferrable")
        deferred = row.get("deferred")
        update = row.get("update_action")
        delete = row.get("delete_action")
        target = row.get("target_name")
        if not all(
            (
                type(name) is str,
                type(source) is str,
                type(kind) is str,
                type(deferrable) is bool,
                type(deferred) is bool,
                type(update) is str,
                type(delete) is str,
                type(key) is list,
                target is None or type(target) is str,
                type(foreign_key) is list,
            )
        ) or any(type(value) is not str for value in (*key, *foreign_key)):
            raise StorageMigrationError()
        actual_constraints.add(
            (
                name if kind == "f" else None,
                source,
                kind,
                deferrable,
                deferred,
                update,
                delete,
                tuple(key),
                target,
                tuple(foreign_key),
            )
        )
    if actual_constraints != expected_constraints:
        raise StorageMigrationError()


def _normalise(
    spec: RecordSpecification, entity: str, fields: FrozenJsonObject
) -> FrozenJsonObject:
    target = spec.entity(entity)
    if target is None:
        raise RecordContractError()
    source = fields.to_dict()
    normal: dict[str, object] = {}
    for field in target.domain_fields:
        value = source.get(field.name)
        if value is not None and field.logical_type == "number":
            if not is_exact_number(value):
                raise RecordContractError()
            value = float(value)
        elif value is not None and field.logical_type == "string_list":
            value = list(value)  # type: ignore[arg-type]
        normal[field.name] = value
    return spec.validate_create(entity, normal)


def _encode(field: RecordField, value: object) -> object:
    if value is None:
        return None
    if field.logical_type == "number":
        if not is_exact_number(value):
            raise RecordContractError()
        return float(value)
    if field.logical_type == "integer" and not is_signed_64_integer(value):
        raise RecordContractError()
    if field.logical_type == "string_list":
        return canonical_json(list(value))  # type: ignore[arg-type]
    return value


def _decode(field: RecordField, value: object) -> object:
    if value is None:
        return None
    if field.logical_type == "uuid":
        return str(_uuid(value))
    if field.logical_type == "datetime":
        return _time(value).isoformat().replace("+00:00", "Z")
    if field.logical_type == "number":
        if not is_exact_number(value):
            raise ValueError
        return float(value)
    if field.logical_type == "integer":
        if not is_signed_64_integer(value):
            raise ValueError
        return value
    if field.logical_type == "string_list":
        result = canonical_json_value(value)
        if type(result) is not list:
            raise ValueError
        return result
    return value


def _execute(connection: object, query: str, params: tuple[object, ...] = ()):
    return connection.execute(query, params)  # type: ignore[attr-defined,no-any-return]


def _rows(cursor: object) -> list[dict[str, object]]:
    values = cursor.fetchall()  # type: ignore[attr-defined]
    names = [item.name if hasattr(item, "name") else item[0] for item in cursor.description]  # type: ignore[attr-defined]
    return [
        dict(zip(names, row, strict=True)) if not isinstance(row, dict) else dict(row)
        for row in values
    ]


def _rollback(connection: object | None) -> None:
    if connection is not None:
        with suppress(Exception):
            connection.rollback()  # type: ignore[attr-defined]


def _commit(connection: object) -> None:
    connection.commit()  # type: ignore[attr-defined]


def _close(connection: object | None) -> None:
    if connection is not None:
        with suppress(Exception):
            connection.close()  # type: ignore[attr-defined]


def _data_failure(error: BaseException, commit_started: bool) -> DataFailure:
    if isinstance(error, StorageMigrationError):
        return DataFailure("storage_migration_required")
    if isinstance(error, StorageBusy):
        return DataFailure("storage_busy")
    if isinstance(error, StorageUnavailable):
        return DataFailure("storage_unavailable")
    state = getattr(error, "sqlstate", None)
    if type(state) is str and state.startswith("23"):
        return DataFailure("constraint_violation")
    mapped = failure(error, commit_started=commit_started)
    if isinstance(mapped, StorageBusy):
        return DataFailure("storage_busy")
    if isinstance(mapped, StorageUnavailable):
        return DataFailure("storage_unavailable")
    return DataFailure("operation_uncertain" if commit_started else "internal_error")


def _text(value: object, maximum: int) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and len(value) <= maximum
        and "\x00" not in value
    )


def _uuid(value: object) -> UUID:
    result = value if type(value) is UUID else UUID(value)  # type: ignore[arg-type]
    if result.int == 0:
        raise ValueError
    return result


def _revision(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_REVISION:
        raise ValueError
    return value


def _time(value: object) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if type(value) is str else value
    if (
        type(result) is not datetime
        or result.tzinfo is None
        or result.utcoffset() != UTC.utcoffset(result)
    ):
        raise ValueError
    return result.astimezone(UTC)


def _string(value: object) -> str:
    if type(value) is not str:
        raise ValueError
    return value


def _now(factory: Callable[[], datetime]) -> datetime:
    return _time(factory())


def _new_id(factory: Callable[[], UUID]) -> UUID:
    return _uuid(factory())
