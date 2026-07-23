"""Private exact SQLite domain-schema persistence; uncomposed by runtime."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from functools import wraps

from aipcs_mcp.relational import RelationalSpecification, RelationalTransition
from aipcs_mcp.storage.contracts import (
    DomainSchemaState,
    ServiceStoreLocator,
)
from aipcs_mcp.storage.errors import (
    StorageBusy,
    StorageContractError,
    StorageMigrationError,
    StorageUnavailable,
)

from . import service_store_inspection
from .connection import (
    SQLiteConnectionPolicy,
    SQLiteConnectionPurpose,
    connect,
    require_supported_sqlite_runtime,
)
from .domain_schema_layout import (
    SQLiteDomainSchemaLayout,
    build_domain_schema_layout,
    build_domain_transition_layout,
)
from .location import AnchoredLocation, SQLiteLocationPolicy
from .result_codes import is_sqlite_busy
from .service_store_migrations import INDEX_XINFO as FOUNDATION_INDEX_XINFO
from .service_store_migrations import RESERVED_PREFIX
from .sql_tokens import sql_tokens_equal

_FOREIGN_KEY_CHECK_BUDGET = 100_000


def _bounded[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return method(*args, **kwargs)
        except StorageContractError:
            failure: Exception = StorageContractError()
        except StorageBusy:
            failure = StorageBusy()
        except StorageUnavailable:
            failure = StorageUnavailable()
        except StorageMigrationError:
            failure = StorageMigrationError()
        except Exception as error:
            failure = StorageBusy() if is_sqlite_busy(error) else StorageMigrationError()
        raise failure from None

    return wrapped


class SQLiteDomainSchemaStore:
    """Own exact domain DDL inside an existing ready service store."""

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
        return "SQLiteDomainSchemaStore(<redacted>)"

    @_bounded
    def inspect(
        self,
        locator: ServiceStoreLocator,
        specification: RelationalSpecification,
    ) -> DomainSchemaState:
        namespace = _namespace(locator)
        layout = build_domain_schema_layout(specification)
        return _inspect_existing(self._location, self._connection_policy, namespace, layout)

    @_bounded
    def materialise(
        self,
        locator: ServiceStoreLocator,
        specification: RelationalSpecification,
    ) -> DomainSchemaState:
        namespace = _namespace(locator)
        layout = build_domain_schema_layout(specification)
        if layout.schema_version != 1:
            raise StorageContractError()

        anchored: AnchoredLocation | None = None
        connection: sqlite3.Connection | None = None
        try:
            anchored = _acquire_existing(self._location, namespace)
            connection = connect(
                anchored,
                "rw",
                query_only=False,
                policy=self._connection_policy,
                purpose=SQLiteConnectionPurpose.READY_OPERATION,
            )
            _begin_immediate(connection, anchored)
            _require_ready_foundation(connection, namespace)
            anchored.verify_live_database_identity()
            state = _inspect_domain(connection, layout)
            if state.status != "unmaterialised":
                _rollback(connection, anchored)
                released_connection, released_location = connection, anchored
                connection = None
                anchored = None
                _release(released_connection, released_location)
                return state

            for statement in layout.ddl:
                connection.execute(statement)
            _require_ready_foundation(connection, namespace)
            if _inspect_domain(connection, layout).status != "ready":
                raise StorageMigrationError()
            anchored.verify_live_database_identity()
            _commit(connection, anchored)
            released_connection, released_location = connection, anchored
            connection = None
            anchored = None
            _release(released_connection, released_location)

            state = _inspect_existing(
                self._location,
                self._connection_policy,
                namespace,
                layout,
            )
            if state.status != "ready":
                raise StorageMigrationError()
            return state
        finally:
            _release(connection, anchored)

    @_bounded
    def evolve(
        self,
        locator: ServiceStoreLocator,
        transition: RelationalTransition,
    ) -> DomainSchemaState:
        namespace = _namespace(locator)
        layout = build_domain_transition_layout(transition)

        anchored: AnchoredLocation | None = None
        connection: sqlite3.Connection | None = None
        try:
            anchored = _acquire_existing(self._location, namespace)
            connection = connect(
                anchored,
                "rw",
                query_only=False,
                policy=self._connection_policy,
                purpose=SQLiteConnectionPurpose.READY_OPERATION,
            )
            _begin_immediate(connection, anchored)
            _require_ready_foundation(connection, namespace)
            anchored.verify_live_database_identity()

            if _inspect_domain(connection, layout.target).status == "ready":
                _rollback(connection, anchored)
                released_connection, released_location = connection, anchored
                connection = None
                anchored = None
                _release(released_connection, released_location)
                return DomainSchemaState("ready")

            if _inspect_domain(connection, layout.current).status != "ready":
                _rollback(connection, anchored)
                released_connection, released_location = connection, anchored
                connection = None
                anchored = None
                _release(released_connection, released_location)
                return DomainSchemaState("incompatible")

            anchored.verify_live_database_identity()
            for statement in layout.ddl:
                connection.execute(statement)
            _require_ready_foundation(connection, namespace)
            if _inspect_domain(connection, layout.target).status != "ready":
                raise StorageMigrationError()
            anchored.verify_live_database_identity()
            _commit(connection, anchored)
            released_connection, released_location = connection, anchored
            connection = None
            anchored = None
            _release(released_connection, released_location)

            state = _inspect_existing(
                self._location,
                self._connection_policy,
                namespace,
                layout.target,
            )
            if state.status != "ready":
                raise StorageMigrationError()
            return state
        finally:
            _release(connection, anchored)


def _namespace(locator: ServiceStoreLocator) -> str:
    if type(locator) is not ServiceStoreLocator or locator.backend != "sqlite":
        raise StorageContractError()
    try:
        checked = ServiceStoreLocator("sqlite", locator.namespace)
    except Exception:
        raise StorageContractError() from None
    return checked.namespace


def _acquire_existing(location: SQLiteLocationPolicy, namespace: str) -> AnchoredLocation:
    anchored = location.acquire_service_store(namespace, create=False, allow_journal=False)
    if anchored is None or anchored._database_stat is None or anchored._database_stat.st_size == 0:
        if anchored is not None:
            with suppress(Exception):
                anchored.close()
        raise StorageMigrationError()
    return anchored


def _require_ready_foundation(connection: sqlite3.Connection, namespace: str) -> None:
    if (
        service_store_inspection.inspect_connection(connection, namespace, integrity=True).status
        != "ready"
    ):
        raise StorageMigrationError()


def _inspect_existing(
    location: SQLiteLocationPolicy,
    connection_policy: SQLiteConnectionPolicy,
    namespace: str,
    layout: SQLiteDomainSchemaLayout,
) -> DomainSchemaState:
    anchored: AnchoredLocation | None = None
    connection: sqlite3.Connection | None = None
    try:
        anchored = _acquire_existing(location, namespace)
        connection = connect(
            anchored,
            "ro",
            query_only=True,
            policy=connection_policy,
            purpose=SQLiteConnectionPurpose.READY_INSPECTION,
        )
        connection.execute("BEGIN")
        _require_ready_foundation(connection, namespace)
        state = _inspect_domain(connection, layout)
        _rollback(connection, anchored)
        released_connection, released_location = connection, anchored
        connection = None
        anchored = None
        _release(released_connection, released_location)
        return state
    finally:
        _close(connection, anchored)


def _inspect_domain(
    connection: sqlite3.Connection,
    layout: SQLiteDomainSchemaLayout,
) -> DomainSchemaState:
    try:
        rows = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema").fetchall()
        internal = {row["name"]: row for row in rows if row["name"].lower().startswith("sqlite_")}
        objects = {
            row["name"]: row
            for row in rows
            if not row["name"].lower().startswith(("sqlite_", RESERVED_PREFIX))
        }
        if not objects:
            return DomainSchemaState(
                "unmaterialised" if set(internal) == set(FOUNDATION_INDEX_XINFO) else "incompatible"
            )

        expected_internal = set(FOUNDATION_INDEX_XINFO) | {
            table.implicit_primary_key_index.name for table in layout.tables
        }
        if set(internal) != expected_internal:
            return DomainSchemaState("incompatible")
        for table in layout.tables:
            row = internal[table.implicit_primary_key_index.name]
            if row["type"] != "index" or row["tbl_name"] != table.name or row["sql"] is not None:
                return DomainSchemaState("incompatible")

        expected_names = {table.name for table in layout.tables} | {
            index.name for index in layout.indices
        }
        if set(objects) != expected_names:
            return DomainSchemaState("incompatible")
        for table in layout.tables:
            row = objects[table.name]
            if (
                row["type"] != "table"
                or row["tbl_name"] != table.name
                or not sql_tokens_equal(row["sql"], table.sql)
                or tuple(map(tuple, connection.execute(f'PRAGMA table_xinfo("{table.name}")')))
                != table.xinfo
                or tuple(map(tuple, connection.execute(f'PRAGMA foreign_key_list("{table.name}")')))
                != table.foreign_keys
            ):
                return DomainSchemaState("incompatible")

            actual_indices = tuple(
                sorted(
                    (row[1], row[2], row[3], row[4])
                    for row in connection.execute(f'PRAGMA index_list("{table.name}")')
                )
            )
            expected_indices = tuple(
                sorted(
                    (index.name, index.unique, index.origin, index.partial)
                    for index in layout.all_indices
                    if index.table == table.name
                )
            )
            if actual_indices != expected_indices:
                return DomainSchemaState("incompatible")

        for index in layout.indices:
            row = objects[index.name]
            if row["type"] != "index" or row["tbl_name"] != index.table or row["sql"] != index.sql:
                return DomainSchemaState("incompatible")
        for index in layout.all_indices:
            if (
                tuple(map(tuple, connection.execute(f'PRAGMA index_xinfo("{index.name}")')))
                != index.xinfo
            ):
                return DomainSchemaState("incompatible")
        if not _foreign_keys_clean(connection):
            return DomainSchemaState("incompatible")
        return DomainSchemaState("ready")
    except StorageMigrationError:
        raise
    except Exception as error:
        if is_sqlite_busy(error):
            raise StorageBusy() from None
        return DomainSchemaState("incompatible")


def _foreign_keys_clean(connection: sqlite3.Connection) -> bool:
    budget = _FOREIGN_KEY_CHECK_BUDGET
    interrupted = False

    def progress() -> int:
        nonlocal budget, interrupted
        budget -= 1_000
        interrupted = budget <= 0
        return int(interrupted)

    try:
        connection.set_progress_handler(progress, 1_000)
        return connection.execute("PRAGMA foreign_key_check").fetchone() is None
    except Exception as error:
        if is_sqlite_busy(error):
            raise StorageBusy() from None
        if interrupted:
            raise StorageMigrationError() from None
        return False
    finally:
        with suppress(Exception):
            connection.set_progress_handler(None, 0)


def _close(
    connection: sqlite3.Connection | None,
    location: AnchoredLocation | None,
) -> None:
    _release(connection, location)


def _begin_immediate(
    connection: sqlite3.Connection,
    location: AnchoredLocation,
) -> None:
    location.verify_live_database_identity()
    location.verify_live_sidecars(allow_journal=False)
    try:
        connection.execute("BEGIN IMMEDIATE")
    except Exception:
        location.adopt_live_sidecars(allow_journal=False)
        raise
    location.adopt_live_sidecars(allow_journal=False)
    location.verify_live_sidecars(allow_journal=False)
    location.verify_live_database_identity()


def _commit(connection: sqlite3.Connection, location: AnchoredLocation) -> None:
    location.verify_live_database_identity()
    location.verify_live_sidecars(allow_journal=False)
    connection.commit()
    location.verify_live_sidecars(allow_journal=False)
    location.verify_live_database_identity()


def _rollback(connection: sqlite3.Connection, location: AnchoredLocation) -> None:
    location.verify_live_database_identity()
    location.verify_live_sidecars(allow_journal=False)
    connection.rollback()
    location.verify_live_sidecars(allow_journal=False)
    location.verify_live_database_identity()


def _release(
    connection: sqlite3.Connection | None,
    location: AnchoredLocation | None,
) -> None:
    failure: Exception | None = None
    if connection is not None:
        try:
            if connection.in_transaction and location is not None:
                _rollback(connection, location)
            elif location is not None:
                location.verify_live_database_identity()
                location.verify_live_sidecars(allow_journal=False)
        except StorageUnavailable:
            failure = StorageUnavailable()
        except Exception as error:
            failure = StorageBusy() if is_sqlite_busy(error) else StorageMigrationError()
        try:
            connection.close()
        except Exception as error:
            if failure is None:
                failure = StorageBusy() if is_sqlite_busy(error) else StorageMigrationError()
        if location is not None:
            try:
                location.verify_database_identity()
                location.verify_closed_sidecars(allow_journal=False)
            except Exception:
                failure = StorageUnavailable()
    if location is not None:
        try:
            location.close()
        except Exception:
            if failure is None:
                failure = StorageUnavailable()
    if failure is not None:
        raise failure
