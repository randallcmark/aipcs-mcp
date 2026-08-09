"""PostgreSQL logical snapshot, staging, and write-admission adapter."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from typing import Protocol

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

from .connection import PostgreSQLConnectionPolicy, PostgreSQLDsn, connect_postgresql
from .data_core import canonical_json, qualified
from .data_store import (
    _close,
    _commit,
    _execute,
    _history_event,
    _insert,
    _record,
    _require_domain,
    _require_service_foundation,
    _rollback,
    _rows,
    _uuid,
)
from .domain_schema import PostgreSQLDomainSchemaStore
from .service_store import (
    PostgreSQLServiceStoreCatalog,
    _acquire_advisory_lock,
)
from .service_store_migrations import (
    BRANCH,
    HISTORY,
    MUTATION,
    RECORD_BRANCH,
    quote_identifier,
)
from .topology import _decode_branch


class ConnectionFactory(Protocol):
    def __call__(self, dsn: PostgreSQLDsn, policy: PostgreSQLConnectionPolicy) -> object: ...


class PostgreSQLPortableServiceStore:
    """Transfer logical state with schema-qualified, role-local SQL only."""

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
    ) -> None:
        if (
            type(dsn) is not PostgreSQLDsn
            or not callable(connection_factory)
            or any(
                type(value) is not int
                for value in (
                    connect_timeout_seconds,
                    lock_timeout_ms,
                    statement_timeout_ms,
                )
            )
            or (clock is not None and not callable(clock))
        ):
            raise StorageContractError()
        self._dsn = dsn
        self._factory = connection_factory
        self._timeouts = (
            connect_timeout_seconds,
            lock_timeout_ms,
            statement_timeout_ms,
        )
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
        self._clock = clock or (lambda: datetime.now(UTC))

    def __repr__(self) -> str:
        return "PostgreSQLPortableServiceStore(<redacted>)"

    def open_snapshot(
        self, service: Service, expected_fence: WriteAdmissionFence
    ) -> _PostgreSQLSnapshotReader:
        locator, specification = self._source(service, require_quiesced=True)
        if type(expected_fence) is not WriteAdmissionFence or expected_fence.state != "closed":
            raise StorageContractError()
        if self._catalog.inspect_migration(locator).status != "ready":
            raise StorageMigrationError()
        connection: object | None = None
        try:
            connection = self._connect(locator.namespace)
            _execute(
                connection,
                "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
            )
            _require_service_foundation(connection, locator.namespace)
            _require_domain(connection, locator.namespace, specification)
            actual = _read_fence(connection, locator.namespace, service.principal_id)
            if actual != expected_fence:
                raise StorageMigrationError()
            return _PostgreSQLSnapshotReader(
                connection,
                locator.namespace,
                service.principal_id,
                specification,
                actual,
            )
        except Exception:
            _rollback(connection)
            _close(connection)
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
        if self._catalog.migrate(locator).status != "ready":
            raise StorageMigrationError()
        if service.manifest is None:
            raise StorageContractError()
        if (
            self._domain.materialise_snapshot(
                locator, compile_manifest(service.manifest)
            ).status
            != "ready"
        ):
            raise StorageMigrationError()

        connection: object | None = None
        try:
            connection = self._connect(locator.namespace)
            _execute(connection, "BEGIN")
            _acquire_advisory_lock(connection, locator.namespace)
            _require_service_foundation(connection, locator.namespace)
            _require_domain(connection, locator.namespace, specification)
            if _has_logical_state(connection, locator.namespace, specification):
                _rollback(connection)
                _close(connection)
                connection = None
                status = (
                    "already_staged"
                    if self.observe(service, initial_fence, checked)
                    else "different"
                )
                return StageResult(status, self.read_fence(service))
            _insert_members(
                connection, locator.namespace, service, specification, checked
            )
            _write_fence(
                connection,
                locator.namespace,
                service.principal_id,
                initial_fence,
                self._clock(),
            )
            _commit(connection)
        except Exception:
            _rollback(connection)
            raise
        finally:
            _close(connection)
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
        connection: object | None = None
        try:
            connection = self._connect(locator.namespace)
            _execute(connection, "BEGIN READ ONLY")
            _require_service_foundation(connection, locator.namespace)
            result = _read_fence(connection, locator.namespace, service.principal_id)
            _rollback(connection)
            return result
        finally:
            _close(connection)

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
        connection: object | None = None
        try:
            connection = self._connect(locator.namespace)
            _execute(connection, "BEGIN")
            _acquire_advisory_lock(connection, locator.namespace)
            _require_service_foundation(connection, locator.namespace)
            current = _read_fence(
                connection, locator.namespace, service.principal_id, for_update=True
            )
            if current != expected:
                _rollback(connection)
                return FenceTransitionResult("stale", current)
            if current.state == target_state:
                _rollback(connection)
                return FenceTransitionResult("current", current)
            if current.generation == MAX_FENCE_GENERATION:
                raise StorageMigrationError()
            changed = WriteAdmissionFence(current.generation + 1, target_state)  # type: ignore[arg-type]
            _write_fence(
                connection,
                locator.namespace,
                service.principal_id,
                changed,
                self._clock(),
            )
            _commit(connection)
            return FenceTransitionResult("applied", changed)
        except Exception:
            _rollback(connection)
            raise
        finally:
            _close(connection)

    def purge(self, service: Service) -> PurgeStoreResult:
        locator, _ = self._source(service, require_quiesced=True)
        if service.operational_status != "archived":
            raise StorageContractError()
        observed = self._catalog.inspect_migration(locator)
        if observed.status == "uninitialised":
            return PurgeStoreResult("already_absent")
        if observed.status != "ready":
            raise StorageMigrationError()
        connection: object | None = None
        try:
            connection = self._connect(locator.namespace)
            _execute(connection, "BEGIN")
            _acquire_advisory_lock(connection, locator.namespace)
            _require_service_foundation(connection, locator.namespace)
            if (
                _read_fence(connection, locator.namespace, service.principal_id).state
                != "closed"
            ):
                raise StorageMigrationError()
            relations = _rows(
                _execute(
                    connection,
                    """SELECT c.relname AS relation_name
                         FROM pg_catalog.pg_class AS c
                         JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                         WHERE n.nspname = %s AND c.relkind IN ('r', 'p')
                         ORDER BY c.relname""",
                    (locator.namespace,),
                )
            )
            names = tuple(row.get("relation_name") for row in relations)
            if not names or any(type(name) is not str for name in names):
                raise StorageMigrationError()
            _execute(
                connection,
                "DROP TABLE "
                + ",".join(
                    f"{quote_identifier(locator.namespace)}.{quote_identifier(name)}"
                    for name in names
                )
                + " RESTRICT",
            )
            _execute(
                connection,
                f"DROP SCHEMA {quote_identifier(locator.namespace)} RESTRICT",
            )
            _commit(connection)
        except Exception:
            _rollback(connection)
            raise
        finally:
            _close(connection)
        if not self.is_absent(service):
            raise StorageMigrationError()
        return PurgeStoreResult("purged")

    def is_absent(self, service: Service) -> bool:
        locator, _ = self._source(service, require_quiesced=True)
        if service.operational_status != "archived":
            raise StorageContractError()
        return self._catalog.inspect_migration(locator).status == "uninitialised"

    def _connect(self, namespace: str) -> object:
        connect_timeout, lock_timeout, statement_timeout = self._timeouts
        return self._factory(
            self._dsn,
            PostgreSQLConnectionPolicy(
                schema=namespace,
                connect_timeout_seconds=connect_timeout,
                lock_timeout_ms=lock_timeout,
                statement_timeout_ms=statement_timeout,
            ),
        )

    @staticmethod
    def _source(
        service: Service, *, require_quiesced: bool
    ) -> tuple[ServiceStoreLocator, RecordSpecification]:
        if (
            type(service) is not Service
            or service.design_state != "materialised"
            or service.manifest is None
            or service.storage is None
            or service.storage.backend != "postgresql"
            or (
                require_quiesced
                and service.operational_status not in {"suspended", "archived"}
            )
        ):
            raise StorageContractError()
        return (
            ServiceStoreLocator("postgresql", service.storage.namespace),
            compile_record_specification(service.manifest),
        )


class _PostgreSQLSnapshotReader:
    def __init__(
        self,
        connection: object,
        namespace: str,
        principal: str,
        specification: RecordSpecification,
        fence: WriteAdmissionFence,
    ) -> None:
        self._connection = connection
        self._namespace = namespace
        self._principal = principal
        self._specification = specification
        self._used = False
        self.fence = fence

    def __enter__(self) -> _PostgreSQLSnapshotReader:
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
        if connection is None:
            raise StorageContractError()
        for entity in specification.entities:  # type: ignore[attr-defined]
            rows = _rows(
                _execute(
                    connection,
                    f"SELECT * FROM {qualified(self._namespace, entity.name)} "
                    "WHERE owner_id=%s ORDER BY id",
                    (self._principal,),
                )
            )
            for row in rows:
                yield PortableStoreMember(
                    "record",
                    _record(row, self._principal, specification, entity.name),
                )
        rows = _rows(
            _execute(
                connection,
                f"SELECT * FROM {qualified(self._namespace, HISTORY)} "
                "WHERE principal_id=%s ORDER BY entity_name,record_id,record_version",
                (self._principal,),
            )
        )
        for row in rows:
            yield PortableStoreMember("history", _history_event(row, specification))
        result = _execute(
            connection,
            f"SELECT id,slug,title,intent,branch_type,parent_branch_id,status,"
            f"retrieval_summary,created_at,updated_at,branch_revision "
            f"FROM {qualified(self._namespace, BRANCH)} "
            "WHERE principal_id=%s ORDER BY id",
            (self._principal,),
        )
        fetchall = result.fetchall
        for row in fetchall():
            yield PortableStoreMember("branch", _decode_branch(tuple(row)))
        rows = _rows(
            _execute(
                connection,
                f"SELECT branch_id,entity_name,record_id,role "
                f"FROM {qualified(self._namespace, RECORD_BRANCH)} "
                "WHERE principal_id=%s "
                "ORDER BY branch_id,entity_name,record_id,role",
                (self._principal,),
            )
        )
        for row in rows:
            yield PortableStoreMember(
                "branch_membership",
                BranchMembership(
                    _uuid(row["branch_id"]),
                    row["entity_name"],  # type: ignore[arg-type]
                    _uuid(row["record_id"]),
                    row["role"],  # type: ignore[arg-type]
                ),
            )

    def close(self) -> None:
        connection = self._connection
        self._connection = None
        _rollback(connection)
        _close(connection)


def _checked_members(
    members: Iterable[PortableStoreMember], specification: RecordSpecification
) -> tuple[PortableStoreMember, ...]:
    try:
        return validate_portable_members(members, specification)
    except Exception:
        raise StorageContractError() from None


def _insert_members(connection, namespace, service, specification, members) -> None:  # type: ignore[no-untyped-def]
    branches: list[BranchValue] = []
    memberships: list[BranchMembership] = []
    for member in members:
        value = member.value
        if type(value) is RecordValue:
            _insert(connection, namespace, service.principal_id, specification, value)
        elif type(value) is RecordHistoryEvent:
            _execute(
                connection,
                f"INSERT INTO {qualified(namespace, HISTORY)} "
                "(principal_id,entity_name,record_id,record_version,event_kind,"
                "before_json,after_json,occurred_at) "
                "VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)",
                (
                    service.principal_id,
                    value.entity_name,
                    value.record_id,
                    value.record_version,
                    value.kind,
                    None if value.before is None else canonical_json(value.before.to_dict()),
                    None if value.after is None else canonical_json(value.after.to_dict()),
                    value.occurred_at,
                ),
            )
        elif type(value) is BranchValue:
            branches.append(value)
        else:
            if type(value) is not BranchMembership:
                raise StorageContractError()
            memberships.append(value)
    for branch in _parent_first(branches):
        _execute(
            connection,
            f"INSERT INTO {qualified(namespace, BRANCH)} "
            "(id,principal_id,slug,title,intent,branch_type,parent_branch_id,status,"
            "retrieval_summary,branch_revision,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                branch.branch_id,
                service.principal_id,
                branch.slug,
                branch.title,
                branch.intent,
                branch.branch_type,
                branch.parent_branch_id,
                branch.status,
                branch.retrieval_summary,
                branch.branch_revision,
                branch.created_at,
                branch.updated_at,
            ),
        )
    for membership in memberships:
        _execute(
            connection,
            f"INSERT INTO {qualified(namespace, RECORD_BRANCH)} "
            "(principal_id,entity_name,record_id,branch_id,role,assigned_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (
                service.principal_id,
                membership.entity_name,
                membership.record_id,
                membership.branch_id,
                membership.role,
                service.updated_at,
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


def _has_logical_state(
    connection: object, namespace: str, specification: RecordSpecification
) -> bool:
    for table in (HISTORY, BRANCH, RECORD_BRANCH, MUTATION):
        if _rows(_execute(connection, f"SELECT 1 AS present FROM {qualified(namespace, table)} LIMIT 1")):
            return True
    return any(
        _rows(
            _execute(
                connection,
                f"SELECT 1 AS present FROM {qualified(namespace, entity.name)} LIMIT 1",
            )
        )
        for entity in specification.entities  # type: ignore[attr-defined]
    )


def _read_fence(
    connection: object,
    namespace: str,
    principal: str,
    *,
    for_update: bool = False,
) -> WriteAdmissionFence:
    suffix = " FOR UPDATE" if for_update else ""
    rows = _rows(
        _execute(
            connection,
            f"SELECT principal_id,idempotency_key,fingerprint,result_json "
            f"FROM {qualified(namespace, MUTATION)} WHERE operation_kind=%s{suffix}",
            (FENCE_OPERATION_KIND,),
        )
    )
    if not rows:
        return INITIAL_FENCE
    row = rows[0]
    if len(rows) != 1 or (
        row["principal_id"],
        row["idempotency_key"],
        row["fingerprint"],
    ) != (principal, FENCE_IDEMPOTENCY_KEY, FENCE_FINGERPRINT):
        raise StorageMigrationError()
    return decode_fence(row["result_json"])


def _write_fence(
    connection: object,
    namespace: str,
    principal: str,
    fence: WriteAdmissionFence,
    at: datetime,
) -> None:
    _execute(
        connection,
        f"INSERT INTO {qualified(namespace, MUTATION)} "
        "(principal_id,idempotency_key,operation_kind,fingerprint,result_json,completed_at) "
        "VALUES (%s,%s,%s,%s,%s::jsonb,%s) "
        "ON CONFLICT(principal_id,idempotency_key) DO UPDATE SET "
        "operation_kind=excluded.operation_kind,fingerprint=excluded.fingerprint,"
        "result_json=excluded.result_json,completed_at=excluded.completed_at",
        (
            principal,
            FENCE_IDEMPOTENCY_KEY,
            FENCE_OPERATION_KIND,
            FENCE_FINGERPRINT,
            encode_fence(fence),
            at,
        ),
    )
