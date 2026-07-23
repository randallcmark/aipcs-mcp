"""Private exact PostgreSQL domain-schema materialisation and evolution boundary."""

from __future__ import annotations

from contextlib import suppress
from typing import Protocol

from aipcs_mcp.relational import RelationalSpecification, RelationalTransition
from aipcs_mcp.storage.contracts import DomainSchemaState, ServiceStoreLocator
from aipcs_mcp.storage.errors import (
    StorageBusy,
    StorageContractError,
    StorageMigrationError,
    StorageUnavailable,
)

from .connection import PostgreSQLConnectionPolicy, PostgreSQLDsn, connect_postgresql
from .domain_schema_layout import (
    PostgreSQLDomainLayout,
    build_postgresql_domain_layout,
    build_postgresql_domain_transition_layout,
)
from .result_codes import is_postgresql_busy
from .service_store import PostgreSQLServiceStoreCatalog


class ConnectionFactory(Protocol):
    def __call__(self, dsn: PostgreSQLDsn, policy: PostgreSQLConnectionPolicy) -> object: ...


class PostgreSQLDomainSchemaStore:
    """Own exact domain DDL inside one already-ready PostgreSQL service schema."""

    def __init__(
        self,
        dsn: PostgreSQLDsn,
        *,
        connect_timeout_seconds: int = 10,
        lock_timeout_ms: int = 5_000,
        statement_timeout_ms: int = 30_000,
        connection_factory: ConnectionFactory = connect_postgresql,
        service_store_catalog: PostgreSQLServiceStoreCatalog | None = None,
    ) -> None:
        if (
            type(dsn) is not PostgreSQLDsn
            or not callable(connection_factory)
            or any(
                type(value) is not int
                for value in (connect_timeout_seconds, lock_timeout_ms, statement_timeout_ms)
            )
            or (
                service_store_catalog is not None
                and type(service_store_catalog) is not PostgreSQLServiceStoreCatalog
            )
        ):
            raise StorageUnavailable()
        self._dsn = dsn
        self._connection_factory = connection_factory
        self._connect_timeout_seconds = connect_timeout_seconds
        self._lock_timeout_ms = lock_timeout_ms
        self._statement_timeout_ms = statement_timeout_ms
        self._catalog = service_store_catalog or PostgreSQLServiceStoreCatalog(
            dsn,
            connect_timeout_seconds=connect_timeout_seconds,
            lock_timeout_ms=lock_timeout_ms,
            statement_timeout_ms=statement_timeout_ms,
            connection_factory=connection_factory,
        )

    def __repr__(self) -> str:
        return "PostgreSQLDomainSchemaStore(<redacted>)"

    def inspect(
        self, locator: ServiceStoreLocator, specification: RelationalSpecification
    ) -> DomainSchemaState:
        namespace = _namespace(locator)
        layout = build_postgresql_domain_layout(specification, namespace)
        connection: object | None = None
        try:
            self._require_foundation(locator)
            connection = self._connect(namespace)
            _begin(connection, read_only=True)
            state = _inspect(connection, namespace, layout)
            _rollback(connection)
            return state
        except StorageContractError:
            raise
        except StorageUnavailable:
            raise
        except Exception as error:
            raise _failure(error, commit_started=False) from None
        finally:
            _close(connection)

    def materialise(
        self, locator: ServiceStoreLocator, specification: RelationalSpecification
    ) -> DomainSchemaState:
        namespace = _namespace(locator)
        layout = build_postgresql_domain_layout(specification, namespace)
        connection: object | None = None
        commit_started = False
        try:
            self._require_foundation(locator)
            connection = self._connect(namespace)
            _begin(connection, read_only=False)
            _lock(connection, namespace)
            state = _inspect(connection, namespace, layout)
            if state.status == "ready":
                _rollback(connection)
                return state
            if state.status != "unmaterialised":
                _rollback(connection)
                return state
            for statement in layout.ddl:
                _execute(connection, statement)
            state = _inspect(connection, namespace, layout)
            if state.status != "ready":
                _rollback(connection)
                return state
            commit_started = True
            _commit(connection)
        except StorageContractError:
            raise
        except StorageUnavailable:
            raise
        except Exception as error:
            _rollback(connection)
            raise _failure(error, commit_started=commit_started) from None
        finally:
            _close(connection)
        return self.inspect(locator, specification)

    def evolve(
        self, locator: ServiceStoreLocator, transition: RelationalTransition
    ) -> DomainSchemaState:
        namespace = _namespace(locator)
        plan = build_postgresql_domain_transition_layout(transition, namespace)
        connection: object | None = None
        commit_started = False
        try:
            self._require_foundation(locator)
            connection = self._connect(namespace)
            _begin(connection, read_only=False)
            _lock(connection, namespace)
            if _inspect(connection, namespace, plan.target).status == "ready":
                _rollback(connection)
                return DomainSchemaState("ready")
            if _inspect(connection, namespace, plan.current).status != "ready":
                _rollback(connection)
                return DomainSchemaState("incompatible")
            for statement in plan.ddl:
                _execute(connection, statement)
            state = _inspect(connection, namespace, plan.target)
            if state.status != "ready":
                _rollback(connection)
                return state
            commit_started = True
            _commit(connection)
        except StorageContractError:
            raise
        except StorageUnavailable:
            raise
        except Exception as error:
            _rollback(connection)
            raise _failure(error, commit_started=commit_started) from None
        finally:
            _close(connection)
        return self.inspect(locator, transition.target)

    def _connect(self, namespace: str) -> object:
        return self._connection_factory(
            self._dsn,
            PostgreSQLConnectionPolicy(
                schema=namespace,
                connect_timeout_seconds=self._connect_timeout_seconds,
                lock_timeout_ms=self._lock_timeout_ms,
                statement_timeout_ms=self._statement_timeout_ms,
            ),
        )

    def _require_foundation(self, locator: ServiceStoreLocator) -> None:
        if self._catalog.inspect_migration(locator).status != "ready":
            raise StorageMigrationError()


def _namespace(locator: ServiceStoreLocator) -> str:
    if type(locator) is not ServiceStoreLocator or locator.backend != "postgresql":
        raise StorageContractError()
    return locator.namespace


def _inspect(
    connection: object, namespace: str, layout: PostgreSQLDomainLayout
) -> DomainSchemaState:
    tables = _tables(connection, namespace)
    expected_tables = {table.name for table in layout.tables}
    if not tables:
        return DomainSchemaState("unmaterialised")
    if tables != expected_tables:
        return DomainSchemaState("incompatible")
    if not _relations_owned_and_private(connection, namespace, layout):
        return DomainSchemaState("incompatible")
    if not _columns_match(connection, namespace, layout):
        return DomainSchemaState("incompatible")
    if not _constraints_match(connection, namespace, layout):
        return DomainSchemaState("incompatible")
    if not _indexes_match(connection, namespace, layout):
        return DomainSchemaState("incompatible")
    return DomainSchemaState("ready")


def _tables(connection: object, namespace: str) -> set[str]:
    rows = _fetchall(
        _execute(
            connection,
            """SELECT c.relname, c.relkind
                 FROM pg_catalog.pg_class AS c
                 JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                 WHERE n.nspname = %s
                   AND c.relname NOT LIKE '__aipcs_%%'
                   AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
                 ORDER BY c.relname""",
            (namespace,),
        )
    )
    if any(len(row) != 2 or type(row[0]) is not str or row[1] != "r" for row in rows):
        raise StorageMigrationError()
    return {row[0] for row in rows}


def _relations_owned_and_private(
    connection: object, namespace: str, layout: PostgreSQLDomainLayout
) -> bool:
    rows = _fetchall(
        _execute(
            connection,
            """SELECT c.relname, c.relkind,
                       c.relowner = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user),
                       EXISTS (
                           SELECT 1 FROM pg_catalog.aclexplode(
                               coalesce(c.relacl, pg_catalog.acldefault('r', c.relowner))
                           ) AS acl WHERE acl.grantee = 0
                       )
                 FROM pg_catalog.pg_class AS c
                 JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                 WHERE n.nspname = %s AND c.relname NOT LIKE '__aipcs_%%'
                   AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S', 'i')
                 ORDER BY c.relname""",
            (namespace,),
        )
    )
    expected = {(table.name, "r") for table in layout.tables}
    expected.update((table.primary_key_name, "i") for table in layout.tables)
    expected.update((index.name, "i") for index in layout.indices)
    actual: set[tuple[str, str]] = set()
    for row in rows:
        if len(row) != 4 or type(row[0]) is not str or type(row[1]) is not str:
            return False
        if type(row[2]) is not bool or not row[2] or type(row[3]) is not bool or row[3]:
            return False
        actual.add((row[0], row[1]))
    return actual == expected


def _columns_match(connection: object, namespace: str, layout: PostgreSQLDomainLayout) -> bool:
    rows = _fetchall(
        _execute(
            connection,
            """SELECT c.relname, a.attname,
                       pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull,
                       a.attidentity, a.atthasdef, coll.collname
                 FROM pg_catalog.pg_class AS c
                 JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                 JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.oid
                 LEFT JOIN pg_catalog.pg_collation AS coll ON coll.oid = a.attcollation
                 WHERE n.nspname = %s AND c.relname NOT LIKE '__aipcs_%%'
                   AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
                 ORDER BY c.relname, a.attnum""",
            (namespace,),
        )
    )
    actual: dict[str, list[tuple[str, str, bool, str, bool, str | None]]] = {}
    for row in rows:
        if (
            len(row) != 7
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not str
            or type(row[3]) is not bool
            or type(row[4]) is not str
            or type(row[5]) is not bool
            or (row[6] is not None and type(row[6]) is not str)
        ):
            return False
        actual.setdefault(row[0], []).append((row[1], row[2], row[3], row[4], row[5], row[6]))
    expected = {
        table.name: [
            (name, data_type, not_null, "", False, collation)
            for name, data_type, not_null, collation in table.columns
        ]
        for table in layout.tables
    }
    return actual == expected


def _constraints_match(connection: object, namespace: str, layout: PostgreSQLDomainLayout) -> bool:
    # PostgreSQL 18 projects NOT NULL attributes into pg_constraint as
    # contype='n'. _columns_match remains the exact authority for attnotnull;
    # this query admits only the constraint kinds owned by the domain layout.
    rows = _fetchall(
        _execute(
            connection,
            """SELECT con.conname, source.relname, con.contype, con.condeferrable,
                       con.condeferred, con.convalidated, con.confupdtype, con.confdeltype,
                       ARRAY(SELECT attribute.attname
                             FROM unnest(con.conkey) WITH ORDINALITY AS key(attnum, ordinal)
                             JOIN pg_catalog.pg_attribute AS attribute
                               ON attribute.attrelid = con.conrelid AND attribute.attnum = key.attnum
                             ORDER BY key.ordinal),
                       target_namespace.nspname, target.relname,
                       ARRAY(SELECT attribute.attname
                             FROM unnest(con.confkey) WITH ORDINALITY AS key(attnum, ordinal)
                             JOIN pg_catalog.pg_attribute AS attribute
                               ON attribute.attrelid = con.confrelid AND attribute.attnum = key.attnum
                             ORDER BY key.ordinal)
                 FROM pg_catalog.pg_constraint AS con
                 JOIN pg_catalog.pg_class AS source ON source.oid = con.conrelid
                 JOIN pg_catalog.pg_namespace AS n ON n.oid = source.relnamespace
                 LEFT JOIN pg_catalog.pg_class AS target ON target.oid = con.confrelid
                 LEFT JOIN pg_catalog.pg_namespace AS target_namespace ON target_namespace.oid = target.relnamespace
                 WHERE n.nspname = %s AND source.relname NOT LIKE '__aipcs_%%'
                   AND con.contype IN ('c', 'p', 'u', 'f')
                 ORDER BY source.relname, con.contype, con.conname""",
            (namespace,),
        )
    )
    expected: set[tuple[object, ...]] = set()
    for table in layout.tables:
        expected.add(
            (
                table.primary_key_name,
                table.name,
                "p",
                False,
                False,
                True,
                " ",
                " ",
                ("id",),
                None,
                None,
                (),
            )
        )
        expected.update(
            (
                item.name,
                item.source_entity,
                "f",
                False,
                False,
                True,
                "r",
                "r",
                (item.source_field,),
                namespace,
                item.target_entity,
                (item.target_field,),
            )
            for item in table.relationships
        )
    actual: set[tuple[object, ...]] = set()
    for row in rows:
        if (
            len(row) != 12
            or not all(type(row[index]) is str for index in (0, 1, 2, 6, 7))
            or not all(type(row[index]) is bool for index in (3, 4, 5))
        ):
            return False
        keys, target_schema, target, target_keys = row[8], row[9], row[10], row[11]
        if type(keys) not in {list, tuple} or type(target_keys) not in {list, tuple}:
            return False
        if target_schema is not None and type(target_schema) is not str:
            return False
        if target is not None and type(target) is not str:
            return False
        actual.add((*row[:8], tuple(keys), target_schema, target, tuple(target_keys)))
    return actual == expected


def _indexes_match(connection: object, namespace: str, layout: PostgreSQLDomainLayout) -> bool:
    rows = _fetchall(
        _execute(
            connection,
            """SELECT table_relation.relname, index_relation.relname, idx.indisunique,
                       idx.indisprimary, idx.indpred IS NOT NULL, idx.indisvalid,
                       idx.indisready, idx.indislive, idx.indnullsnotdistinct,
                       am.amname, idx.indnkeyatts, idx.indnatts,
                       ARRAY(SELECT attribute.attname
                             FROM unnest(idx.indkey) WITH ORDINALITY AS key(attnum, ordinal)
                             JOIN pg_catalog.pg_attribute AS attribute
                               ON attribute.attrelid = idx.indrelid AND attribute.attnum = key.attnum
                             ORDER BY key.ordinal),
                       ARRAY(SELECT option_values.value
                             FROM unnest(idx.indoption) WITH ORDINALITY AS option_values(value, ordinal)
                             ORDER BY option_values.ordinal),
                       ARRAY(SELECT opclass.opcname
                             FROM unnest(idx.indclass) WITH ORDINALITY AS key(opclass_oid, ordinal)
                             JOIN pg_catalog.pg_opclass AS opclass ON opclass.oid = key.opclass_oid
                             ORDER BY key.ordinal),
                       ARRAY(SELECT coll.collname
                             FROM unnest(idx.indcollation) WITH ORDINALITY AS key(collation_oid, ordinal)
                             LEFT JOIN pg_catalog.pg_collation AS coll ON coll.oid = key.collation_oid
                             ORDER BY key.ordinal)
                 FROM pg_catalog.pg_index AS idx
                 JOIN pg_catalog.pg_class AS index_relation ON index_relation.oid = idx.indexrelid
                 JOIN pg_catalog.pg_class AS table_relation ON table_relation.oid = idx.indrelid
                 JOIN pg_catalog.pg_namespace AS n ON n.oid = table_relation.relnamespace
                 JOIN pg_catalog.pg_am AS am ON am.oid = index_relation.relam
                 WHERE n.nspname = %s AND table_relation.relname NOT LIKE '__aipcs_%%'
                 ORDER BY table_relation.relname, index_relation.relname""",
            (namespace,),
        )
    )
    expected = {
        (
            index.table,
            index.name,
            index.unique,
            False,
            False,
            True,
            True,
            True,
            False,
            "btree",
            len(index.fields),
            len(index.fields),
            index.fields,
            (0,) * len(index.fields),
            tuple(value[0] for value in index.key_signatures),
            tuple(value[1] for value in index.key_signatures),
        )
        for index in layout.indices
    }
    primary = {
        (
            table.name,
            table.primary_key_name,
            True,
            True,
            False,
            True,
            True,
            True,
            False,
            "btree",
            1,
            1,
            ("id",),
            (0,),
            (table.primary_key_signature[0],),
            (table.primary_key_signature[1],),
        )
        for table in layout.tables
    }
    actual: set[tuple[object, ...]] = set()
    for row in rows:
        if (
            len(row) != 16
            or type(row[0]) is not str
            or type(row[1]) is not str
            or not all(type(row[index]) is bool for index in range(2, 9))
            or type(row[9]) is not str
            or type(row[10]) is not int
            or type(row[11]) is not int
        ):
            return False
        fields, options, opclasses, collations = row[12], row[13], row[14], row[15]
        if type(fields) not in {list, tuple} or any(type(value) is not str for value in fields):
            return False
        if type(options) not in {list, tuple} or any(type(value) is not int for value in options):
            return False
        if type(opclasses) not in {list, tuple} or any(
            type(value) is not str for value in opclasses
        ):
            return False
        if type(collations) not in {list, tuple} or any(
            value is not None and type(value) is not str for value in collations
        ):
            return False
        actual.add((*row[:12], tuple(fields), tuple(options), tuple(opclasses), tuple(collations)))
    return actual == expected | primary


def _begin(connection: object, *, read_only: bool) -> None:
    _execute(connection, "BEGIN READ ONLY" if read_only else "BEGIN")


def _lock(connection: object, namespace: str) -> None:
    _execute(
        connection,
        "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s, 0))",
        (f"aipcs:domain_schema:{namespace}",),
    )


def _execute(connection: object, statement: str, parameters: tuple[object, ...] = ()) -> object:
    execute = getattr(connection, "execute", None)
    if not callable(execute):
        raise StorageUnavailable()
    return execute(statement, parameters)


def _fetchall(result: object) -> list[tuple[object, ...]]:
    fetchall = getattr(result, "fetchall", None)
    if not callable(fetchall):
        raise StorageMigrationError()
    return [tuple(row) for row in fetchall()]


def _commit(connection: object) -> None:
    commit = getattr(connection, "commit", None)
    if not callable(commit):
        raise StorageUnavailable()
    commit()


def _rollback(connection: object | None) -> None:
    rollback = getattr(connection, "rollback", None) if connection is not None else None
    if callable(rollback):
        with suppress(Exception):
            rollback()


def _close(connection: object | None) -> None:
    close = getattr(connection, "close", None) if connection is not None else None
    if callable(close):
        with suppress(Exception):
            close()


def _failure(
    error: BaseException, *, commit_started: bool
) -> StorageBusy | StorageMigrationError | StorageUnavailable:
    """Map connection phase and SQLSTATE without guessing after commit begins."""

    if commit_started:
        return StorageMigrationError()
    state = getattr(error, "sqlstate", None)
    if type(state) is str and state.startswith("08"):
        return StorageUnavailable()
    return StorageBusy() if is_postgresql_busy(error) else StorageMigrationError()
