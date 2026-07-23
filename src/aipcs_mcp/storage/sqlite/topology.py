"""Bounded SQLite branch topology operations for one materialised service.

The adapter deliberately operates on a caller-owned SQLite transaction.  This
keeps location, readiness, and domain-schema admission in ``data_store`` while
making branch mutation semantics independently testable and reusable by later
storage adapters.  Results are always public record vocabulary or
``DataFailure``; storage implementation details never cross this boundary.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from aipcs_mcp.numeric_domain import is_exact_number, is_signed_64_integer
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
    PageCursor,
    RecordContractError,
    RecordField,
    RecordSpecification,
    UpdateBranchCommand,
)
from aipcs_mcp.storage.codecs import decode_time, encode_time

from .service_store_migrations import (
    R3_BRANCH_TABLE,
    R3_HISTORY_TABLE,
    R3_MUTATION_TABLE,
    R3_RECORD_BRANCH_TABLE,
)

_MAX_REVISION = 2**63 - 1
_PRINCIPAL_MAX = 128
_CREATED_VIA_MAX = 128


class SQLiteBranchTopology:
    """Branch and record-assignment operations inside an admitted transaction.

    Callers must already have verified exact R3 readiness, the materialised
    domain schema, and started a read or write transaction.  Mutations use the
    supplied connection atomically; callers decide whether to commit.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        branch_ids: Callable[[], UUID] | None = None,
    ) -> None:
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        if branch_ids is not None and not callable(branch_ids):
            raise ValueError("branch_ids must be callable")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._branch_ids = branch_ids or uuid4

    def create_branch(
        self,
        connection: sqlite3.Connection,
        principal_id: str,
        created_via: str,
        command: CreateBranchCommand,
    ) -> BranchMutationOutcome | DataFailure:
        if not _admitted(connection, principal_id, created_via, command, CreateBranchCommand):
            return DataFailure("validation_failed")
        fingerprint = _fingerprint(
            {
                "operation": "create_branch",
                "created_via": created_via,
                "slug": command.slug,
                "title": command.title,
                "intent": command.intent,
                "branch_type": command.branch_type,
                "parent_branch_id": _uuid_text(command.parent_branch_id),
                "retrieval_summary": command.retrieval_summary,
            }
        )
        try:
            replay = _branch_replay(connection, principal_id, command.idempotency_key, "create_branch", fingerprint)
            if replay is not None:
                return replay
            if command.parent_branch_id is not None and not _branch_exists(
                connection, principal_id, command.parent_branch_id
            ):
                return DataFailure("not_found")
            now = _now(self._clock)
            branch_id = _new_id(self._branch_ids)
            branch = BranchValue(
                branch_id,
                command.slug,
                command.title,
                command.intent,
                command.branch_type,
                command.parent_branch_id,
                "active",
                command.retrieval_summary,
                now,
                now,
                1,
            )
            connection.execute(
                f'INSERT INTO "{R3_BRANCH_TABLE}" '
                '("id","principal_id","slug","title","intent","branch_type","parent_branch_id",'
                '"status","retrieval_summary","branch_revision","created_at","updated_at") '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (
                    str(branch.branch_id), principal_id, branch.slug, branch.title, branch.intent,
                    branch.branch_type, _uuid_text(branch.parent_branch_id), branch.status,
                    branch.retrieval_summary, branch.branch_revision, encode_time(branch.created_at),
                    encode_time(branch.updated_at),
                ),
            )
            _complete_mutation(
                connection, principal_id, command.idempotency_key, "create_branch", fingerprint,
                _encode_branch_result(branch), now,
            )
            return BranchMutationOutcome(branch, False)
        except sqlite3.IntegrityError:
            return DataFailure("already_exists")
        except sqlite3.DatabaseError:
            return DataFailure("internal_error")
        except Exception:
            return DataFailure("internal_error")

    def list_branches(
        self,
        connection: sqlite3.Connection,
        principal_id: str,
        query: ListBranchesQuery,
    ) -> BranchPage | DataFailure:
        if not _admitted(connection, principal_id, "read", query, ListBranchesQuery):
            return DataFailure("validation_failed")
        identity = _fingerprint(
            {
                "operation": "list_branches",
                "status": query.status,
                "branch_type": query.branch_type,
                "parent_branch_id": _uuid_text(query.parent_branch_id),
            }
        )
        try:
            position = _cursor_position(query.page.cursor, identity)
            clauses = ['"principal_id"=?', '"id">?']
            parameters: list[object] = [principal_id, position or ""]
            if query.status is not None:
                clauses.append('"status"=?')
                parameters.append(query.status)
            if query.branch_type is not None:
                clauses.append('"branch_type"=?')
                parameters.append(query.branch_type)
            if query.parent_branch_id is not None:
                clauses.append('"parent_branch_id"=?')
                parameters.append(str(query.parent_branch_id))
            rows = connection.execute(
                f'SELECT * FROM "{R3_BRANCH_TABLE}" WHERE {" AND ".join(clauses)} '
                'ORDER BY "id" LIMIT ?',
                (*parameters, query.page.limit + 1),
            ).fetchall()
            values = tuple(_decode_branch(row) for row in rows[: query.page.limit])
            next_cursor = (
                _encode_cursor(identity, str(rows[query.page.limit - 1]["id"]))
                if len(rows) > query.page.limit
                else None
            )
            return BranchPage(values, next_cursor)
        except RecordContractError:
            return DataFailure("validation_failed")
        except sqlite3.DatabaseError:
            return DataFailure("internal_error")
        except Exception:
            return DataFailure("internal_error")

    def update_branch(
        self,
        connection: sqlite3.Connection,
        principal_id: str,
        created_via: str,
        command: UpdateBranchCommand,
    ) -> BranchMutationOutcome | DataFailure:
        if not _admitted(connection, principal_id, created_via, command, UpdateBranchCommand):
            return DataFailure("validation_failed")
        if command.expected_branch_revision >= _MAX_REVISION:
            return DataFailure("validation_failed")
        fingerprint = _fingerprint(
            {
                "operation": "update_branch",
                "created_via": created_via,
                "branch_id": str(command.branch_id),
                "expected_branch_revision": command.expected_branch_revision,
                "updates": command.updates.to_dict(),
            }
        )
        try:
            replay = _branch_replay(connection, principal_id, command.idempotency_key, "update_branch", fingerprint)
            if replay is not None:
                return replay
            row = _branch_row(connection, principal_id, command.branch_id)
            if row is None:
                return DataFailure("not_found")
            current = _decode_branch(row)
            if current.branch_revision != command.expected_branch_revision:
                return DataFailure("stale_branch_revision")
            updates = command.updates.to_dict()
            parent = _parse_parent(updates.get("parent_branch_id", _uuid_text(current.parent_branch_id)))
            if parent == current.branch_id:
                return DataFailure("constraint_violation")
            if parent is not None:
                if not _branch_exists(connection, principal_id, parent):
                    return DataFailure("not_found")
                if _has_ancestor(connection, principal_id, parent, current.branch_id):
                    return DataFailure("constraint_violation")
            candidate = {
                "title": updates.get("title", current.title),
                "intent": updates.get("intent", current.intent),
                "branch_type": updates.get("branch_type", current.branch_type),
                "parent_branch_id": parent,
                "status": updates.get("status", current.status),
                "retrieval_summary": updates.get("retrieval_summary", current.retrieval_summary),
            }
            changed = (
                candidate["title"] != current.title
                or candidate["intent"] != current.intent
                or candidate["branch_type"] != current.branch_type
                or candidate["parent_branch_id"] != current.parent_branch_id
                or candidate["status"] != current.status
                or candidate["retrieval_summary"] != current.retrieval_summary
            )
            if not changed:
                _complete_mutation(
                    connection, principal_id, command.idempotency_key, "update_branch", fingerprint,
                    _encode_branch_result(current), _now(self._clock),
                )
                return BranchMutationOutcome(current, False)
            now = _now(self._clock)
            next_value = BranchValue(
                current.branch_id, current.slug, candidate["title"], candidate["intent"],
                candidate["branch_type"], candidate["parent_branch_id"], candidate["status"],
                candidate["retrieval_summary"], current.created_at, now, current.branch_revision + 1,
            )
            result = connection.execute(
                f'UPDATE "{R3_BRANCH_TABLE}" SET "title"=?,"intent"=?,"branch_type"=?,'
                '"parent_branch_id"=?,"status"=?,"retrieval_summary"=?,"updated_at"=?,'
                '"branch_revision"=? WHERE "id"=? AND "principal_id"=? AND "branch_revision"=?',
                (
                    next_value.title, next_value.intent, next_value.branch_type,
                    _uuid_text(next_value.parent_branch_id), next_value.status,
                    next_value.retrieval_summary, encode_time(next_value.updated_at),
                    next_value.branch_revision, str(next_value.branch_id), principal_id,
                    current.branch_revision,
                ),
            )
            if result.rowcount != 1:
                return DataFailure("stale_branch_revision")
            _complete_mutation(
                connection, principal_id, command.idempotency_key, "update_branch", fingerprint,
                _encode_branch_result(next_value), now,
            )
            return BranchMutationOutcome(next_value, False)
        except sqlite3.IntegrityError:
            return DataFailure("constraint_violation")
        except sqlite3.DatabaseError:
            return DataFailure("internal_error")
        except Exception:
            return DataFailure("internal_error")

    def assign_records(
        self,
        connection: sqlite3.Connection,
        principal_id: str,
        created_via: str,
        specification: RecordSpecification,
        command: AssignBranchRecordsCommand,
    ) -> BranchAssignmentOutcome | DataFailure:
        if not _admitted(connection, principal_id, created_via, command, AssignBranchRecordsCommand):
            return DataFailure("validation_failed")
        if type(specification) is not RecordSpecification or any(
            specification.entity(target.entity_name) is None for target in command.targets
        ):
            return DataFailure("validation_failed")
        if any(target.expected_record_version >= _MAX_REVISION for target in command.targets):
            return DataFailure("validation_failed")
        fingerprint = _fingerprint(
            {
                "operation": "assign_branch_records",
                "created_via": created_via,
                "branch_id": str(command.branch_id),
                "role": command.role,
                "operation_kind": command.operation,
                "targets": [
                    {
                        "entity_name": target.entity_name,
                        "record_id": str(target.record_id),
                        "expected_record_version": target.expected_record_version,
                    }
                    for target in command.targets
                ],
            }
        )
        try:
            replay = _assignment_replay(connection, principal_id, command.idempotency_key, fingerprint)
            if replay is not None:
                return replay
            branch = _branch_row(connection, principal_id, command.branch_id)
            if branch is None:
                return DataFailure("not_found")
            if command.operation == "assign" and branch["status"] != "active":
                return DataFailure("constraint_violation")
            # Validate every target before any mapping changes: the command is atomic.
            records: list[tuple[object, FrozenJsonObject]] = []
            for target in command.targets:
                row = _record_row(connection, principal_id, target.entity_name, target.record_id)
                if row is None:
                    return DataFailure("not_found")
                if row["record_version"] != target.expected_record_version:
                    return DataFailure("stale_record_version")
                records.append((row, _record_fields(row, specification, target.entity_name)))
            now = _now(self._clock)
            resulting: list[AssignedRecordRef] = []
            for target, (_row, fields) in zip(command.targets, records, strict=True):
                changed, kind = _apply_mapping(
                    connection, principal_id, command.branch_id, command.role, command.operation,
                    target.entity_name, target.record_id, now,
                )
                version = target.expected_record_version
                if changed:
                    version += 1
                    update = connection.execute(
                        f'UPDATE {_quote(target.entity_name)} SET "record_version"=?,"updated_at"=? '
                        'WHERE "id"=? AND "owner_id"=? AND "record_version"=?',
                        (version, encode_time(now), str(target.record_id), principal_id, target.expected_record_version),
                    )
                    if update.rowcount != 1:
                        return DataFailure("stale_record_version")
                    _append_topology_history(
                        connection, principal_id, target.entity_name, target.record_id, version, kind, fields, now
                    )
                resulting.append(AssignedRecordRef(target.entity_name, target.record_id, version))
            outcome = BranchAssignmentOutcome(tuple(resulting), False)
            _complete_mutation(
                connection, principal_id, command.idempotency_key, "assign_branch_records", fingerprint,
                _encode_assignment_result(outcome), now,
            )
            return outcome
        except sqlite3.IntegrityError:
            return DataFailure("constraint_violation")
        except sqlite3.DatabaseError:
            return DataFailure("internal_error")
        except Exception:
            return DataFailure("internal_error")


def _apply_mapping(
    connection: sqlite3.Connection,
    principal_id: str,
    branch_id: UUID,
    role: str,
    operation: str,
    entity_name: str,
    record_id: UUID,
    now: datetime,
) -> tuple[bool, str]:
    """Apply one membership delta and return its history kind, if effective."""

    mapping = connection.execute(
        f'SELECT "role" FROM "{R3_RECORD_BRANCH_TABLE}" WHERE '
        '"principal_id"=? AND "entity_name"=? AND "record_id"=? AND "branch_id"=?',
        (principal_id, entity_name, str(record_id), str(branch_id)),
    ).fetchone()
    if operation == "unassign":
        if mapping is None or mapping["role"] != role:
            return False, ""
        connection.execute(
            f'DELETE FROM "{R3_RECORD_BRANCH_TABLE}" WHERE '
            '"principal_id"=? AND "entity_name"=? AND "record_id"=? AND "branch_id"=?',
            (principal_id, entity_name, str(record_id), str(branch_id)),
        )
        return True, f"{role}_unassign"
    if role == "related":
        # A primary assignment already expresses membership in that same branch.
        if mapping is not None:
            return False, ""
        connection.execute(
            f'INSERT INTO "{R3_RECORD_BRANCH_TABLE}" '
            '("principal_id","entity_name","record_id","branch_id","role","assigned_at") '
            'VALUES (?,?,?,?,?,?)',
            (principal_id, entity_name, str(record_id), str(branch_id), "related", encode_time(now)),
        )
        return True, "related_assign"
    if mapping is not None and mapping["role"] == "primary":
        return False, ""
    previous = connection.execute(
        f'SELECT "branch_id" FROM "{R3_RECORD_BRANCH_TABLE}" WHERE '
        '"principal_id"=? AND "entity_name"=? AND "record_id"=? AND "role"=\'primary\'',
        (principal_id, entity_name, str(record_id)),
    ).fetchone()
    if mapping is not None:
        connection.execute(
            f'DELETE FROM "{R3_RECORD_BRANCH_TABLE}" WHERE '
            '"principal_id"=? AND "entity_name"=? AND "record_id"=? AND "branch_id"=?',
            (principal_id, entity_name, str(record_id), str(branch_id)),
        )
    if previous is not None:
        connection.execute(
            f'DELETE FROM "{R3_RECORD_BRANCH_TABLE}" WHERE '
            '"principal_id"=? AND "entity_name"=? AND "record_id"=? AND "role"=\'primary\'',
            (principal_id, entity_name, str(record_id)),
        )
    connection.execute(
        f'INSERT INTO "{R3_RECORD_BRANCH_TABLE}" '
        '("principal_id","entity_name","record_id","branch_id","role","assigned_at") '
        'VALUES (?,?,?,?,?,?)',
        (principal_id, entity_name, str(record_id), str(branch_id), "primary", encode_time(now)),
    )
    return True, "primary_move" if previous is not None else "primary_assign"


def _branch_replay(
    connection: sqlite3.Connection, principal_id: str, key: str, operation: str, fingerprint: str
) -> BranchMutationOutcome | DataFailure | None:
    row = _mutation_row(connection, principal_id, key)
    if row is None:
        return None
    if row["operation_kind"] != operation or row["fingerprint"] != fingerprint:
        return DataFailure("changed_fingerprint")
    try:
        return BranchMutationOutcome(_decode_branch_result(row["result_json"]), True)
    except Exception:
        return DataFailure("internal_error")


def observe_branch_replay(
    connection: sqlite3.Connection,
    principal_id: str,
    key: str,
    operation: str,
    fingerprint: str,
) -> BranchMutationOutcome | None:
    """Decode only exact completed branch evidence after a commit uncertainty.

    A missing row is the sole proof that the transaction was not committed.
    Mismatched or malformed evidence deliberately raises so the caller returns
    ``operation_uncertain`` rather than exposing or interpreting internals.
    """

    row = _mutation_row(connection, principal_id, key)
    if row is None:
        return None
    if row["operation_kind"] != operation or row["fingerprint"] != fingerprint:
        raise ValueError
    return BranchMutationOutcome(_decode_branch_result(row["result_json"]), True)


def _assignment_replay(
    connection: sqlite3.Connection, principal_id: str, key: str, fingerprint: str
) -> BranchAssignmentOutcome | DataFailure | None:
    row = _mutation_row(connection, principal_id, key)
    if row is None:
        return None
    if row["operation_kind"] != "assign_branch_records" or row["fingerprint"] != fingerprint:
        return DataFailure("changed_fingerprint")
    try:
        return BranchAssignmentOutcome(_decode_assignment_result(row["result_json"]), True)
    except Exception:
        return DataFailure("internal_error")


def observe_assignment_replay(
    connection: sqlite3.Connection,
    principal_id: str,
    key: str,
    fingerprint: str,
) -> BranchAssignmentOutcome | None:
    """Decode only exact completed assignment evidence after uncertain commit."""

    row = _mutation_row(connection, principal_id, key)
    if row is None:
        return None
    if row["operation_kind"] != "assign_branch_records" or row["fingerprint"] != fingerprint:
        raise ValueError
    return BranchAssignmentOutcome(_decode_assignment_result(row["result_json"]), True)


def observe_create_branch_replay(
    connection: sqlite3.Connection,
    principal_id: str,
    created_via: str,
    command: CreateBranchCommand,
) -> BranchMutationOutcome | None:
    """Observe exact create-branch evidence without re-executing the mutation."""

    return observe_branch_replay(
        connection,
        principal_id,
        command.idempotency_key,
        "create_branch",
        _fingerprint(
            {
                "operation": "create_branch",
                "created_via": created_via,
                "slug": command.slug,
                "title": command.title,
                "intent": command.intent,
                "branch_type": command.branch_type,
                "parent_branch_id": _uuid_text(command.parent_branch_id),
                "retrieval_summary": command.retrieval_summary,
            }
        ),
    )


def observe_update_branch_replay(
    connection: sqlite3.Connection,
    principal_id: str,
    created_via: str,
    command: UpdateBranchCommand,
) -> BranchMutationOutcome | None:
    """Observe exact update-branch evidence without re-executing the mutation."""

    return observe_branch_replay(
        connection,
        principal_id,
        command.idempotency_key,
        "update_branch",
        _fingerprint(
            {
                "operation": "update_branch",
                "created_via": created_via,
                "branch_id": str(command.branch_id),
                "expected_branch_revision": command.expected_branch_revision,
                "updates": command.updates.to_dict(),
            }
        ),
    )


def observe_assignment_command_replay(
    connection: sqlite3.Connection,
    principal_id: str,
    created_via: str,
    command: AssignBranchRecordsCommand,
) -> BranchAssignmentOutcome | None:
    """Observe exact branch-assignment evidence without re-executing it."""

    return observe_assignment_replay(
        connection,
        principal_id,
        command.idempotency_key,
        _fingerprint(
            {
                "operation": "assign_branch_records",
                "created_via": created_via,
                "branch_id": str(command.branch_id),
                "role": command.role,
                "operation_kind": command.operation,
                "targets": [
                    {
                        "entity_name": target.entity_name,
                        "record_id": str(target.record_id),
                        "expected_record_version": target.expected_record_version,
                    }
                    for target in command.targets
                ],
            }
        ),
    )


def _mutation_row(connection: sqlite3.Connection, principal_id: str, key: str) -> sqlite3.Row | None:
    return connection.execute(
        f'SELECT "operation_kind","fingerprint","result_json" FROM "{R3_MUTATION_TABLE}" '
        'WHERE "principal_id"=? AND "idempotency_key"=?', (principal_id, key)
    ).fetchone()


def _complete_mutation(
    connection: sqlite3.Connection, principal_id: str, key: str, operation: str,
    fingerprint: str, result_json: str, now: datetime,
) -> None:
    connection.execute(
        f'INSERT INTO "{R3_MUTATION_TABLE}" '
        '("principal_id","idempotency_key","operation_kind","fingerprint","result_json","completed_at") '
        'VALUES (?,?,?,?,?,?)', (principal_id, key, operation, fingerprint, result_json, encode_time(now))
    )


def _branch_row(connection: sqlite3.Connection, principal_id: str, branch_id: UUID) -> sqlite3.Row | None:
    return connection.execute(
        f'SELECT * FROM "{R3_BRANCH_TABLE}" WHERE "id"=? AND "principal_id"=?',
        (str(branch_id), principal_id),
    ).fetchone()


def _branch_exists(connection: sqlite3.Connection, principal_id: str, branch_id: UUID) -> bool:
    return _branch_row(connection, principal_id, branch_id) is not None


def _has_ancestor(
    connection: sqlite3.Connection, principal_id: str, start: UUID, sought: UUID
) -> bool:
    """Walk only parent links, bounded by branch ids, to reject a cycle."""

    current: UUID | None = start
    seen: set[UUID] = set()
    while current is not None:
        if current == sought:
            return True
        if current in seen:
            return True
        seen.add(current)
        row = _branch_row(connection, principal_id, current)
        if row is None:
            return False
        current = _parse_parent(row["parent_branch_id"])
    return False


def _record_row(
    connection: sqlite3.Connection, principal_id: str, entity_name: str, record_id: UUID
) -> sqlite3.Row | None:
    return connection.execute(
        f'SELECT * FROM {_quote(entity_name)} WHERE "id"=? AND "owner_id"=?',
        (str(record_id), principal_id),
    ).fetchone()


def _record_fields(row: Mapping[str, Any], specification: RecordSpecification, entity_name: str) -> FrozenJsonObject:
    entity = specification.entity(entity_name)
    if entity is None:
        raise ValueError
    values = {field.name: _decode_field(field, row[field.name]) for field in entity.domain_fields}
    return specification.validate_create(entity_name, values)


def _append_topology_history(
    connection: sqlite3.Connection, principal_id: str, entity_name: str, record_id: UUID,
    record_version: int, kind: str, fields: FrozenJsonObject, now: datetime,
) -> None:
    snapshot = _canonical_json(fields.to_dict())
    connection.execute(
        f'INSERT INTO "{R3_HISTORY_TABLE}" '
        '("principal_id","entity_name","record_id","record_version","event_kind",'
        '"before_json","after_json","occurred_at") VALUES (?,?,?,?,?,?,?,?)',
        (principal_id, entity_name, str(record_id), record_version, kind, snapshot, snapshot, encode_time(now)),
    )


def _decode_branch(row: Mapping[str, Any]) -> BranchValue:
    branch = BranchValue(
        UUID(row["id"]), row["slug"], row["title"], row["intent"], row["branch_type"],
        _parse_parent(row["parent_branch_id"]), row["status"], row["retrieval_summary"],
        decode_time(row["created_at"]), decode_time(row["updated_at"]), _revision(row["branch_revision"]),
    )
    if str(branch.branch_id) != row["id"]:
        raise ValueError
    return branch


def _encode_branch_result(branch: BranchValue) -> str:
    return _canonical_json(
        {"kind": "branch", "branch": {
            "id": str(branch.branch_id), "slug": branch.slug, "title": branch.title,
            "intent": branch.intent, "branch_type": branch.branch_type,
            "parent_branch_id": _uuid_text(branch.parent_branch_id), "status": branch.status,
            "retrieval_summary": branch.retrieval_summary, "created_at": encode_time(branch.created_at),
            "updated_at": encode_time(branch.updated_at), "branch_revision": branch.branch_revision,
        }}
    )


def _decode_branch_result(text: object) -> BranchValue:
    raw = _decode_canonical_json(text)
    if type(raw) is not dict or set(raw) != {"kind", "branch"} or raw["kind"] != "branch":
        raise ValueError
    item = raw["branch"]
    if type(item) is not dict or set(item) != {
        "id", "slug", "title", "intent", "branch_type", "parent_branch_id", "status",
        "retrieval_summary", "created_at", "updated_at", "branch_revision",
    }:
        raise ValueError
    branch = BranchValue(
        UUID(item["id"]), item["slug"], item["title"], item["intent"], item["branch_type"],
        _parse_parent(item["parent_branch_id"]), item["status"], item["retrieval_summary"],
        decode_time(item["created_at"]), decode_time(item["updated_at"]), _revision(item["branch_revision"]),
    )
    if _encode_branch_result(branch) != text:
        raise ValueError
    return branch


def _encode_assignment_result(value: BranchAssignmentOutcome) -> str:
    return _canonical_json({"kind": "branch_assignment", "records": [
        {"entity_name": item.entity_name, "id": str(item.record_id), "record_version": item.record_version}
        for item in value.records
    ]})


def _decode_assignment_result(text: object) -> tuple[AssignedRecordRef, ...]:
    raw = _decode_canonical_json(text)
    if type(raw) is not dict or set(raw) != {"kind", "records"} or raw["kind"] != "branch_assignment":
        raise ValueError
    values = raw["records"]
    if type(values) is not list:
        raise ValueError
    result = tuple(AssignedRecordRef(item["entity_name"], UUID(item["id"]), _revision(item["record_version"])) for item in values if type(item) is dict and set(item) == {"entity_name", "id", "record_version"})
    if len(result) != len(values) or not result:
        raise ValueError
    outcome = BranchAssignmentOutcome(result, False)
    if _encode_assignment_result(outcome) != text:
        raise ValueError
    return result


def _encode_cursor(identity: str, position: str) -> PageCursor:
    raw = _canonical_json({"v": 1, "kind": "branch", "query": identity, "position": position})
    return PageCursor(base64.urlsafe_b64encode(raw.encode()).decode().rstrip("="))


def _cursor_position(cursor: PageCursor | None, identity: str) -> str | None:
    if cursor is None:
        return None
    try:
        padding = "=" * (-len(cursor.value) % 4)
        text = base64.b64decode(cursor.value + padding, altchars=b"-_", validate=True).decode()
        raw = json.loads(text)
        if _canonical_json(raw) != text or type(raw) is not dict or set(raw) != {
            "v", "kind", "query", "position"
        } or raw["v"] != 1 or raw["kind"] != "branch" or raw["query"] != identity:
            raise ValueError
        value = UUID(raw["position"])
        if type(raw["position"]) is not str or str(value) != raw["position"] or value.int == 0:
            raise ValueError
        return raw["position"]
    except Exception as error:
        raise RecordContractError() from error


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
        decoded = _decode_canonical_json(value)
        if type(decoded) is not list:
            raise ValueError
        return decoded
    if type(value) is not str:
        raise ValueError
    return value


def _admitted(connection: object, principal_id: object, created_via: object, value: object, expected: type) -> bool:
    return (
        isinstance(connection, sqlite3.Connection)
        and type(principal_id) is str and bool(principal_id) and principal_id == principal_id.strip()
        and len(principal_id) <= _PRINCIPAL_MAX and "\x00" not in principal_id
        and type(created_via) is str and bool(created_via) and created_via == created_via.strip()
        and len(created_via) <= _CREATED_VIA_MAX and "\x00" not in created_via
        and type(value) is expected
    )


def _parse_parent(value: object) -> UUID | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError
    result = UUID(value)
    if str(result) != value or result.int == 0:
        raise ValueError
    return result


def _uuid_text(value: UUID | None) -> str | None:
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


def _revision(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_REVISION:
        raise ValueError
    return value


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


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'
