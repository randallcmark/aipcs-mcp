"""SQLite implementation of the backend-neutral materialised data-store port.

The adapter owns service-local transactions and never accepts a path.  Every
operation opens an already allocated service store through
``SQLiteLocationPolicy``, verifies the exact R3 foundation, and uses only
identifiers compiled from a deeply revalidated record specification.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
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
    DiscoveryFacet,
    FrozenJsonObject,
    GetRecordQuery,
    HistoryPage,
    ListBranchesQuery,
    ListRecordsQuery,
    MaintenanceQuery,
    MaintenanceResult,
    PageCursor,
    PageRequest,
    RecordContractError,
    RecordEntity,
    RecordField,
    RecordHistoryEvent,
    RecordHistoryQuery,
    RecordMutationOutcome,
    RecordPage,
    RecordRelationship,
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

from . import discovery, service_store_inspection
from .codecs import decode_time, encode_time
from .connection import (
    SQLiteConnectionPolicy,
    SQLiteConnectionPurpose,
    connect,
    require_supported_sqlite_runtime,
)
from .location import AnchoredLocation, SQLiteLocationPolicy
from .result_codes import is_sqlite_busy
from .service_store import SQLiteServiceStoreCatalog
from .service_store_migrations import R2, R3, R3_HISTORY_TABLE, R3_MUTATION_TABLE
from .topology import (
    SQLiteBranchTopology,
    observe_assignment_command_replay,
    observe_create_branch_replay,
    observe_update_branch_replay,
)

_MAX_REVISION = 2**63 - 1
_CREATED_VIA_MAX = 128
_PRINCIPAL_MAX = 128
_TYPE_NAMES = {
    "string": "TEXT",
    "datetime": "TEXT",
    "uuid": "TEXT",
    "string_list": "TEXT",
    "integer": "INTEGER",
    "boolean": "INTEGER",
    "number": "REAL",
}


class _OperationUncertain(Exception):
    pass


class _ConstraintViolation(Exception):
    pass


class _UncertainMutation:
    """Private marker: a commit path failed and requires exact replay observation."""


class SQLiteMaterialisedDataStore:
    """Record operations over exact, already materialised SQLite service stores."""

    def __init__(
        self,
        location: SQLiteLocationPolicy,
        *,
        busy_timeout_ms: int = 5_000,
        connection_policy: SQLiteConnectionPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        record_ids: Callable[[], UUID] | None = None,
        branch_ids: Callable[[], UUID] | None = None,
    ) -> None:
        require_supported_sqlite_runtime()
        if not isinstance(location, SQLiteLocationPolicy):
            raise StorageUnavailable()
        if connection_policy is None:
            connection_policy = SQLiteConnectionPolicy(busy_timeout_ms)
        elif type(connection_policy) is not SQLiteConnectionPolicy or busy_timeout_ms != 5_000:
            raise StorageUnavailable()
        if clock is not None and not callable(clock):
            raise StorageUnavailable()
        if record_ids is not None and not callable(record_ids):
            raise StorageUnavailable()
        if branch_ids is not None and not callable(branch_ids):
            raise StorageUnavailable()
        self._location = location
        self._connection_policy = connection_policy
        self._service_store_catalog = SQLiteServiceStoreCatalog(
            location,
            connection_policy=self._connection_policy,
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._record_ids = record_ids or uuid4
        self._topology = SQLiteBranchTopology(clock=self._clock, branch_ids=branch_ids)

    def __repr__(self) -> str:
        return "SQLiteMaterialisedDataStore(<redacted>)"

    def create_record(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        command: CreateRecordCommand,
    ) -> RecordMutationOutcome | DataFailure:
        checked = self._admit(locator, principal_id, created_via, specification)
        if type(checked) is DataFailure or type(command) is not CreateRecordCommand:
            return checked if type(checked) is DataFailure else DataFailure("validation_failed")
        try:
            record = _normalise_fields(
                checked,
                command.entity_name,
                checked.validate_create(command.entity_name, command.record),
            )
        except Exception:
            return DataFailure("validation_failed")
        fingerprint = _fingerprint(
            {
                "operation": "create_record",
                "created_via": created_via,
                "entity_name": command.entity_name,
                "record": record.to_dict(),
            }
        )

        def operation(connection: sqlite3.Connection) -> RecordMutationOutcome | DataFailure:
            replay = _record_replay(
                connection,
                principal_id,
                command.idempotency_key,
                "create_record",
                fingerprint,
                checked,
            )
            if replay is not None:
                return replay
            now = _now(self._clock)
            record_id = _new_id(self._record_ids)
            value = RecordValue(
                command.entity_name,
                record_id,
                1,
                now,
                now,
                created_via,
                record,
            )
            _require_references(connection, principal_id, checked, command.entity_name, record)
            _insert_record(connection, principal_id, checked, value)
            _append_history(connection, principal_id, value, "create", None, record, now)
            result = RecordMutationOutcome(value, False)
            _complete_mutation(
                connection,
                principal_id,
                command.idempotency_key,
                "create_record",
                fingerprint,
                _encode_record_result(value),
                now,
            )
            return result

        return self._mutate(
            locator,
            checked,
            operation,
            lambda connection: _observe_record_replay(
                connection,
                principal_id,
                command.idempotency_key,
                "create_record",
                fingerprint,
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
        if type(checked) is DataFailure or type(query) is not GetRecordQuery:
            return checked if type(checked) is DataFailure else DataFailure("validation_failed")
        if checked.entity(query.entity_name) is None:
            return DataFailure("validation_failed")

        def operation(connection: sqlite3.Connection) -> RecordValue | DataFailure:
            value = _select_record(connection, principal_id, checked, query.entity_name, query.record_id)
            return DataFailure("not_found") if value is None else value

        return self._read(locator, checked, operation)

    def list_records(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        query: ListRecordsQuery,
    ) -> RecordPage | DataFailure:
        checked = self._admit(locator, principal_id, created_via, specification)
        if type(checked) is DataFailure or type(query) is not ListRecordsQuery:
            return checked if type(checked) is DataFailure else DataFailure("validation_failed")
        if checked.entity(query.entity_name) is None:
            return DataFailure("validation_failed")
        identity = _query_identity(
            "list_records",
            principal_id,
            checked.schema_version,
            query.entity_name,
            query.branch_id,
            query.branch_role,
            (),
        )
        try:
            position = _cursor_position(query.page.cursor, "record", identity, str)
        except RecordContractError:
            return DataFailure("validation_failed")

        def operation(connection: sqlite3.Connection) -> RecordPage:
            return _record_page(
                connection,
                principal_id,
                checked,
                query.entity_name,
                (),
                query.branch_id,
                query.branch_role,
                query.page.limit,
                position,
                identity,
            )

        return self._read(locator, checked, operation)

    def search_records(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        query: SearchRecordsQuery,
    ) -> RecordPage | DataFailure:
        checked = self._admit(locator, principal_id, created_via, specification)
        if type(checked) is DataFailure or type(query) is not SearchRecordsQuery:
            return checked if type(checked) is DataFailure else DataFailure("validation_failed")
        try:
            filters = tuple(
                checked.validate_filter(query.entity_name, item.field_name, item.value)
                for item in query.filters
            )
            if filters != query.filters:
                raise RecordContractError()
        except Exception:
            return DataFailure("validation_failed")
        identity = _query_identity(
            "search_records",
            principal_id,
            checked.schema_version,
            query.entity_name,
            query.branch_id,
            query.branch_role,
            tuple((item.field_name, item.mode, item.value) for item in filters),
        )
        try:
            position = _cursor_position(query.page.cursor, "record", identity, str)
        except RecordContractError:
            return DataFailure("validation_failed")

        def operation(connection: sqlite3.Connection) -> RecordPage:
            return _record_page(
                connection,
                principal_id,
                checked,
                query.entity_name,
                filters,
                query.branch_id,
                query.branch_role,
                query.page.limit,
                position,
                identity,
            )

        return self._read(locator, checked, operation)

    def update_record(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        command: UpdateRecordCommand,
    ) -> RecordMutationOutcome | DataFailure:
        checked = self._admit(locator, principal_id, created_via, specification)
        if type(checked) is DataFailure or type(command) is not UpdateRecordCommand:
            return checked if type(checked) is DataFailure else DataFailure("validation_failed")
        if command.expected_record_version >= _MAX_REVISION:
            return DataFailure("validation_failed")
        try:
            updates = checked.validate_updates(command.entity_name, command.updates)
        except Exception:
            return DataFailure("validation_failed")
        fingerprint = _fingerprint(
            {
                "operation": "update_record",
                "created_via": created_via,
                "entity_name": command.entity_name,
                "record_id": str(command.record_id),
                "expected_record_version": command.expected_record_version,
                "updates": updates.to_dict(),
            }
        )

        def operation(connection: sqlite3.Connection) -> RecordMutationOutcome | DataFailure:
            replay = _record_replay(
                connection,
                principal_id,
                command.idempotency_key,
                "update_record",
                fingerprint,
                checked,
            )
            if replay is not None:
                return replay
            before = _select_record(
                connection, principal_id, checked, command.entity_name, command.record_id
            )
            if before is None:
                return DataFailure("not_found")
            if before.record_version != command.expected_record_version:
                return DataFailure("stale_record_version")
            merged = before.fields.to_dict()
            merged.update(updates.to_dict())
            try:
                fields = _normalise_fields(
                    checked,
                    command.entity_name,
                    checked.validate_create(command.entity_name, merged),
                )
            except Exception:
                return DataFailure("validation_failed")
            _require_references(connection, principal_id, checked, command.entity_name, fields)
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
            if not _update_record(connection, principal_id, checked, after, before.record_version):
                return DataFailure("stale_record_version")
            _append_history(
                connection, principal_id, after, "update", before.fields, after.fields, now
            )
            result = RecordMutationOutcome(after, False)
            _complete_mutation(
                connection,
                principal_id,
                command.idempotency_key,
                "update_record",
                fingerprint,
                _encode_record_result(after),
                now,
            )
            return result

        return self._mutate(
            locator,
            checked,
            operation,
            lambda connection: _observe_record_replay(
                connection,
                principal_id,
                command.idempotency_key,
                "update_record",
                fingerprint,
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
        if type(checked) is DataFailure or type(command) is not DeleteRecordCommand:
            return checked if type(checked) is DataFailure else DataFailure("validation_failed")
        if command.expected_record_version >= _MAX_REVISION:
            return DataFailure("validation_failed")
        fingerprint = _fingerprint(
            {
                "operation": "delete_record",
                "created_via": created_via,
                "entity_name": command.entity_name,
                "record_id": str(command.record_id),
                "expected_record_version": command.expected_record_version,
            }
        )

        def operation(connection: sqlite3.Connection) -> DeleteRecordOutcome | DataFailure:
            replay = _delete_replay(
                connection,
                principal_id,
                command.idempotency_key,
                fingerprint,
                checked,
                command.entity_name,
            )
            if replay is not None:
                return replay
            before = _select_record(
                connection, principal_id, checked, command.entity_name, command.record_id
            )
            if before is None:
                return DataFailure("not_found")
            if before.record_version != command.expected_record_version:
                return DataFailure("stale_record_version")
            terminal_version = before.record_version + 1
            now = _now(self._clock)
            connection.execute(
                'DELETE FROM "__aipcs_record_branch" '
                'WHERE "principal_id"=? AND "entity_name"=? AND "record_id"=?',
                (principal_id, command.entity_name, str(command.record_id)),
            )
            cursor = connection.execute(
                f'DELETE FROM {_quote(command.entity_name)} '
                'WHERE "id"=? AND "owner_id"=? AND "record_version"=?',
                (str(command.record_id), principal_id, before.record_version),
            )
            if cursor.rowcount != 1:
                return DataFailure("stale_record_version")
            terminal = RecordValue(
                before.entity_name,
                before.record_id,
                terminal_version,
                before.created_at,
                now,
                before.created_via,
                before.fields,
            )
            _append_history(
                connection, principal_id, terminal, "delete", before.fields, None, now
            )
            result = DeleteRecordOutcome(command.record_id, terminal_version, False)
            _complete_mutation(
                connection,
                principal_id,
                command.idempotency_key,
                "delete_record",
                fingerprint,
                _encode_delete_result(result, command.entity_name),
                now,
            )
            return result

        return self._mutate(
            locator,
            checked,
            operation,
            lambda connection: _observe_delete_replay(
                connection,
                principal_id,
                command.idempotency_key,
                fingerprint,
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
        if type(checked) is DataFailure or type(query) is not RecordHistoryQuery:
            return checked if type(checked) is DataFailure else DataFailure("validation_failed")
        if checked.entity(query.entity_name) is None:
            return DataFailure("validation_failed")
        identity = _query_identity(
            "record_history",
            principal_id,
            checked.schema_version,
            query.entity_name,
            query.record_id,
            None,
            (),
        )
        try:
            position = _cursor_position(query.page.cursor, "history", identity, int)
        except RecordContractError:
            return DataFailure("validation_failed")

        def operation(connection: sqlite3.Connection) -> HistoryPage:
            clauses = ['"principal_id"=?', '"entity_name"=?', '"event_sequence">?']
            parameters: list[object] = [principal_id, query.entity_name, position or 0]
            if query.record_id is not None:
                clauses.append('"record_id"=?')
                parameters.append(str(query.record_id))
            rows = connection.execute(
                f'SELECT * FROM "{R3_HISTORY_TABLE}" WHERE {" AND ".join(clauses)} '
                'ORDER BY "event_sequence" LIMIT ?',
                (*parameters, query.page.limit + 1),
            ).fetchall()
            events = tuple(_decode_history(row, checked) for row in rows[: query.page.limit])
            next_cursor = (
                _encode_cursor("history", identity, int(rows[query.page.limit - 1]["event_sequence"]))
                if len(rows) > query.page.limit
                else None
            )
            return HistoryPage(events, next_cursor)

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
        if type(checked) is DataFailure or type(command) is not CreateBranchCommand:
            return checked if type(checked) is DataFailure else DataFailure("validation_failed")
        return self._mutate(
            locator,
            checked,
            lambda connection: self._topology.create_branch(
                connection, principal_id, created_via, command
            ),
            lambda connection: observe_create_branch_replay(
                connection, principal_id, created_via, command
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
        if type(checked) is DataFailure or type(query) is not ListBranchesQuery:
            return checked if type(checked) is DataFailure else DataFailure("validation_failed")
        cursor_identity = _query_identity(
            "list_branches",
            principal_id,
            checked.schema_version,
            "branch",
            query.parent_branch_id,
            None,
            (("status", query.status), ("branch_type", query.branch_type)),
        )
        try:
            inner_cursor = _unwrap_branch_cursor(query.page.cursor, cursor_identity)
            inner_query = ListBranchesQuery(
                query.status,
                query.branch_type,
                query.parent_branch_id,
                PageRequest(query.page.limit, inner_cursor),
            )
        except Exception:
            return DataFailure("validation_failed")

        def operation(connection: sqlite3.Connection) -> BranchPage | DataFailure:
            result = self._topology.list_branches(connection, principal_id, inner_query)
            if type(result) is DataFailure:
                return result
            return BranchPage(
                result.branches,
                _wrap_branch_cursor(result.next_cursor, cursor_identity),
            )

        return self._read(locator, checked, operation)

    def update_branch(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        command: UpdateBranchCommand,
    ) -> BranchMutationOutcome | DataFailure:
        checked = self._admit(locator, principal_id, created_via, specification)
        if type(checked) is DataFailure or type(command) is not UpdateBranchCommand:
            return checked if type(checked) is DataFailure else DataFailure("validation_failed")
        return self._mutate(
            locator,
            checked,
            lambda connection: self._topology.update_branch(
                connection, principal_id, created_via, command
            ),
            lambda connection: observe_update_branch_replay(
                connection, principal_id, created_via, command
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
        if type(checked) is DataFailure or type(command) is not AssignBranchRecordsCommand:
            return checked if type(checked) is DataFailure else DataFailure("validation_failed")
        return self._mutate(
            locator,
            checked,
            lambda connection: self._topology.assign_records(
                connection, principal_id, created_via, checked, command
            ),
            lambda connection: observe_assignment_command_replay(
                connection, principal_id, created_via, command
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
        """Read a ready R3 summary or expose exact R2 as affordance-only state.

        This is intentionally separate from ``_read``: the compatibility
        summary is the one v1 read that can truthfully describe a clean R2
        store without opening R3-only tables or performing any migration.
        """

        checked = self._admit(locator, principal_id, created_via, specification)
        if type(checked) is DataFailure or type(query) is not SummaryQuery:
            return checked if type(checked) is DataFailure else DataFailure("validation_failed")
        if set(name for name, _size in query.sample_sizes) - {
            entity.name for entity in checked.entities
        }:
            return DataFailure("validation_failed")

        def operation(connection: sqlite3.Connection, state) -> ServiceSummary:  # type: ignore[no-untyped-def]
            if (
                state.status == "outdated"
                and state.applied_revision == R2.revision
                and state.target_revision == R3.revision
            ):
                return ServiceSummary("migration_required", (), 0, (), (), ())
            if state.status != "ready":
                raise StorageMigrationError()
            return discovery.service_summary(connection, principal_id, checked, query)

        return self._summary_read(locator, checked, operation)

    def maintenance_scan(
        self,
        locator: ServiceStoreLocator,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        query: MaintenanceQuery,
    ) -> MaintenanceResult | DataFailure:
        checked = self._admit(locator, principal_id, created_via, specification)
        if type(checked) is DataFailure or type(query) is not MaintenanceQuery:
            return checked if type(checked) is DataFailure else DataFailure("validation_failed")
        if query.entity_name is not None and checked.entity(query.entity_name) is None:
            return DataFailure("validation_failed")

        return self._read(
            locator,
            checked,
            lambda connection: discovery.maintenance_scan(
                connection, principal_id, checked, query, now=_now(self._clock)
            ),
        )

    @staticmethod
    def _admit(
        locator: object,
        principal_id: object,
        created_via: object,
        specification: object,
    ) -> RecordSpecification | DataFailure:
        try:
            _namespace(locator)
            if not _text(principal_id, _PRINCIPAL_MAX) or not _text(
                created_via, _CREATED_VIA_MAX
            ):
                raise RecordContractError()
            return _detach_specification(specification)
        except Exception:
            return DataFailure("validation_failed")

    def _read(self, locator, specification, operation):  # type: ignore[no-untyped-def]
        anchored: AnchoredLocation | None = None
        connection: sqlite3.Connection | None = None
        try:
            namespace = _namespace(locator)
            anchored = _acquire_existing(self._location, namespace)
            connection = connect(
                anchored,
                "ro",
                query_only=True,
                policy=self._connection_policy,
                purpose=SQLiteConnectionPurpose.READY_INSPECTION,
            )
            connection.execute("BEGIN")
            _require_ready(connection, namespace)
            _require_domain(connection, specification)
            result = operation(connection)
            connection.rollback()
            return result
        except StorageMigrationError:
            return DataFailure("storage_migration_required")
        except StorageBusy:
            return DataFailure("storage_busy")
        except sqlite3.DatabaseError as error:
            return DataFailure("storage_busy" if is_sqlite_busy(error) else "storage_unavailable")
        except StorageUnavailable:
            return DataFailure("storage_unavailable")
        except Exception:
            return DataFailure("internal_error")
        finally:
            _release(connection, anchored)

    def _summary_read(self, locator, specification, operation):  # type: ignore[no-untyped-def]
        """Read summary state without treating a clean R2 predecessor as ready."""

        anchored: AnchoredLocation | None = None
        connection: sqlite3.Connection | None = None
        try:
            namespace = _namespace(locator)
            anchored = _acquire_existing(self._location, namespace)
            connection = connect(
                anchored,
                "ro",
                query_only=True,
                policy=self._connection_policy,
                purpose=SQLiteConnectionPurpose.READY_INSPECTION,
            )
            connection.execute("BEGIN")
            state = service_store_inspection.inspect_connection(connection, namespace, integrity=True)
            _require_domain(connection, specification)
            result = operation(connection, state)
            connection.rollback()
            return result
        except StorageMigrationError:
            return DataFailure("storage_migration_required")
        except StorageBusy:
            return DataFailure("storage_busy")
        except sqlite3.DatabaseError as error:
            return DataFailure("storage_busy" if is_sqlite_busy(error) else "storage_unavailable")
        except StorageUnavailable:
            return DataFailure("storage_unavailable")
        except Exception:
            return DataFailure("internal_error")
        finally:
            _release(connection, anchored)

    def _mutate(self, locator, specification, operation, observer):  # type: ignore[no-untyped-def]
        """Run one mutation with a single evidence-led retry after commit uncertainty."""

        first = self._mutate_once(locator, specification, operation)
        if type(first) is not _UncertainMutation:
            return first
        observed = self._observe_completed(locator, specification, observer)
        if observed is not None:
            return observed
        retry = self._mutate_once(locator, specification, operation)
        if type(retry) is not _UncertainMutation:
            return retry
        observed = self._observe_completed(locator, specification, observer)
        return DataFailure("operation_uncertain") if observed is None else observed

    def _mutate_once(self, locator, specification, operation):  # type: ignore[no-untyped-def]
        anchored: AnchoredLocation | None = None
        connection: sqlite3.Connection | None = None
        try:
            namespace = _namespace(locator)
            state = self._service_store_catalog.inspect_migration(locator)
            if (
                state.status in {"outdated", "dirty"}
                and state.applied_revision == R3.revision - 1
                and state.target_revision == R3.revision
            ):
                state = self._service_store_catalog.migrate(locator)
            if state.status != "ready":
                return DataFailure("storage_migration_required")
            anchored = _acquire_existing(self._location, namespace)
            connection = connect(
                anchored,
                "rw",
                query_only=False,
                policy=self._connection_policy,
                purpose=SQLiteConnectionPurpose.READY_OPERATION,
            )
            _begin_immediate(connection, anchored)
            _require_ready(connection, namespace)
            _require_domain(connection, specification)
            result = operation(connection)
            if type(result) is DataFailure:
                connection.rollback()
                return result
            _commit(connection, anchored)
            return result
        except _OperationUncertain:
            return _UncertainMutation()
        except _ConstraintViolation:
            return DataFailure("constraint_violation")
        except StorageMigrationError:
            return DataFailure("storage_migration_required")
        except StorageBusy:
            return DataFailure("storage_busy")
        except sqlite3.IntegrityError:
            return DataFailure("constraint_violation")
        except sqlite3.DatabaseError as error:
            return DataFailure("storage_busy" if is_sqlite_busy(error) else "storage_unavailable")
        except StorageUnavailable:
            return DataFailure("storage_unavailable")
        except Exception:
            return DataFailure("internal_error")
        finally:
            _release(connection, anchored)

    def _observe_completed(self, locator, specification, observer):  # type: ignore[no-untyped-def]
        """Reopen exact R3 state and trust only canonical local replay evidence."""

        anchored: AnchoredLocation | None = None
        connection: sqlite3.Connection | None = None
        try:
            namespace = _namespace(locator)
            anchored = _acquire_existing(self._location, namespace)
            connection = connect(
                anchored,
                "ro",
                query_only=True,
                policy=self._connection_policy,
                purpose=SQLiteConnectionPurpose.READY_INSPECTION,
            )
            connection.execute("BEGIN")
            _require_ready(connection, namespace)
            _require_domain(connection, specification)
            result = observer(connection)
            connection.rollback()
            return result
        except Exception:
            # A mismatched/corrupt row and an unavailable re-open are equally
            # untrustworthy.  The caller must not guess which occurred.
            return DataFailure("operation_uncertain")
        finally:
            _release(connection, anchored)


def _record_page(
    connection: sqlite3.Connection,
    principal_id: str,
    specification: RecordSpecification,
    entity_name: str,
    filters: tuple,
    branch_id: UUID | None,
    branch_role: str | None,
    limit: int,
    position: str | None,
    identity: str,
) -> RecordPage:
    alias = "r"
    clauses = [f'{alias}."owner_id"=?', f'{alias}."id">?']
    parameters: list[object] = [principal_id, position or ""]
    joins = ""
    if branch_id is not None:
        joins = (
            ' JOIN "__aipcs_record_branch" b'
            f' ON b."principal_id"={alias}."owner_id"'
            f' AND b."entity_name"={_literal(entity_name)}'
            f' AND b."record_id"={alias}."id"'
        )
        clauses.extend(['b."branch_id"=?', 'b."role"=?'])
        parameters.extend((str(branch_id), branch_role))
    for item in filters:
        field = specification.field(entity_name, item.field_name)
        if field is None:
            raise StorageContractError()
        if item.mode == "membership":
            clauses.append(
                f'EXISTS (SELECT 1 FROM json_each({alias}.{_quote(item.field_name)}) '
                'WHERE json_each.value=?)'
            )
            parameters.append(item.value)
        else:
            clauses.append(f'{alias}.{_quote(item.field_name)}=?')
            parameters.append(_encode_field(field, item.value))
    rows = connection.execute(
        f"SELECT {alias}.* FROM {_quote(entity_name)} {alias}{joins} "
        f'WHERE {" AND ".join(clauses)} ORDER BY {alias}."id" LIMIT ?',
        (*parameters, limit + 1),
    ).fetchall()
    values = tuple(
        _decode_record(row, principal_id, specification, entity_name) for row in rows[:limit]
    )
    next_cursor = (
        _encode_cursor("record", identity, str(rows[limit - 1]["id"]))
        if len(rows) > limit
        else None
    )
    return RecordPage(values, next_cursor)


def _select_record(
    connection: sqlite3.Connection,
    principal_id: str,
    specification: RecordSpecification,
    entity_name: str,
    record_id: UUID,
) -> RecordValue | None:
    row = connection.execute(
        f'SELECT * FROM {_quote(entity_name)} WHERE "id"=? AND "owner_id"=?',
        (str(record_id), principal_id),
    ).fetchone()
    return None if row is None else _decode_record(row, principal_id, specification, entity_name)


def _insert_record(
    connection: sqlite3.Connection,
    principal_id: str,
    specification: RecordSpecification,
    value: RecordValue,
) -> None:
    entity = specification.entity(value.entity_name)
    if entity is None:
        raise StorageContractError()
    fields = value.fields.to_dict()
    names = ("id", "owner_id", "created_at", "updated_at", "created_via", "record_version") + tuple(
        field.name for field in entity.domain_fields
    )
    values: tuple[object, ...] = (
        str(value.record_id),
        principal_id,
        encode_time(value.created_at),
        encode_time(value.updated_at),
        value.created_via,
        value.record_version,
    ) + tuple(_encode_field(field, fields.get(field.name)) for field in entity.domain_fields)
    connection.execute(
        f'INSERT INTO {_quote(entity.name)} ({",".join(_quote(name) for name in names)}) '
        f'VALUES ({",".join("?" for _ in names)})',
        values,
    )


def _update_record(
    connection: sqlite3.Connection,
    principal_id: str,
    specification: RecordSpecification,
    value: RecordValue,
    expected_version: int,
) -> bool:
    entity = specification.entity(value.entity_name)
    if entity is None:
        raise StorageContractError()
    fields = value.fields.to_dict()
    assignments = ['"updated_at"=?', '"record_version"=?'] + [
        f"{_quote(field.name)}=?" for field in entity.domain_fields
    ]
    parameters = [
        encode_time(value.updated_at),
        value.record_version,
        *(_encode_field(field, fields.get(field.name)) for field in entity.domain_fields),
        str(value.record_id),
        principal_id,
        expected_version,
    ]
    cursor = connection.execute(
        f'UPDATE {_quote(entity.name)} SET {",".join(assignments)} '
        'WHERE "id"=? AND "owner_id"=? AND "record_version"=?',
        parameters,
    )
    return cursor.rowcount == 1


def _decode_record(
    row: Mapping[str, Any],
    principal_id: str,
    specification: RecordSpecification,
    entity_name: str,
) -> RecordValue:
    try:
        entity = specification.entity(entity_name)
        if entity is None or row["owner_id"] != principal_id:
            raise ValueError
        record_id = UUID(row["id"])
        if str(record_id) != row["id"] or record_id.int == 0:
            raise ValueError
        fields = {
            field.name: _decode_field(field, row[field.name]) for field in entity.domain_fields
        }
        checked_fields = specification.validate_create(entity_name, fields)
        checked_fields = _normalise_fields(specification, entity_name, checked_fields)
        value = RecordValue(
            entity_name,
            record_id,
            _revision(row["record_version"]),
            decode_time(row["created_at"]),
            decode_time(row["updated_at"]),
            row["created_via"],
            checked_fields,
        )
        if value.created_via != row["created_via"]:
            raise ValueError
        return value
    except Exception:
        raise StorageUnavailable() from None


def _append_history(
    connection: sqlite3.Connection,
    principal_id: str,
    value: RecordValue,
    kind: Literal["create", "update", "delete"],
    before: FrozenJsonObject | None,
    after: FrozenJsonObject | None,
    occurred_at: datetime,
) -> None:
    connection.execute(
        f'INSERT INTO "{R3_HISTORY_TABLE}" '
        '("principal_id","entity_name","record_id","record_version","event_kind",'
        '"before_json","after_json","occurred_at") VALUES (?,?,?,?,?,?,?,?)',
        (
            principal_id,
            value.entity_name,
            str(value.record_id),
            value.record_version,
            kind,
            None if before is None else _canonical_json(before.to_dict()),
            None if after is None else _canonical_json(after.to_dict()),
            encode_time(occurred_at),
        ),
    )


def _decode_history(row: Mapping[str, Any], specification: RecordSpecification) -> RecordHistoryEvent:
    try:
        entity_name = row["entity_name"]
        entity = specification.entity(entity_name)
        if entity is None:
            raise ValueError
        before = _decode_snapshot(row["before_json"], specification, entity_name)
        after = _decode_snapshot(row["after_json"], specification, entity_name)
        record_id = UUID(row["record_id"])
        if str(record_id) != row["record_id"] or record_id.int == 0:
            raise ValueError
        kind = row["event_kind"]
        if kind == "update" and (before is None or after is None):
            raise ValueError
        value = RecordHistoryEvent(
            entity_name,
            record_id,
            _revision(row["record_version"]),
            kind,
            decode_time(row["occurred_at"]),
            before,
            after,
        )
        return value
    except Exception:
        raise StorageUnavailable() from None


def _decode_snapshot(
    text: object, specification: RecordSpecification, entity_name: str
) -> FrozenJsonObject | None:
    if text is None:
        return None
    raw = _decode_canonical_json(text)
    if not isinstance(raw, dict):
        raise StorageUnavailable()
    return _normalise_fields(
        specification, entity_name, specification.validate_create(entity_name, raw)
    )


def _record_replay(
    connection: sqlite3.Connection,
    principal_id: str,
    key: str,
    operation_kind: str,
    fingerprint: str,
    specification: RecordSpecification,
) -> RecordMutationOutcome | DataFailure | None:
    row = _mutation_row(connection, principal_id, key)
    if row is None:
        return None
    if row["operation_kind"] != operation_kind or row["fingerprint"] != fingerprint:
        return DataFailure("changed_fingerprint")
    value = _decode_record_result(row["result_json"], specification)
    return RecordMutationOutcome(value, True)


def _observe_record_replay(
    connection: sqlite3.Connection,
    principal_id: str,
    key: str,
    operation_kind: str,
    fingerprint: str,
    specification: RecordSpecification,
) -> RecordMutationOutcome | None:
    """Decode exact completed record evidence; anything else is untrustworthy."""

    row = _mutation_row(connection, principal_id, key)
    if row is None:
        return None
    if row["operation_kind"] != operation_kind or row["fingerprint"] != fingerprint:
        raise ValueError
    return RecordMutationOutcome(_decode_record_result(row["result_json"], specification), True)


def _delete_replay(
    connection: sqlite3.Connection,
    principal_id: str,
    key: str,
    fingerprint: str,
    specification: RecordSpecification,
    entity_name: str,
) -> DeleteRecordOutcome | DataFailure | None:
    row = _mutation_row(connection, principal_id, key)
    if row is None:
        return None
    if row["operation_kind"] != "delete_record" or row["fingerprint"] != fingerprint:
        return DataFailure("changed_fingerprint")
    return _decode_delete_result(row["result_json"], specification, entity_name)


def _observe_delete_replay(
    connection: sqlite3.Connection,
    principal_id: str,
    key: str,
    fingerprint: str,
    specification: RecordSpecification,
    entity_name: str,
) -> DeleteRecordOutcome | None:
    """Decode exact completed delete evidence after a post-commit fault."""

    row = _mutation_row(connection, principal_id, key)
    if row is None:
        return None
    if row["operation_kind"] != "delete_record" or row["fingerprint"] != fingerprint:
        raise ValueError
    return _decode_delete_result(row["result_json"], specification, entity_name)


def _mutation_row(
    connection: sqlite3.Connection, principal_id: str, key: str
) -> sqlite3.Row | None:
    return connection.execute(
        f'SELECT "operation_kind","fingerprint","result_json" FROM "{R3_MUTATION_TABLE}" '
        'WHERE "principal_id"=? AND "idempotency_key"=?',
        (principal_id, key),
    ).fetchone()


def _complete_mutation(
    connection: sqlite3.Connection,
    principal_id: str,
    key: str,
    operation_kind: str,
    fingerprint: str,
    result_json: str,
    completed_at: datetime,
) -> None:
    connection.execute(
        f'INSERT INTO "{R3_MUTATION_TABLE}" '
        '("principal_id","idempotency_key","operation_kind","fingerprint","result_json","completed_at") '
        "VALUES (?,?,?,?,?,?)",
        (principal_id, key, operation_kind, fingerprint, result_json, encode_time(completed_at)),
    )


def _encode_record_result(value: RecordValue) -> str:
    return _canonical_json(
        {
            "kind": "record",
            "record": {
                "entity_name": value.entity_name,
                "id": str(value.record_id),
                "record_version": value.record_version,
                "created_at": encode_time(value.created_at),
                "updated_at": encode_time(value.updated_at),
                "created_via": value.created_via,
                "fields": value.fields.to_dict(),
            },
        }
    )


def _decode_record_result(text: object, specification: RecordSpecification) -> RecordValue:
    try:
        raw = _decode_canonical_json(text)
        if type(raw) is not dict or set(raw) != {"kind", "record"} or raw["kind"] != "record":
            raise ValueError
        record = raw["record"]
        if type(record) is not dict or set(record) != {
            "entity_name",
            "id",
            "record_version",
            "created_at",
            "updated_at",
            "created_via",
            "fields",
        }:
            raise ValueError
        entity_name = record["entity_name"]
        fields = specification.validate_create(entity_name, record["fields"])
        value = RecordValue(
            entity_name,
            UUID(record["id"]),
            _revision(record["record_version"]),
            decode_time(record["created_at"]),
            decode_time(record["updated_at"]),
            record["created_via"],
            fields,
        )
        if _encode_record_result(value) != text:
            raise ValueError
        return value
    except Exception:
        raise StorageUnavailable() from None


def _encode_delete_result(value: DeleteRecordOutcome, entity_name: str) -> str:
    return _canonical_json(
        {
            "kind": "delete",
            "entity_name": entity_name,
            "id": str(value.deleted_record_id),
            "record_version": value.deleted_record_version,
        }
    )


def _decode_delete_result(
    text: object, specification: RecordSpecification, entity_name: str
) -> DeleteRecordOutcome:
    try:
        raw = _decode_canonical_json(text)
        if type(raw) is not dict or set(raw) != {
            "kind",
            "entity_name",
            "id",
            "record_version",
        }:
            raise ValueError
        if (
            raw["kind"] != "delete"
            or raw["entity_name"] != entity_name
            or specification.entity(entity_name) is None
        ):
            raise ValueError
        result = DeleteRecordOutcome(UUID(raw["id"]), _revision(raw["record_version"]), True)
        if _encode_delete_result(result, entity_name) != text:
            raise ValueError
        return result
    except Exception:
        raise StorageUnavailable() from None


def _require_references(
    connection: sqlite3.Connection,
    principal_id: str,
    specification: RecordSpecification,
    entity_name: str,
    fields: FrozenJsonObject,
) -> None:
    values = fields.to_dict()
    for relationship in specification.relationships:
        if relationship.source_entity != entity_name:
            continue
        value = values.get(relationship.source_field)
        if value is None:
            continue
        row = connection.execute(
            f'SELECT 1 FROM {_quote(relationship.target_entity)} '
            f'WHERE {_quote(relationship.target_field)}=? AND "owner_id"=? LIMIT 1',
            (value, principal_id),
        ).fetchone()
        if row is None:
            raise _ConstraintViolation()


def _require_ready(connection: sqlite3.Connection, namespace: str) -> None:
    if (
        service_store_inspection.inspect_connection(connection, namespace, integrity=True).status
        != "ready"
    ):
        raise StorageMigrationError()


def _require_domain(
    connection: sqlite3.Connection, specification: RecordSpecification
) -> None:
    for entity in specification.entities:
        rows = tuple(map(tuple, connection.execute(f"PRAGMA table_xinfo({_quote(entity.name)})")))
        if not rows:
            raise StorageMigrationError()
        actual = {row[1]: row for row in rows}
        if set(actual) != {field.name for field in entity.fields}:
            raise StorageMigrationError()
        for field in entity.fields:
            row = actual[field.name]
            expected_not_null = 1 if field.required else 0
            expected_pk = 1 if field.primary_key else 0
            if (
                row[2] != _TYPE_NAMES[field.logical_type]
                or row[3] != expected_not_null
                or row[4] is not None
                or row[5] != expected_pk
                or row[6] != 0
            ):
                raise StorageMigrationError()
        expected_foreign_keys = sorted(
            (
                relation.target_entity,
                relation.source_field,
                relation.target_field,
                "RESTRICT",
                "RESTRICT",
                "NONE",
            )
            for relation in specification.relationships
            if relation.source_entity == entity.name
        )
        actual_foreign_keys = sorted(
            (row[2], row[3], row[4], row[5], row[6], row[7])
            for row in connection.execute(f"PRAGMA foreign_key_list({_quote(entity.name)})")
        )
        if actual_foreign_keys != expected_foreign_keys:
            raise StorageMigrationError()


def _encode_field(field: RecordField, value: object) -> object:
    if value is None:
        return None
    if field.logical_type == "boolean":
        if type(value) is not bool:
            raise RecordContractError()
        return int(value)  # type: ignore[arg-type]
    if field.logical_type == "integer" and not is_signed_64_integer(value):
        raise RecordContractError()
    if field.logical_type == "number":
        if not is_exact_number(value):
            raise RecordContractError()
        return float(value)  # type: ignore[arg-type]
    if field.logical_type == "string_list":
        return _canonical_json(list(value))  # type: ignore[arg-type]
    return value


def _decode_field(field: RecordField, value: object) -> object:
    if value is None:
        return None
    if field.logical_type == "boolean":
        if type(value) is not int or value not in {0, 1}:
            raise ValueError
        return bool(value)
    if field.logical_type == "integer":
        if not is_signed_64_integer(value):
            raise ValueError
        return value
    if field.logical_type == "number":
        if not is_exact_number(value):
            raise ValueError
        return float(value)
    if field.logical_type == "string_list":
        raw = _decode_canonical_json(value)
        if type(raw) is not list:
            raise ValueError
        return raw
    if type(value) is not str:
        raise ValueError
    return value


def _normalise_fields(
    specification: RecordSpecification,
    entity_name: str,
    fields: FrozenJsonObject,
) -> FrozenJsonObject:
    entity = specification.entity(entity_name)
    if entity is None:
        raise RecordContractError()
    source = fields.to_dict()
    complete: dict[str, object] = {}
    for field in entity.domain_fields:
        value = source.get(field.name)
        if value is not None and field.logical_type == "number":
            if not is_exact_number(value):
                raise RecordContractError()
            value = float(value)  # type: ignore[arg-type]
        elif value is not None and field.logical_type == "string_list":
            value = list(value)  # type: ignore[arg-type]
        complete[field.name] = value
    return specification.validate_create(entity_name, complete)


def _query_identity(
    kind: str,
    principal_id: str,
    schema_version: int,
    entity_name: str,
    branch_id: object,
    branch_role: object,
    filters: tuple,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "kind": kind,
                "principal_id": principal_id,
                "schema_version": schema_version,
                "entity_name": entity_name,
                "branch_id": None if branch_id is None else str(branch_id),
                "branch_role": branch_role,
                "filters": filters,
            }
        ).encode()
    ).hexdigest()


def _encode_cursor(kind: str, identity: str, position: str | int) -> PageCursor:
    raw = _canonical_json({"v": 1, "kind": kind, "query": identity, "position": position}).encode()
    return PageCursor(base64.urlsafe_b64encode(raw).decode().rstrip("="))


def _wrap_branch_cursor(cursor: PageCursor | None, identity: str) -> PageCursor | None:
    if cursor is None:
        return None
    raw = _canonical_json(
        {"v": 1, "kind": "branch_scope", "query": identity, "inner": cursor.value}
    ).encode()
    return PageCursor(base64.urlsafe_b64encode(raw).decode().rstrip("="))


def _unwrap_branch_cursor(
    cursor: PageCursor | None, identity: str
) -> PageCursor | None:
    if cursor is None:
        return None
    try:
        padding = "=" * (-len(cursor.value) % 4)
        raw_bytes = base64.b64decode(
            cursor.value + padding, altchars=b"-_", validate=True
        )
        if base64.urlsafe_b64encode(raw_bytes).decode().rstrip("=") != cursor.value:
            raise ValueError
        raw_text = raw_bytes.decode("utf-8")
        raw = json.loads(raw_text)
        if (
            _canonical_json(raw) != raw_text
            or type(raw) is not dict
            or set(raw) != {"v", "kind", "query", "inner"}
            or raw["v"] != 1
            or raw["kind"] != "branch_scope"
            or raw["query"] != identity
            or type(raw["inner"]) is not str
        ):
            raise ValueError
        return PageCursor(raw["inner"])
    except Exception:
        raise RecordContractError() from None


def _cursor_position(
    cursor: PageCursor | None,
    kind: str,
    identity: str,
    expected_type: type,
) -> str | int | None:
    if cursor is None:
        return None
    try:
        padding = "=" * (-len(cursor.value) % 4)
        raw_bytes = base64.b64decode(cursor.value + padding, altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw_bytes).decode().rstrip("=") != cursor.value:
            raise ValueError
        raw_text = raw_bytes.decode("utf-8")
        raw = json.loads(raw_text)
        if _canonical_json(raw) != raw_text or type(raw) is not dict or set(raw) != {
            "v",
            "kind",
            "query",
            "position",
        }:
            raise ValueError
        if (
            raw["v"] != 1
            or raw["kind"] != kind
            or raw["query"] != identity
            or type(raw["position"]) is not expected_type
        ):
            raise ValueError
        if expected_type is str:
            value = UUID(raw["position"])
            if str(value) != raw["position"] or value.int == 0:
                raise ValueError
        elif raw["position"] < 1:
            raise ValueError
        return raw["position"]
    except Exception:
        raise RecordContractError() from None


def _detach_specification(value: object) -> RecordSpecification:
    if type(value) is not RecordSpecification:
        raise RecordContractError()
    entities = tuple(
        RecordEntity(
            entity.name,
            tuple(
                RecordField(
                    field.name,
                    field.logical_type,
                    field.required,
                    field.primary_key,
                    field.description,
                    field.allowed_values,
                    field.retrieval_mode,
                )
                for field in entity.fields
            ),
            entity.description,
        )
        for entity in value.entities
    )
    relationships = tuple(
        RecordRelationship(
            item.name,
            item.source_entity,
            item.source_field,
            item.target_entity,
            item.target_field,
            item.on_delete,
        )
        for item in value.relationships
    )
    facets = tuple(
        DiscoveryFacet(item.entity_name, item.field_name, item.filter_mode)
        for item in value.discovery_facets
    )
    return RecordSpecification(
        value.schema_version,
        entities,
        relationships,
        facets,
        tuple(value.query_patterns),
        value.retrieval_guidance,
    )


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _decode_canonical_json(value: object) -> object:
    if type(value) is not str:
        raise ValueError
    raw = json.loads(value)
    if _canonical_json(raw) != value:
        raise ValueError
    return raw


def _revision(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_REVISION:
        raise ValueError
    return value


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if (
        type(value) is not datetime
        or value.tzinfo is not UTC
        or value.utcoffset() != timedelta(0)
    ):
        raise StorageUnavailable()
    return value


def _new_id(factory: Callable[[], UUID]) -> UUID:
    value = factory()
    if type(value) is not UUID or value.int == 0:
        raise StorageUnavailable()
    return value


def _text(value: object, maximum: int) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and len(value) <= maximum
        and "\x00" not in value
    )


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _namespace(locator: object) -> str:
    if type(locator) is not ServiceStoreLocator or locator.backend != "sqlite":
        raise StorageContractError()
    return ServiceStoreLocator("sqlite", locator.namespace).namespace


def _acquire_existing(location: SQLiteLocationPolicy, namespace: str) -> AnchoredLocation:
    anchored = location.acquire_service_store(namespace, create=False, allow_journal=False)
    if anchored is None or anchored._database_stat is None or anchored._database_stat.st_size == 0:
        if anchored is not None:
            with suppress(Exception):
                anchored.close()
        raise StorageMigrationError()
    return anchored


def _begin_immediate(connection: sqlite3.Connection, location: AnchoredLocation) -> None:
    location.verify_live_database_identity()
    location.verify_live_sidecars(allow_journal=False)
    try:
        connection.execute("BEGIN IMMEDIATE")
    except Exception as error:
        with suppress(Exception):
            location.adopt_live_sidecars(allow_journal=False)
        if is_sqlite_busy(error):
            raise StorageBusy() from None
        raise
    location.adopt_live_sidecars(allow_journal=False)
    location.verify_live_sidecars(allow_journal=False)
    location.verify_live_database_identity()


def _commit(connection: sqlite3.Connection, location: AnchoredLocation) -> None:
    try:
        location.verify_live_database_identity()
        location.verify_live_sidecars(allow_journal=False)
        connection.commit()
        location.verify_live_sidecars(allow_journal=False)
        location.verify_live_database_identity()
    except Exception:
        raise _OperationUncertain() from None


def _release(
    connection: sqlite3.Connection | None, location: AnchoredLocation | None
) -> None:
    if connection is not None:
        if connection.in_transaction:
            with suppress(Exception):
                connection.rollback()
        with suppress(Exception):
            connection.close()
    if location is not None:
        with suppress(Exception):
            location.close()
