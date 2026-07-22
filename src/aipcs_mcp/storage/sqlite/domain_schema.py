"""Private exact SQLite domain-schema materialisation; uncomposed by runtime."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from functools import wraps

from aipcs_mcp.relational import (
    FieldAddition,
    MetadataChange,
    RelationalAdditions,
    RelationalEntity,
    RelationalIndex,
    RelationalRelationship,
    RelationalSpecification,
    RelationalTransition,
    _expected_additions,
)
from aipcs_mcp.storage.contracts import (
    DomainSchemaState,
    ServiceStoreLocator,
)
from aipcs_mcp.storage.errors import (
    StorageContractError,
    StorageMigrationError,
    StorageUnavailable,
)

from . import service_store_inspection
from .connection import connect
from .domain_schema_layout import (
    SQLiteDomainSchemaLayout,
    _detached_specification,
    build_domain_schema_layout,
)
from .location import AnchoredLocation, SQLiteLocationPolicy
from .service_store_migrations import INDEX_XINFO as FOUNDATION_INDEX_XINFO
from .service_store_migrations import RESERVED_PREFIX

_FOREIGN_KEY_CHECK_BUDGET = 100_000


def _bounded[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return method(*args, **kwargs)
        except StorageContractError:
            failure: Exception = StorageContractError()
        except StorageUnavailable:
            failure = StorageUnavailable()
        except StorageMigrationError:
            failure = StorageMigrationError()
        except Exception:
            failure = StorageMigrationError()
        raise failure from None

    return wrapped


class SQLiteDomainSchemaStore:
    """Own exact initial domain DDL inside an existing ready service store."""

    def __init__(self, location: SQLiteLocationPolicy) -> None:
        if not isinstance(location, SQLiteLocationPolicy):
            raise StorageUnavailable()
        self._location = location

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
        return _inspect_existing(self._location, namespace, layout)

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
        committed = False
        try:
            anchored = _acquire_existing(self._location, namespace)
            connection = connect(anchored, "rw", query_only=False)
            connection.execute("BEGIN EXCLUSIVE")
            _require_ready_foundation(connection, namespace)
            anchored.verify_database_identity()
            state = _inspect_domain(connection, layout)
            if state.status != "unmaterialised":
                connection.rollback()
                anchored.verify_database_identity()
                connection.close()
                connection = None
                anchored.verify_database_identity()
                anchored.close()
                anchored = None
                return state

            for statement in layout.ddl:
                connection.execute(statement)
            _require_ready_foundation(connection, namespace)
            if _inspect_domain(connection, layout).status != "ready":
                raise StorageMigrationError()
            anchored.verify_database_identity()
            connection.commit()
            committed = True
            anchored.verify_database_identity()
            connection.close()
            connection = None
            anchored.verify_database_identity()
            anchored.close()
            anchored = None

            state = _inspect_existing(self._location, namespace, layout)
            if state.status != "ready":
                raise StorageMigrationError()
            return state
        finally:
            if connection is not None:
                if not committed:
                    with suppress(Exception):
                        connection.rollback()
                with suppress(Exception):
                    connection.close()
            if anchored is not None:
                with suppress(Exception):
                    anchored.close()

    @_bounded
    def evolve(
        self,
        locator: ServiceStoreLocator,
        transition: RelationalTransition,
    ) -> DomainSchemaState:
        _namespace(locator)
        _validate_transition(transition)
        raise StorageMigrationError()


def _namespace(locator: ServiceStoreLocator) -> str:
    if type(locator) is not ServiceStoreLocator or locator.backend != "sqlite":
        raise StorageContractError()
    try:
        checked = ServiceStoreLocator("sqlite", locator.namespace)
    except Exception:
        raise StorageContractError() from None
    return checked.namespace


def _validate_transition(value: object) -> None:
    if type(value) is not RelationalTransition:
        raise StorageContractError()
    try:
        current = _detached_specification(value.current)
        target = _detached_specification(value.target)
        if target.schema_version != current.schema_version + 1:
            raise StorageContractError()

        additions = value.additions
        if (
            type(additions) is not RelationalAdditions
            or type(additions.entities) is not tuple
            or type(additions.fields) is not tuple
            or type(additions.relationships) is not tuple
            or type(additions.indices) is not tuple
            or any(type(item) is not RelationalEntity for item in additions.entities)
            or any(type(item) is not FieldAddition for item in additions.fields)
            or any(type(item) is not RelationalRelationship for item in additions.relationships)
            or any(type(item) is not RelationalIndex for item in additions.indices)
        ):
            raise StorageContractError()
        if additions != _expected_additions(current, target):
            raise StorageContractError()

        changes = value.metadata_changes
        if type(changes) is not tuple or any(
            type(change) is not MetadataChange for change in changes
        ):
            raise StorageContractError()
        checked_changes = tuple(
            MetadataChange(change.kind, change.entity, change.field) for change in changes
        )
        if (
            checked_changes != changes
            or checked_changes != tuple(sorted(checked_changes, key=_metadata_sort_key))
            or len(set(checked_changes)) != len(checked_changes)
            or not (
                additions.entities
                or additions.fields
                or additions.relationships
                or additions.indices
                or changes
            )
        ):
            raise StorageContractError()
    except StorageContractError:
        raise
    except Exception:
        raise StorageContractError() from None


def _metadata_sort_key(change: MetadataChange) -> tuple[str, str, str]:
    return (change.kind, change.entity or "", change.field or "")


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
    namespace: str,
    layout: SQLiteDomainSchemaLayout,
) -> DomainSchemaState:
    anchored: AnchoredLocation | None = None
    connection: sqlite3.Connection | None = None
    try:
        anchored = _acquire_existing(location, namespace)
        connection = connect(anchored, "ro", query_only=True)
        _require_ready_foundation(connection, namespace)
        state = _inspect_domain(connection, layout)
        anchored.verify_database_identity()
        connection.close()
        connection = None
        anchored.verify_database_identity()
        anchored.close()
        anchored = None
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
                or row["sql"] != table.sql
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
    except Exception:
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
    except Exception:
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
    if connection is not None:
        with suppress(Exception):
            connection.close()
    if location is not None:
        with suppress(Exception):
            location.close()
