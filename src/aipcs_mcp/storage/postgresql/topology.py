"""PostgreSQL branch topology operations inside a caller-owned transaction.

This module deliberately owns neither readiness admission nor transaction
boundaries.  A data-store adapter supplies an already-open transaction for one
ready, materialised service namespace; this helper takes the row locks and
performs only topology/mutation/history work in that transaction.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from aipcs_mcp.records import (
    AssignBranchRecordsCommand,
    AssignedRecordRef,
    BranchAssignmentOutcome,
    BranchMutationOutcome,
    BranchPage,
    BranchValue,
    CreateBranchCommand,
    DataFailure,
    FrozenJsonObject,
    ListBranchesQuery,
    RecordContractError,
    RecordField,
    RecordSpecification,
    UpdateBranchCommand,
)

from .data_core import canonical_json, cursor, cursor_record, fingerprint, json_object, utc
from .service_store_migrations import BRANCH, HISTORY, MUTATION, RECORD_BRANCH, qualified

_MAX_REVISION = 2**63 - 1
_PRINCIPAL_MAX = 128
_CREATED_VIA_MAX = 128


class Connection(Protocol):
    def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> object: ...


class PostgreSQLBranchTopology:
    """Topology helper for an admitted caller-owned PostgreSQL transaction."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None, branch_ids: Callable[[], UUID] | None = None) -> None:
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        if branch_ids is not None and not callable(branch_ids):
            raise ValueError("branch_ids must be callable")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._branch_ids = branch_ids or uuid4

    def create_branch(
        self, connection: Connection, namespace: str, principal_id: str, created_via: str, command: CreateBranchCommand
    ) -> BranchMutationOutcome | DataFailure:
        if not _admitted(connection, namespace, principal_id, created_via, command, CreateBranchCommand):
            return DataFailure("validation_failed")
        request = _create_request(created_via, command)
        try:
            replay = _branch_replay(connection, namespace, principal_id, command.idempotency_key, "create_branch", request)
            if replay is not None:
                return replay
            if command.parent_branch_id is not None and _branch(connection, namespace, principal_id, command.parent_branch_id, lock=True) is None:
                return DataFailure("not_found")
            now = _now(self._clock)
            branch_id = _new_id(self._branch_ids)
            value = BranchValue(branch_id, command.slug, command.title, command.intent, command.branch_type, command.parent_branch_id, "active", command.retrieval_summary, now, now, 1)
            _execute(connection, f"INSERT INTO {qualified(namespace, BRANCH)} (id, principal_id, slug, title, intent, branch_type, parent_branch_id, status, retrieval_summary, branch_revision, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (branch_id, principal_id, value.slug, value.title, value.intent, value.branch_type, value.parent_branch_id, value.status, value.retrieval_summary, value.branch_revision, value.created_at, value.updated_at))
            _complete_mutation(connection, namespace, principal_id, command.idempotency_key, "create_branch", request, _branch_result(value), now)
            return BranchMutationOutcome(value, False)
        except Exception as error:
            return _data_failure(error)

    def list_branches(
        self, connection: Connection, namespace: str, principal_id: str, query: ListBranchesQuery
    ) -> BranchPage | DataFailure:
        if not _admitted(connection, namespace, principal_id, "read", query, ListBranchesQuery):
            return DataFailure("validation_failed")
        identity = fingerprint({"operation": "list_branches", "principal_id": principal_id, "status": query.status, "branch_type": query.branch_type, "parent_branch_id": _uuid(query.parent_branch_id)})
        try:
            position = cursor_record(query.page.cursor, identity)
            clauses = ["principal_id = %s"]
            parameters: list[object] = [principal_id]
            if position is not None:
                clauses.append("id > %s")
                parameters.append(position)
            if query.status is not None:
                clauses.append("status = %s")
                parameters.append(query.status)
            if query.branch_type is not None:
                clauses.append("branch_type = %s")
                parameters.append(query.branch_type)
            if query.parent_branch_id is not None:
                clauses.append("parent_branch_id = %s")
                parameters.append(query.parent_branch_id)
            rows = _fetchall(_execute(connection, f"SELECT id, slug, title, intent, branch_type, parent_branch_id, status, retrieval_summary, created_at, updated_at, branch_revision FROM {qualified(namespace, BRANCH)} WHERE {' AND '.join(clauses)} ORDER BY id LIMIT %s", tuple((*parameters, query.page.limit + 1))))
            values = tuple(_decode_branch(row) for row in rows[: query.page.limit])
            next_cursor = cursor(identity, values[-1].branch_id) if len(rows) > query.page.limit else None
            return BranchPage(values, next_cursor)
        except RecordContractError:
            return DataFailure("validation_failed")
        except Exception as error:
            return _data_failure(error)

    def update_branch(
        self, connection: Connection, namespace: str, principal_id: str, created_via: str, command: UpdateBranchCommand
    ) -> BranchMutationOutcome | DataFailure:
        if not _admitted(connection, namespace, principal_id, created_via, command, UpdateBranchCommand) or command.expected_branch_revision >= _MAX_REVISION:
            return DataFailure("validation_failed")
        request = _update_request(created_via, command)
        try:
            replay = _branch_replay(connection, namespace, principal_id, command.idempotency_key, "update_branch", request)
            if replay is not None:
                return replay
            current = _branch(connection, namespace, principal_id, command.branch_id, lock=True)
            if current is None:
                return DataFailure("not_found")
            if current.branch_revision != command.expected_branch_revision:
                return DataFailure("stale_branch_revision")
            updates = command.updates.to_dict()
            parent = _parse_parent(updates.get("parent_branch_id", _uuid(current.parent_branch_id)))
            if parent == current.branch_id or (parent is not None and _has_ancestor(connection, namespace, principal_id, parent, current.branch_id)):
                return DataFailure("constraint_violation")
            if parent is not None and _branch(connection, namespace, principal_id, parent, lock=True) is None:
                return DataFailure("not_found")
            candidate = BranchValue(current.branch_id, current.slug, updates.get("title", current.title), updates.get("intent", current.intent), updates.get("branch_type", current.branch_type), parent, updates.get("status", current.status), updates.get("retrieval_summary", current.retrieval_summary), current.created_at, _now(self._clock), current.branch_revision + 1)
            if _same_branch(current, candidate):
                _complete_mutation(connection, namespace, principal_id, command.idempotency_key, "update_branch", request, _branch_result(current), candidate.updated_at)
                return BranchMutationOutcome(current, False)
            updated = _execute(connection, f"UPDATE {qualified(namespace, BRANCH)} SET title=%s, intent=%s, branch_type=%s, parent_branch_id=%s, status=%s, retrieval_summary=%s, updated_at=%s, branch_revision=%s WHERE id=%s AND principal_id=%s AND branch_revision=%s", (candidate.title, candidate.intent, candidate.branch_type, candidate.parent_branch_id, candidate.status, candidate.retrieval_summary, candidate.updated_at, candidate.branch_revision, candidate.branch_id, principal_id, current.branch_revision))
            if _rowcount(updated) != 1:
                return DataFailure("stale_branch_revision")
            _complete_mutation(connection, namespace, principal_id, command.idempotency_key, "update_branch", request, _branch_result(candidate), candidate.updated_at)
            return BranchMutationOutcome(candidate, False)
        except Exception as error:
            return _data_failure(error)

    def assign_records(
        self, connection: Connection, namespace: str, principal_id: str, created_via: str, specification: RecordSpecification, command: AssignBranchRecordsCommand
    ) -> BranchAssignmentOutcome | DataFailure:
        if not _admitted(connection, namespace, principal_id, created_via, command, AssignBranchRecordsCommand) or type(specification) is not RecordSpecification or any(specification.entity(item.entity_name) is None for item in command.targets) or any(item.expected_record_version >= _MAX_REVISION for item in command.targets):
            return DataFailure("validation_failed")
        request = _assignment_request(created_via, command)
        try:
            replay = _assignment_replay(connection, namespace, principal_id, command.idempotency_key, request)
            if replay is not None:
                return replay
            branch = _branch(connection, namespace, principal_id, command.branch_id, lock=True)
            if branch is None:
                return DataFailure("not_found")
            if command.operation == "assign" and branch.status != "active":
                return DataFailure("constraint_violation")
            locked: list[tuple[object, FrozenJsonObject]] = []
            for target in command.targets:
                record = _record(connection, namespace, principal_id, specification, target.entity_name, target.record_id, lock=True)
                if record is None:
                    return DataFailure("not_found")
                version, fields = record
                if version != target.expected_record_version:
                    return DataFailure("stale_record_version")
                locked.append(record)
            now = _now(self._clock)
            results: list[AssignedRecordRef] = []
            for target, (_version, fields) in zip(command.targets, locked, strict=True):
                changed, kind = _mapping(connection, namespace, principal_id, command.branch_id, command.role, command.operation, target.entity_name, target.record_id, now)
                version = target.expected_record_version
                if changed:
                    version += 1
                    updated = _execute(connection, f"UPDATE {qualified(namespace, target.entity_name)} SET record_version=%s, updated_at=%s WHERE id=%s AND owner_id=%s AND record_version=%s", (version, now, target.record_id, principal_id, target.expected_record_version))
                    if _rowcount(updated) != 1:
                        return DataFailure("stale_record_version")
                    _history(connection, namespace, principal_id, target.entity_name, target.record_id, version, kind, fields, now)
                results.append(AssignedRecordRef(target.entity_name, target.record_id, version))
            outcome = BranchAssignmentOutcome(tuple(results), False)
            _complete_mutation(connection, namespace, principal_id, command.idempotency_key, "assign_branch_records", request, _assignment_result(outcome), now)
            return outcome
        except Exception as error:
            return _data_failure(error)


def observe_create_branch_replay(connection: Connection, namespace: str, principal_id: str, created_via: str, command: CreateBranchCommand) -> BranchMutationOutcome | None:
    return _observe_branch(connection, namespace, principal_id, command.idempotency_key, "create_branch", _create_request(created_via, command))


def observe_update_branch_replay(connection: Connection, namespace: str, principal_id: str, created_via: str, command: UpdateBranchCommand) -> BranchMutationOutcome | None:
    return _observe_branch(connection, namespace, principal_id, command.idempotency_key, "update_branch", _update_request(created_via, command))


def observe_assignment_command_replay(connection: Connection, namespace: str, principal_id: str, created_via: str, command: AssignBranchRecordsCommand) -> BranchAssignmentOutcome | None:
    row = _mutation_row(connection, namespace, principal_id, command.idempotency_key)
    if row is None:
        return None
    if row[0] != "assign_branch_records" or row[1] != _assignment_request(created_via, command):
        raise ValueError
    return BranchAssignmentOutcome(_decode_assignment(row[2]), True)


def _mapping(connection: Connection, namespace: str, principal: str, branch: UUID, role: str, operation: str, entity: str, record: UUID, now: datetime) -> tuple[bool, str]:
    row = _fetchone(_execute(connection, f"SELECT role FROM {qualified(namespace, RECORD_BRANCH)} WHERE principal_id=%s AND entity_name=%s AND record_id=%s AND branch_id=%s FOR UPDATE", (principal, entity, record, branch)))
    if operation == "unassign":
        if row is None or row[0] != role:
            return False, ""
        _execute(connection, f"DELETE FROM {qualified(namespace, RECORD_BRANCH)} WHERE principal_id=%s AND entity_name=%s AND record_id=%s AND branch_id=%s AND role=%s", (principal, entity, record, branch, role))
        return True, f"{role}_unassign"
    if role == "related":
        if row is not None:
            return False, ""
        _execute(connection, f"INSERT INTO {qualified(namespace, RECORD_BRANCH)} (principal_id, entity_name, record_id, branch_id, role, assigned_at) VALUES (%s,%s,%s,%s,'related',%s)", (principal, entity, record, branch, now))
        return True, "related_assign"
    if row is not None and row[0] == "primary":
        return False, ""
    previous = _fetchone(_execute(connection, f"SELECT branch_id FROM {qualified(namespace, RECORD_BRANCH)} WHERE principal_id=%s AND entity_name=%s AND record_id=%s AND role='primary' FOR UPDATE", (principal, entity, record)))
    if row is not None:
        _execute(connection, f"DELETE FROM {qualified(namespace, RECORD_BRANCH)} WHERE principal_id=%s AND entity_name=%s AND record_id=%s AND branch_id=%s", (principal, entity, record, branch))
    if previous is not None:
        _execute(connection, f"DELETE FROM {qualified(namespace, RECORD_BRANCH)} WHERE principal_id=%s AND entity_name=%s AND record_id=%s AND role='primary'", (principal, entity, record))
    _execute(connection, f"INSERT INTO {qualified(namespace, RECORD_BRANCH)} (principal_id, entity_name, record_id, branch_id, role, assigned_at) VALUES (%s,%s,%s,%s,'primary',%s)", (principal, entity, record, branch, now))
    return True, "primary_move" if previous is not None else "primary_assign"


def _branch(connection: Connection, namespace: str, principal: str, branch_id: UUID, *, lock: bool) -> BranchValue | None:
    suffix = " FOR UPDATE" if lock else ""
    row = _fetchone(_execute(connection, f"SELECT id, slug, title, intent, branch_type, parent_branch_id, status, retrieval_summary, created_at, updated_at, branch_revision FROM {qualified(namespace, BRANCH)} WHERE id=%s AND principal_id=%s{suffix}", (branch_id, principal)))
    return None if row is None else _decode_branch(row)


def _has_ancestor(connection: Connection, namespace: str, principal: str, start: UUID, sought: UUID) -> bool:
    current: UUID | None = start
    seen: set[UUID] = set()
    while current is not None:
        if current == sought or current in seen:
            return True
        seen.add(current)
        branch = _branch(connection, namespace, principal, current, lock=True)
        if branch is None:
            return False
        current = branch.parent_branch_id
    return False


def _record(connection: Connection, namespace: str, principal: str, specification: RecordSpecification, entity_name: str, record_id: UUID, *, lock: bool) -> tuple[int, FrozenJsonObject] | None:
    entity = specification.entity(entity_name)
    if entity is None:
        return None
    fields = tuple(field.name for field in entity.domain_fields)
    selected = ", ".join(("record_version", *(_quote(field) for field in fields)))
    suffix = " FOR UPDATE" if lock else ""
    row = _fetchone(_execute(connection, f"SELECT {selected} FROM {qualified(namespace, entity_name)} WHERE id=%s AND owner_id=%s{suffix}", (record_id, principal)))
    if row is None or type(row[0]) is not int:
        return None
    return row[0], specification.validate_create(
        entity_name,
        {
            field.name: _decode_field(field, value)
            for field, value in zip(entity.domain_fields, row[1:], strict=True)
        },
    )


def _decode_field(field: RecordField, value: object) -> object:
    """Translate native PostgreSQL domain values back to the public JSON contract."""

    if value is None:
        return None
    if field.logical_type == "uuid":
        result = value if type(value) is UUID else UUID(value)  # type: ignore[arg-type]
        if result.int == 0:
            raise ValueError
        return str(result)
    if field.logical_type == "datetime":
        return _time(value).isoformat().replace("+00:00", "Z")
    if field.logical_type == "string_list":
        if type(value) is not list:
            raise ValueError
        return list(value)
    return value


def _history(connection: Connection, namespace: str, principal: str, entity: str, record: UUID, version: int, kind: str, fields: FrozenJsonObject, now: datetime) -> None:
    snapshot = canonical_json(fields.to_dict())
    _execute(connection, f"INSERT INTO {qualified(namespace, HISTORY)} (principal_id, entity_name, record_id, record_version, event_kind, before_json, after_json, occurred_at) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)", (principal, entity, record, version, kind, snapshot, snapshot, now))


def _mutation_row(connection: Connection, namespace: str, principal: str, key: str) -> tuple[object, ...] | None:
    return _fetchone(_execute(connection, f"SELECT operation_kind, fingerprint, result_json FROM {qualified(namespace, MUTATION)} WHERE principal_id=%s AND idempotency_key=%s FOR UPDATE", (principal, key)))


def _branch_replay(connection: Connection, namespace: str, principal: str, key: str, operation: str, request: str) -> BranchMutationOutcome | DataFailure | None:
    row = _mutation_row(connection, namespace, principal, key)
    if row is None:
        return None
    if row[0] != operation or row[1] != request:
        return DataFailure("changed_fingerprint")
    return BranchMutationOutcome(_decode_branch_result(row[2]), True)


def _assignment_replay(connection: Connection, namespace: str, principal: str, key: str, request: str) -> BranchAssignmentOutcome | DataFailure | None:
    row = _mutation_row(connection, namespace, principal, key)
    if row is None:
        return None
    if row[0] != "assign_branch_records" or row[1] != request:
        return DataFailure("changed_fingerprint")
    return BranchAssignmentOutcome(_decode_assignment(row[2]), True)


def _observe_branch(connection: Connection, namespace: str, principal: str, key: str, operation: str, request: str) -> BranchMutationOutcome | None:
    row = _mutation_row(connection, namespace, principal, key)
    if row is None:
        return None
    if row[0] != operation or row[1] != request:
        raise ValueError
    return BranchMutationOutcome(_decode_branch_result(row[2]), True)


def _complete_mutation(connection: Connection, namespace: str, principal: str, key: str, operation: str, request: str, result: dict[str, object], now: datetime) -> None:
    _execute(connection, f"INSERT INTO {qualified(namespace, MUTATION)} (principal_id, idempotency_key, operation_kind, fingerprint, result_json, completed_at) VALUES (%s,%s,%s,%s,%s::jsonb,%s)", (principal, key, operation, request, canonical_json(result), now))


def _create_request(created_via: str, command: CreateBranchCommand) -> str:
    return fingerprint({"operation": "create_branch", "created_via": created_via, "slug": command.slug, "title": command.title, "intent": command.intent, "branch_type": command.branch_type, "parent_branch_id": _uuid(command.parent_branch_id), "retrieval_summary": command.retrieval_summary})


def _update_request(created_via: str, command: UpdateBranchCommand) -> str:
    return fingerprint({"operation": "update_branch", "created_via": created_via, "branch_id": str(command.branch_id), "expected_branch_revision": command.expected_branch_revision, "updates": command.updates.to_dict()})


def _assignment_request(created_via: str, command: AssignBranchRecordsCommand) -> str:
    return fingerprint({"operation": "assign_branch_records", "created_via": created_via, "branch_id": str(command.branch_id), "role": command.role, "operation_kind": command.operation, "targets": [{"entity_name": item.entity_name, "record_id": str(item.record_id), "expected_record_version": item.expected_record_version} for item in command.targets]})


def _branch_result(branch: BranchValue) -> dict[str, object]:
    return {"kind": "branch", "branch": {"id": str(branch.branch_id), "slug": branch.slug, "title": branch.title, "intent": branch.intent, "branch_type": branch.branch_type, "parent_branch_id": _uuid(branch.parent_branch_id), "status": branch.status, "retrieval_summary": branch.retrieval_summary, "created_at": branch.created_at.isoformat().replace("+00:00", "Z"), "updated_at": branch.updated_at.isoformat().replace("+00:00", "Z"), "branch_revision": branch.branch_revision}}


def _assignment_result(outcome: BranchAssignmentOutcome) -> dict[str, object]:
    return {"kind": "branch_assignment", "records": [{"entity_name": value.entity_name, "id": str(value.record_id), "record_version": value.record_version} for value in outcome.records]}


def _decode_branch_result(value: object) -> BranchValue:
    raw = json_object(value)
    item = raw.to_dict().get("branch")
    if type(item) is FrozenJsonObject:
        item = item.to_dict()
    if raw.to_dict().get("kind") != "branch" or type(item) is not dict:
        raise ValueError
    return _branch_from_values(item)


def _decode_assignment(value: object) -> tuple[AssignedRecordRef, ...]:
    raw = json_object(value).to_dict()
    values = raw.get("records")
    if raw.get("kind") != "branch_assignment" or type(values) is not list:
        raise ValueError
    result = tuple(
        AssignedRecordRef(item["entity_name"], UUID(item["id"]), item["record_version"])
        for item in values
        if type(item) is dict
    )
    if len(result) != len(values):
        raise ValueError
    return result


def _decode_branch(row: tuple[object, ...]) -> BranchValue:
    return _branch_from_values({"id": row[0], "slug": row[1], "title": row[2], "intent": row[3], "branch_type": row[4], "parent_branch_id": row[5], "status": row[6], "retrieval_summary": row[7], "created_at": row[8], "updated_at": row[9], "branch_revision": row[10]})


def _branch_from_values(value: dict[str, object]) -> BranchValue:
    branch_id = value["id"] if type(value["id"]) is UUID else UUID(value["id"])  # type: ignore[arg-type]
    parent = value["parent_branch_id"] if type(value["parent_branch_id"]) is UUID or value["parent_branch_id"] is None else UUID(value["parent_branch_id"])  # type: ignore[arg-type]
    created = _time(value["created_at"])
    updated = _time(value["updated_at"])
    return BranchValue(branch_id, value["slug"], value["title"], value["intent"], value["branch_type"], parent, value["status"], value["retrieval_summary"], created, updated, value["branch_revision"])  # type: ignore[arg-type]


def _same_branch(left: BranchValue, right: BranchValue) -> bool:
    return (left.title, left.intent, left.branch_type, left.parent_branch_id, left.status, left.retrieval_summary) == (right.title, right.intent, right.branch_type, right.parent_branch_id, right.status, right.retrieval_summary)


def _admitted(connection: object, namespace: object, principal: object, created_via: object, value: object, expected: type) -> bool:
    return hasattr(connection, "execute") and _namespace(namespace) and _text(principal, _PRINCIPAL_MAX) and _text(created_via, _CREATED_VIA_MAX) and type(value) is expected


def _namespace(value: object) -> bool:
    return type(value) is str and len(value) == 36 and value.startswith("svc_") and all(char in "0123456789abcdef" for char in value[4:])


def _text(value: object, maximum: int) -> bool:
    return type(value) is str and bool(value) and value == value.strip() and len(value) <= maximum and "\x00" not in value


def _parse_parent(value: object) -> UUID | None:
    if value is None:
        return None
    result = UUID(value) if type(value) is str else value
    if type(result) is not UUID or result.int == 0:
        raise ValueError
    return result


def _uuid(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def _new_id(factory: Callable[[], UUID]) -> UUID:
    value = factory()
    if type(value) is not UUID or value.int == 0:
        raise ValueError
    return value


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if type(value) is not datetime or value.tzinfo is not UTC or value.utcoffset() != timedelta(0):
        raise ValueError
    return value


def _time(value: object) -> datetime:
    if type(value) is str:
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return utc(value).astimezone(UTC)


def _data_failure(error: BaseException) -> DataFailure:
    """Only convert expected data faults; preserve driver faults for the caller.

    The composed data store owns transaction rollback and phase-aware mapping of
    lock, cancellation, connection, and post-commit failures.  Flattening a
    SQLSTATE here would make a retryable mutation indistinguishable from an
    ordinary data rejection.
    """

    state = getattr(error, "sqlstate", None)
    if type(state) is str:
        if state == "23505":
            return DataFailure("already_exists")
        if state.startswith("23"):
            return DataFailure("constraint_violation")
        raise error
    if isinstance(error, (KeyError, RecordContractError, TypeError, ValueError)):
        return DataFailure("internal_error")
    raise error


def _execute(connection: Connection, statement: str, parameters: tuple[object, ...] = ()) -> object:
    return connection.execute(statement, parameters)


def _fetchone(result: object) -> tuple[object, ...] | None:
    method = getattr(result, "fetchone", None)
    if not callable(method):
        raise ValueError
    row = method()
    return None if row is None else tuple(row)


def _fetchall(result: object) -> list[tuple[object, ...]]:
    method = getattr(result, "fetchall", None)
    if not callable(method):
        raise ValueError
    return [tuple(row) for row in method()]


def _rowcount(result: object) -> int:
    value = getattr(result, "rowcount", None)
    if type(value) is not int:
        raise ValueError
    return value


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'
