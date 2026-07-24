"""SQLite logical snapshot, staging, and write-admission adapter."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import suppress
from datetime import UTC, datetime

from aipcs_mcp.application.models import Service
from aipcs_mcp.application.portable_store import (
    MAX_FENCE_GENERATION,
    BranchMembership,
    FenceTransitionResult,
    PortableStoreMember,
    PurgeStoreResult,
    StageResult,
    WriteAdmissionFence,
    validate_portable_members,
)
from aipcs_mcp.records import (
    BranchValue,
    RecordHistoryEvent,
    RecordSpecification,
    RecordValue,
    compile_record_specification,
)
from aipcs_mcp.relational import compile_manifest
from aipcs_mcp.storage.codecs import encode_time
from aipcs_mcp.storage.contracts import ServiceStoreLocator
from aipcs_mcp.storage.errors import StorageContractError, StorageMigrationError
from aipcs_mcp.storage.write_admission import (
    FENCE_FINGERPRINT,
    FENCE_IDEMPOTENCY_KEY,
    FENCE_OPERATION_KIND,
    INITIAL_FENCE,
    decode_fence,
    encode_fence,
)

from . import service_store_inspection
from .connection import SQLiteConnectionPolicy, SQLiteConnectionPurpose, connect
from .data_store import (
    _acquire_existing,
    _begin_immediate,
    _canonical_json,
    _commit,
    _decode_history,
    _decode_record,
    _insert_record,
    _quote,
)
from .domain_schema import SQLiteDomainSchemaStore
from .location import AnchoredLocation, SQLiteLocationPolicy
from .service_store import SQLiteServiceStoreCatalog
from .service_store_migrations import (
    R3_BRANCH_TABLE,
    R3_HISTORY_TABLE,
    R3_MUTATION_TABLE,
    R3_RECORD_BRANCH_TABLE,
)
from .topology import _decode_branch


class SQLitePortableServiceStore:
    """Transfer logical state without exposing or copying a SQLite file."""

    def __init__(
        self,
        location: SQLiteLocationPolicy,
        *,
        busy_timeout_ms: int = 5_000,
        connection_policy: SQLiteConnectionPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if connection_policy is None:
            connection_policy = SQLiteConnectionPolicy(busy_timeout_ms)
        elif type(connection_policy) is not SQLiteConnectionPolicy or busy_timeout_ms != 5_000:
            raise StorageContractError()
        if not isinstance(location, SQLiteLocationPolicy) or (
            clock is not None and not callable(clock)
        ):
            raise StorageContractError()
        self._location = location
        self._policy = connection_policy
        self._catalog = SQLiteServiceStoreCatalog(location, connection_policy=connection_policy)
        self._domain = SQLiteDomainSchemaStore(location, connection_policy=connection_policy)
        self._clock = clock or (lambda: datetime.now(UTC))

    def __repr__(self) -> str:
        return "SQLitePortableServiceStore(<redacted>)"

    def open_snapshot(
        self, service: Service, expected_fence: WriteAdmissionFence
    ) -> _SQLiteSnapshotReader:
        locator, specification = self._source(service, require_quiesced=True)
        if type(expected_fence) is not WriteAdmissionFence or expected_fence.state != "closed":
            raise StorageContractError()
        if self._catalog.inspect_migration(locator).status != "ready":
            raise StorageMigrationError()
        anchored = _acquire_existing(self._location, locator.namespace)
        connection: sqlite3.Connection | None = None
        try:
            connection = connect(
                anchored,
                "ro",
                query_only=True,
                policy=self._policy,
                purpose=SQLiteConnectionPurpose.READY_INSPECTION,
            )
            connection.execute("BEGIN")
            _require_ready(connection, locator.namespace)
            actual = _read_fence(connection, service.principal_id)
            if actual != expected_fence:
                raise StorageMigrationError()
            return _SQLiteSnapshotReader(
                connection, anchored, service.principal_id, specification, actual
            )
        except Exception:
            _release(connection, anchored)
            raise

    def stage(
        self,
        service: Service,
        members: Iterable[PortableStoreMember],
        initial_fence: WriteAdmissionFence,
    ) -> StageResult:
        locator, specification = self._source(service, require_quiesced=True)
        if type(initial_fence) is not WriteAdmissionFence or initial_fence.state != "closed":
            raise StorageContractError()
        checked = _checked_members(members, specification)
        state = self._catalog.migrate(locator)
        if state.status != "ready":
            raise StorageMigrationError()
        if service.manifest is None:
            raise StorageContractError()
        domain = self._domain.materialise(locator, compile_manifest(service.manifest))
        if domain.status != "ready":
            raise StorageMigrationError()

        anchored = _acquire_existing(self._location, locator.namespace)
        connection: sqlite3.Connection | None = None
        try:
            connection = connect(
                anchored,
                "rw",
                query_only=False,
                policy=self._policy,
                purpose=SQLiteConnectionPurpose.READY_OPERATION,
            )
            _begin_immediate(connection, anchored)
            _require_ready(connection, locator.namespace)
            if _has_logical_state(connection):
                connection.rollback()
                _release(connection, anchored)
                connection = None
                anchored = None
                status = (
                    "already_staged"
                    if self.observe(service, initial_fence, checked)
                    else "different"
                )
                return StageResult(status, self.read_fence(service))
            _insert_members(connection, service, specification, checked)
            _write_fence(connection, service.principal_id, initial_fence, self._clock())
            _commit(connection, anchored)
        finally:
            _release(connection, anchored)
        return StageResult("staged", initial_fence)

    def observe(
        self,
        service: Service,
        expected_fence: WriteAdmissionFence,
        members: Iterable[PortableStoreMember],
    ) -> bool:
        _locator, specification = self._source(service, require_quiesced=True)
        checked = _checked_members(members, specification)
        try:
            with self.open_snapshot(service, expected_fence) as reader:
                return tuple(reader) == checked
        except Exception:
            return False

    def read_fence(self, service: Service) -> WriteAdmissionFence:
        locator, _ = self._source(service, require_quiesced=False)
        anchored = _acquire_existing(self._location, locator.namespace)
        connection: sqlite3.Connection | None = None
        try:
            connection = connect(
                anchored,
                "ro",
                query_only=True,
                policy=self._policy,
                purpose=SQLiteConnectionPurpose.READY_INSPECTION,
            )
            connection.execute("BEGIN")
            _require_ready(connection, locator.namespace)
            result = _read_fence(connection, service.principal_id)
            connection.rollback()
            return result
        finally:
            _release(connection, anchored)

    def transition_fence(
        self,
        service: Service,
        expected: WriteAdmissionFence,
        target_state: str,
    ) -> FenceTransitionResult:
        locator, _ = self._source(service, require_quiesced=False)
        if (
            type(expected) is not WriteAdmissionFence
            or type(target_state) is not str
            or target_state not in {"open", "closed"}
        ):
            raise StorageContractError()
        anchored = _acquire_existing(self._location, locator.namespace)
        connection: sqlite3.Connection | None = None
        try:
            connection = connect(
                anchored,
                "rw",
                query_only=False,
                policy=self._policy,
                purpose=SQLiteConnectionPurpose.READY_OPERATION,
            )
            _begin_immediate(connection, anchored)
            _require_ready(connection, locator.namespace)
            current = _read_fence(connection, service.principal_id)
            if current != expected:
                connection.rollback()
                return FenceTransitionResult("stale", current)
            if current.state == target_state:
                connection.rollback()
                return FenceTransitionResult("current", current)
            if current.generation == MAX_FENCE_GENERATION:
                raise StorageMigrationError()
            changed = WriteAdmissionFence(current.generation + 1, target_state)  # type: ignore[arg-type]
            _write_fence(connection, service.principal_id, changed, self._clock())
            _commit(connection, anchored)
            return FenceTransitionResult("applied", changed)
        finally:
            _release(connection, anchored)

    def purge(self, service: Service) -> PurgeStoreResult:
        locator, _ = self._source(service, require_quiesced=True)
        if service.operational_status != "archived":
            raise StorageContractError()
        if self._location.service_store_absent(locator.namespace):
            return PurgeStoreResult("already_absent")
        if self.read_fence(service).state != "closed":
            raise StorageMigrationError()
        removed = self._location.remove_service_store(locator.namespace)
        return PurgeStoreResult("purged" if removed else "already_absent")

    def is_absent(self, service: Service) -> bool:
        locator, _ = self._source(service, require_quiesced=True)
        if service.operational_status != "archived":
            raise StorageContractError()
        return self._location.service_store_absent(locator.namespace)

    @staticmethod
    def _source(
        service: Service, *, require_quiesced: bool
    ) -> tuple[ServiceStoreLocator, RecordSpecification]:
        if (
            type(service) is not Service
            or service.design_state != "materialised"
            or service.manifest is None
            or service.storage is None
            or service.storage.backend != "sqlite"
            or (
                require_quiesced
                and service.operational_status not in {"suspended", "archived"}
            )
        ):
            raise StorageContractError()
        return (
            ServiceStoreLocator("sqlite", service.storage.namespace),
            compile_record_specification(service.manifest),
        )


class _SQLiteSnapshotReader:
    def __init__(
        self,
        connection: sqlite3.Connection,
        anchored: AnchoredLocation,
        principal: str,
        specification: RecordSpecification,
        fence: WriteAdmissionFence,
    ) -> None:
        self._connection = connection
        self._anchored = anchored
        self._principal = principal
        self._specification = specification
        self._used = False
        self.fence = fence

    def __enter__(self) -> _SQLiteSnapshotReader:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def __iter__(self) -> Iterator[PortableStoreMember]:
        if self._used or self._connection is None:
            raise StorageContractError()
        self._used = True
        return self._members()

    def _members(self) -> Iterator[PortableStoreMember]:
        connection = self._connection
        specification = self._specification
        assert connection is not None
        for entity in specification.entities:  # type: ignore[attr-defined]
            rows = connection.execute(
                f"SELECT * FROM {_quote(entity.name)} WHERE owner_id=? ORDER BY id",
                (self._principal,),
            )
            for row in rows:
                yield PortableStoreMember(
                    "record",
                    _decode_record(row, self._principal, specification, entity.name),
                )
        rows = connection.execute(
            f'SELECT * FROM "{R3_HISTORY_TABLE}" WHERE principal_id=? '
            "ORDER BY entity_name,record_id,record_version",
            (self._principal,),
        )
        for row in rows:
            yield PortableStoreMember("history", _decode_history(row, specification))
        rows = connection.execute(
            f'SELECT * FROM "{R3_BRANCH_TABLE}" WHERE principal_id=? ORDER BY id',
            (self._principal,),
        )
        for row in rows:
            yield PortableStoreMember("branch", _decode_branch(row))
        rows = connection.execute(
            f'SELECT branch_id,entity_name,record_id,role FROM "{R3_RECORD_BRANCH_TABLE}" '
            "WHERE principal_id=? ORDER BY branch_id,entity_name,record_id,role",
            (self._principal,),
        )
        for row in rows:
            yield PortableStoreMember(
                "branch_membership",
                BranchMembership(
                    _uuid(row["branch_id"]),
                    row["entity_name"],
                    _uuid(row["record_id"]),
                    row["role"],
                ),
            )

    def close(self) -> None:
        connection, anchored = self._connection, self._anchored
        self._connection = None  # type: ignore[assignment]
        self._anchored = None  # type: ignore[assignment]
        _release(connection, anchored)


def _checked_members(
    members: Iterable[PortableStoreMember], specification: RecordSpecification
) -> tuple[PortableStoreMember, ...]:
    try:
        return validate_portable_members(members, specification)
    except Exception:
        raise StorageContractError() from None


def _insert_members(connection, service, specification, members) -> None:  # type: ignore[no-untyped-def]
    branches: list[BranchValue] = []
    memberships: list[BranchMembership] = []
    for member in members:
        value = member.value
        if type(value) is RecordValue:
            _insert_record(connection, service.principal_id, specification, value)
        elif type(value) is RecordHistoryEvent:
            connection.execute(
                f'INSERT INTO "{R3_HISTORY_TABLE}" '
                "(principal_id,entity_name,record_id,record_version,event_kind,"
                "before_json,after_json,occurred_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    service.principal_id,
                    value.entity_name,
                    str(value.record_id),
                    value.record_version,
                    value.kind,
                    None if value.before is None else _canonical_json(value.before.to_dict()),
                    None if value.after is None else _canonical_json(value.after.to_dict()),
                    encode_time(value.occurred_at),
                ),
            )
        elif type(value) is BranchValue:
            branches.append(value)
        else:
            assert type(value) is BranchMembership
            memberships.append(value)
    for branch in _parent_first(branches):
        connection.execute(
            f'INSERT INTO "{R3_BRANCH_TABLE}" '
            "(id,principal_id,slug,title,intent,branch_type,parent_branch_id,status,"
            "retrieval_summary,branch_revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(branch.branch_id),
                service.principal_id,
                branch.slug,
                branch.title,
                branch.intent,
                branch.branch_type,
                None if branch.parent_branch_id is None else str(branch.parent_branch_id),
                branch.status,
                branch.retrieval_summary,
                branch.branch_revision,
                encode_time(branch.created_at),
                encode_time(branch.updated_at),
            ),
        )
    for membership in memberships:
        connection.execute(
            f'INSERT INTO "{R3_RECORD_BRANCH_TABLE}" '
            "(principal_id,entity_name,record_id,branch_id,role,assigned_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                service.principal_id,
                membership.entity_name,
                str(membership.record_id),
                str(membership.branch_id),
                membership.role,
                encode_time(service.updated_at),
            ),
        )


def _parent_first(branches: list[BranchValue]) -> tuple[BranchValue, ...]:
    pending = {branch.branch_id: branch for branch in branches}
    ordered: list[BranchValue] = []
    while pending:
        ready = sorted(
            (
                branch
                for branch in pending.values()
                if branch.parent_branch_id is None
                or branch.parent_branch_id not in pending
                and any(item.branch_id == branch.parent_branch_id for item in ordered)
            ),
            key=lambda value: value.branch_id.hex,
        )
        if not ready:
            raise StorageContractError()
        for branch in ready:
            if branch.parent_branch_id is not None and not any(
                item.branch_id == branch.parent_branch_id for item in ordered
            ):
                raise StorageContractError()
            ordered.append(branch)
            del pending[branch.branch_id]
    return tuple(ordered)


def _has_logical_state(connection: sqlite3.Connection) -> bool:
    tables = (R3_HISTORY_TABLE, R3_BRANCH_TABLE, R3_RECORD_BRANCH_TABLE, R3_MUTATION_TABLE)
    if any(connection.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() for table in tables):
        return True
    domain = connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '__aipcs_%'"
    )
    return any(
        connection.execute(f"SELECT 1 FROM {_quote(row[0])} LIMIT 1").fetchone()
        for row in domain
    )


def _read_fence(connection: sqlite3.Connection, principal: str) -> WriteAdmissionFence:
    rows = connection.execute(
        f'SELECT principal_id,idempotency_key,fingerprint,result_json '
        f'FROM "{R3_MUTATION_TABLE}" WHERE operation_kind=?',
        (FENCE_OPERATION_KIND,),
    ).fetchall()
    if not rows:
        return INITIAL_FENCE
    if len(rows) != 1 or tuple(rows[0][:3]) != (
        principal,
        FENCE_IDEMPOTENCY_KEY,
        FENCE_FINGERPRINT,
    ):
        raise StorageMigrationError()
    return decode_fence(rows[0][3])


def _write_fence(
    connection: sqlite3.Connection,
    principal: str,
    fence: WriteAdmissionFence,
    at: datetime,
) -> None:
    encoded = encode_fence(fence)
    connection.execute(
        f'INSERT INTO "{R3_MUTATION_TABLE}" '
        "(principal_id,idempotency_key,operation_kind,fingerprint,result_json,completed_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(principal_id,idempotency_key) DO UPDATE SET "
        "operation_kind=excluded.operation_kind,fingerprint=excluded.fingerprint,"
        "result_json=excluded.result_json,completed_at=excluded.completed_at",
        (
            principal,
            FENCE_IDEMPOTENCY_KEY,
            FENCE_OPERATION_KIND,
            FENCE_FINGERPRINT,
            encoded,
            encode_time(at),
        ),
    )


def _require_ready(connection: sqlite3.Connection, namespace: str) -> None:
    if service_store_inspection.inspect_connection(connection, namespace, integrity=True).status != "ready":
        raise StorageMigrationError()


def _uuid(value: object):
    from uuid import UUID

    parsed = UUID(value)
    if parsed.int == 0 or str(parsed) != value:
        raise StorageMigrationError()
    return parsed


def _release(
    connection: sqlite3.Connection | None, anchored: AnchoredLocation | None
) -> None:
    if connection is not None:
        with suppress(Exception):
            if connection.in_transaction:
                connection.rollback()
        with suppress(Exception):
            connection.close()
    if anchored is not None:
        with suppress(Exception):
            anchored.close()
