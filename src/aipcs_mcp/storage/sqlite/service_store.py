"""Private SQLite service-store catalogue, composed only through lifecycle coordination."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from functools import wraps
from uuid import UUID

from aipcs_mcp.storage.contracts import MigrationState, ServiceStoreLocator, StorageAdapterInfo
from aipcs_mcp.storage.errors import (
    StorageBusy,
    StorageContractError,
    StorageMigrationError,
    StorageUnavailable,
)

from . import service_store_inspection
from .codecs import encode_time
from .connection import (
    SQLiteConnectionPolicy,
    SQLiteConnectionPurpose,
    connect,
    require_supported_sqlite_runtime,
)
from .location import AnchoredLocation, SQLiteLocationPolicy, create_database
from .result_codes import is_sqlite_busy
from .service_store_migrations import META, MIGRATION, R1, R2, R2_POLICY, TARGET_REVISION
from .wal_policy import WAL_JOURNAL_MODE_OPERATION, WAL_POLICY_ID, WALPolicyState


def _bounded[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return method(*args, **kwargs)
        except StorageContractError:
            raise
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


class SQLiteServiceStoreCatalog:
    """Own opaque per-service database migration state, not domain materialisation."""

    def __init__(
        self,
        location: SQLiteLocationPolicy,
        *,
        busy_timeout_ms: int = 5_000,
        connection_policy: SQLiteConnectionPolicy | None = None,
    ) -> None:
        require_supported_sqlite_runtime()
        if not isinstance(location, SQLiteLocationPolicy):
            raise StorageUnavailable()
        if connection_policy is None:
            connection_policy = SQLiteConnectionPolicy(busy_timeout_ms)
        elif type(connection_policy) is not SQLiteConnectionPolicy or busy_timeout_ms != 5_000:
            raise StorageUnavailable()
        self._location = location
        self._connection_policy = connection_policy

    def __repr__(self) -> str:
        return "SQLiteServiceStoreCatalog(<redacted>)"

    def info(self) -> StorageAdapterInfo:
        return StorageAdapterInfo("sqlite", frozenset({"service_store"}))

    def allocate(self, service_id: UUID) -> ServiceStoreLocator:
        return ServiceStoreLocator.for_service("sqlite", service_id)

    @_bounded
    def inspect_migration(self, locator: ServiceStoreLocator) -> MigrationState:
        namespace = _namespace(locator)
        anchored: AnchoredLocation | None = None
        connection: sqlite3.Connection | None = None
        try:
            anchored = self._location.acquire_service_store(
                namespace, create=False, allow_journal=False
            )
            if anchored is None or anchored._database_stat is None or anchored._database_stat.st_size == 0:
                return _state(0, "uninitialised")
            connection = connect(
                anchored,
                "ro",
                query_only=True,
                policy=self._connection_policy,
                purpose=SQLiteConnectionPurpose.POLICY_INSPECTION,
            )
            connection.execute("BEGIN")
            state = service_store_inspection.inspect_connection(connection, namespace, integrity=True)
            connection.rollback()
            return state
        finally:
            _close(connection, anchored, allow_journal=False)

    @_bounded
    def migrate(self, locator: ServiceStoreLocator) -> MigrationState:
        namespace = _namespace(locator)
        anchored: AnchoredLocation | None = None
        connection: sqlite3.Connection | None = None
        try:
            anchored = self._acquire_for_migration(namespace)
            connection = self._migration_connection(anchored)
            state = self._prepare_or_classify(connection, anchored, namespace)

            if state is WALPolicyState.PREPARED_DELETE:
                _switch_to_wal(connection, anchored)
                state = WALPolicyState.PREPARED_WAL

            if state is WALPolicyState.PREPARED_WAL:
                self._finalise_wal(connection, anchored, namespace)
            elif state is not WALPolicyState.READY_WAL:
                # The inspection result is the bounded compatibility result for
                # states which must never be repaired by a migration attempt.
                return service_store_inspection.inspect_connection(connection, namespace, integrity=True)

            _checkpoint_passive(connection)
            anchored.verify_live_database_identity()
            anchored.verify_live_sidecars(allow_journal=False)
            connection.close()
            connection = None
            anchored.verify_database_identity()
            anchored.verify_closed_sidecars(allow_journal=False)
            anchored.close()
            anchored = None
            # This fresh acquisition is intentionally independent of the
            # checkpoint connection and is the one final ready observation.
            return self.inspect_migration(locator)
        finally:
            _close(connection, anchored, allow_journal=True)

    def _acquire_for_migration(self, namespace: str) -> AnchoredLocation:
        anchored = self._location.acquire_service_store(namespace, create=True, allow_journal=True)
        if anchored is None:
            raise StorageUnavailable()
        if anchored.journal_present:
            if anchored._database_stat is None:
                anchored.close()
                raise StorageUnavailable()
            connection: sqlite3.Connection | None = None
            try:
                connection = self._migration_connection(anchored)
                connection.execute("SELECT 1").fetchone()
            finally:
                _close(connection, anchored, allow_journal=True)
            anchored = self._location.acquire_service_store(namespace, create=True, allow_journal=False)
            if anchored is None:
                raise StorageUnavailable()
        if anchored._database_stat is None:
            anchored._database_stat = create_database(anchored._root_fd, anchored._database_name)
        return anchored

    def _migration_connection(self, anchored: AnchoredLocation) -> sqlite3.Connection:
        return connect(
            anchored,
            "rw",
            query_only=False,
            policy=self._connection_policy,
            purpose=SQLiteConnectionPurpose.MIGRATION,
        )

    def _prepare_or_classify(
        self,
        connection: sqlite3.Connection,
        anchored: AnchoredLocation,
        namespace: str,
    ) -> WALPolicyState:
        connection.execute("BEGIN IMMEDIATE")
        observed = service_store_inspection.inspect_connection(connection, namespace, integrity=False)
        if observed.status == "uninitialised":
            _create_predecessor_and_prepare(connection, namespace)
            anchored.verify_live_database_identity()
            anchored.validate_sidecars(allow_journal=True)
            connection.commit()
            anchored.verify_live_database_identity()
            anchored.verify_live_sidecars(allow_journal=True)
            return WALPolicyState.PREPARED_DELETE

        state, _ = service_store_inspection.classify_connection(connection, namespace)
        if state is WALPolicyState.CLEAN_PREDECESSOR:
            _prepare_predecessor(connection)
            anchored.verify_live_database_identity()
            anchored.validate_sidecars(allow_journal=True)
            connection.commit()
            anchored.verify_live_database_identity()
            anchored.verify_live_sidecars(allow_journal=True)
            return WALPolicyState.PREPARED_DELETE
        if state is WALPolicyState.PREPARED_DELETE:
            connection.rollback()
            anchored.verify_live_database_identity()
            anchored.verify_live_sidecars(allow_journal=True)
            return state
        if state is WALPolicyState.PREPARED_WAL:
            # We already own the WAL writer slot.  The classifier did not
            # start/finish a transaction, so finalisation may continue here.
            return state
        if state is WALPolicyState.READY_WAL:
            connection.rollback()
            anchored.verify_live_database_identity()
            anchored.verify_live_sidecars(allow_journal=False)
            return state
        connection.rollback()
        anchored.verify_live_database_identity()
        return state

    def _finalise_wal(
        self,
        connection: sqlite3.Connection,
        anchored: AnchoredLocation,
        namespace: str,
    ) -> None:
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        # A WAL writer's first begin may create SQLite-owned WAL/SHM siblings.
        anchored.adopt_live_sidecars(allow_journal=False)
        anchored.verify_live_sidecars(allow_journal=False)
        state, _ = service_store_inspection.classify_connection(connection, namespace)
        if state is WALPolicyState.READY_WAL:
            connection.rollback()
            return
        if state is not WALPolicyState.PREPARED_WAL:
            connection.rollback()
            raise StorageMigrationError()
        connection.execute(
            f'UPDATE "{R2_POLICY.table_name}" SET "phase"=\'ready\' WHERE "singleton"=1'
        )
        connection.execute(
            f'INSERT INTO "{MIGRATION}" VALUES (?,?,?,?,?)',
            ("service_store", R2.revision, R2.migration_id, R2.checksum, encode_time(datetime.now(UTC))),
        )
        connection.execute(
            f'UPDATE "{META}" SET "applied_revision"=?, "dirty"=0 WHERE "singleton"=1',
            (R2.revision,),
        )
        if service_store_inspection.inspect_connection(connection, namespace, integrity=True).status != "ready":
            raise StorageMigrationError()
        anchored.verify_live_database_identity()
        anchored.verify_live_sidecars(allow_journal=False)
        connection.commit()
        anchored.verify_live_database_identity()
        anchored.verify_live_sidecars(allow_journal=False)


def _create_predecessor_and_prepare(connection: sqlite3.Connection, namespace: str) -> None:
    for statement in R1.ddl:
        connection.execute(statement)
    connection.execute(
        f'INSERT INTO "{META}" VALUES (1,?,?,?,?,?)',
        ("aipcs.sqlite.service_store", "service_store", namespace, R1.revision, 1),
    )
    connection.execute(
        f'INSERT INTO "{MIGRATION}" VALUES (?,?,?,?,?)',
        ("service_store", R1.revision, R1.migration_id, R1.checksum, encode_time(datetime.now(UTC))),
    )
    _prepare_predecessor(connection)


def _prepare_predecessor(connection: sqlite3.Connection) -> None:
    connection.execute(R2_POLICY.ddl)
    connection.execute(
        f'INSERT INTO "{R2_POLICY.table_name}" VALUES (?,?,?,?)',
        (1, WAL_POLICY_ID, R2_POLICY.checksum, "prepared"),
    )
    connection.execute(f'UPDATE "{META}" SET "dirty"=1 WHERE "singleton"=1')


def _switch_to_wal(connection: sqlite3.Connection, anchored: AnchoredLocation) -> None:
    if connection.in_transaction:
        raise StorageMigrationError()
    row = connection.execute(WAL_JOURNAL_MODE_OPERATION).fetchone()
    if row is None or row[0] != "wal":
        raise StorageMigrationError()
    anchored.verify_live_database_identity()


def _checkpoint_passive(connection: sqlite3.Connection) -> None:
    row = connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    if row is None or len(row) != 3 or any(type(value) is not int for value in row):
        raise StorageMigrationError()
    busy, log_frames, checkpointed_frames = row
    if busy == 1:
        raise StorageBusy()
    if busy != 0 or (log_frames, checkpointed_frames) != (-1, -1) and (
        log_frames < 0 or checkpointed_frames < 0 or checkpointed_frames > log_frames
    ):
        raise StorageMigrationError()


def _namespace(locator: ServiceStoreLocator) -> str:
    if type(locator) is not ServiceStoreLocator or locator.backend != "sqlite":
        raise StorageContractError()
    try:
        checked = ServiceStoreLocator("sqlite", locator.namespace)
    except Exception:
        raise StorageContractError() from None
    return checked.namespace


def _state(revision: int, status: str) -> MigrationState:
    return MigrationState("service_store", revision, TARGET_REVISION, status)  # type: ignore[arg-type]


def _close(
    connection: sqlite3.Connection | None,
    location: AnchoredLocation | None,
    *,
    allow_journal: bool,
) -> None:
    if connection is not None:
        if connection.in_transaction:
            with suppress(Exception):
                connection.rollback()
        if location is not None:
            location.verify_live_sidecars(allow_journal=allow_journal)
        connection.close()
        if location is not None:
            location.verify_closed_sidecars(allow_journal=allow_journal)
    if location is not None:
        location.close()
