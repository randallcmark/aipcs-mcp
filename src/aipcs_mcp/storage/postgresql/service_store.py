"""Private PostgreSQL service-schema foundation catalogue.

The catalogue owns only per-service schema allocation and R1 foundation
readiness.  It neither materialises caller schemas nor opens registry state.
Every database action is schema-qualified and the public contract receives
only opaque locators and bounded migration state.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID

from aipcs_mcp.storage.contracts import MigrationState, ServiceStoreLocator, StorageAdapterInfo
from aipcs_mcp.storage.errors import (
    StorageBusy,
    StorageContractError,
    StorageMigrationError,
    StorageUnavailable,
)

from .connection import PostgreSQLConnectionPolicy, PostgreSQLDsn, connect_postgresql
from .result_codes import is_postgresql_busy
from .service_store_migrations import (
    ADAPTER_ID,
    CHECK_EXPRESSIONS,
    CHECKSUM,
    COLUMN_COLLATIONS,
    COMPONENT,
    CONSTRAINT_KEYS,
    CONSTRAINTS,
    INDEX_COLLATIONS,
    INDEX_COLUMNS,
    INDEX_OPCLASSES,
    INDEX_OPTIONS,
    INDEX_PREDICATES,
    INDEXES,
    META,
    MIGRATION,
    MIGRATION_ID,
    RESERVED_PREFIX,
    SEQUENCES,
    TABLE_COLUMNS,
    TARGET_REVISION,
    ddl,
    qualified,
    quote_identifier,
)


class ConnectionFactory(Protocol):
    def __call__(self, dsn: PostgreSQLDsn, policy: PostgreSQLConnectionPolicy) -> object: ...


class PostgreSQLServiceStoreCatalog:
    """Own exact namespace-bound PostgreSQL service-store R1 state."""

    def __init__(
        self,
        dsn: PostgreSQLDsn,
        *,
        connect_timeout_seconds: int = 10,
        lock_timeout_ms: int = 5_000,
        statement_timeout_ms: int = 30_000,
        connection_factory: ConnectionFactory = connect_postgresql,
    ) -> None:
        if (
            type(dsn) is not PostgreSQLDsn
            or not callable(connection_factory)
            or type(connect_timeout_seconds) is not int
            or type(lock_timeout_ms) is not int
            or type(statement_timeout_ms) is not int
        ):
            raise StorageUnavailable()
        self._dsn = dsn
        self._connect_timeout_seconds = connect_timeout_seconds
        self._lock_timeout_ms = lock_timeout_ms
        self._statement_timeout_ms = statement_timeout_ms
        self._connection_factory = connection_factory

    def __repr__(self) -> str:
        return "PostgreSQLServiceStoreCatalog(<redacted>)"

    def info(self) -> StorageAdapterInfo:
        return StorageAdapterInfo("postgresql", frozenset({"service_store"}))

    def allocate(self, service_id: UUID) -> ServiceStoreLocator:
        return ServiceStoreLocator.for_service("postgresql", service_id)

    def inspect_migration(self, locator: ServiceStoreLocator) -> MigrationState:
        namespace = _namespace(locator)
        connection: object | None = None
        try:
            connection = self._connect(namespace)
            _begin(connection, read_only=True)
            _require_supported_server(connection)
            state = _inspect(connection, namespace)
            _rollback(connection)
            return state
        except StorageContractError:
            raise
        except StorageUnavailable:
            raise
        except Exception as error:
            raise _inspection_failure(error) from None
        finally:
            _close(connection)

    def migrate(self, locator: ServiceStoreLocator) -> MigrationState:
        namespace = _namespace(locator)
        connection: object | None = None
        commit_started = False
        try:
            connection = self._connect(namespace)
            _begin(connection, read_only=False)
            _require_supported_server(connection)
            _acquire_advisory_lock(connection, namespace)
            observed = _inspect(connection, namespace)
            if observed.status == "uninitialised":
                _create_r1(connection, namespace)
                observed = _inspect(connection, namespace)
                if observed.status != "ready":
                    _rollback(connection)
                    return observed
            if observed.status != "ready":
                _rollback(connection)
                return observed
            commit_started = True
            _commit(connection)
        except StorageContractError:
            raise
        except StorageUnavailable:
            raise
        except Exception as error:
            _rollback(connection)
            if commit_started:
                raise StorageMigrationError() from None
            raise _migration_failure(error) from None
        finally:
            _close(connection)
        # The commit did not prove durable readiness to a caller.  Reinspect on
        # an independent read-only snapshot; this also makes concurrent callers
        # converge on the committed state rather than stale transaction evidence.
        return self.inspect_migration(locator)

    def _connect(self, namespace: str) -> object:
        policy = PostgreSQLConnectionPolicy(
            schema=namespace,
            connect_timeout_seconds=self._connect_timeout_seconds,
            lock_timeout_ms=self._lock_timeout_ms,
            statement_timeout_ms=self._statement_timeout_ms,
        )
        return self._connection_factory(self._dsn, policy)


def _namespace(locator: ServiceStoreLocator) -> str:
    if type(locator) is not ServiceStoreLocator or locator.backend != "postgresql":
        raise StorageContractError()
    return locator.namespace


def _state(applied_revision: int, status: str) -> MigrationState:
    return MigrationState("service_store", applied_revision, TARGET_REVISION, status)  # type: ignore[arg-type]


def _begin(connection: object, *, read_only: bool) -> None:
    _execute(connection, "BEGIN READ ONLY" if read_only else "BEGIN")


def _acquire_advisory_lock(connection: object, namespace: str) -> None:
    _execute(
        connection,
        "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s, 0))",
        (f"aipcs:service_store:{namespace}",),
    )


def _require_supported_server(connection: object) -> None:
    row = _fetchone(_execute(connection, "SELECT current_setting('server_version_num')::integer"))
    version = row[0] if row is not None else None
    if type(version) is not int or not 160_000 <= version < 190_000:
        raise StorageUnavailable()


def _inspect(connection: object, namespace: str) -> MigrationState:
    schema = _schema(connection, namespace)
    if schema is None:
        return _state(0, "uninitialised")
    schema_oid, owner_matches, public_usage, public_create = schema
    if not owner_matches or public_usage or public_create:
        return _state(0, "incompatible")
    relations = _relations(connection, schema_oid)
    revision = _revision_hint(connection, namespace, relations)
    if not _relations_match(relations):
        return _state(revision, "incompatible")
    if not _owners_and_public_acl_match(connection, schema_oid):
        return _state(revision, "incompatible")
    if not _columns_match(connection, schema_oid):
        return _state(revision, "incompatible")
    if not _constraints_match(connection, schema_oid):
        return _state(revision, "incompatible")
    if not _indexes_match(connection, schema_oid):
        return _state(revision, "incompatible")
    if not _metadata_matches(connection, namespace):
        return _state(revision, "incompatible")
    return _state(TARGET_REVISION, "ready")


def _schema(connection: object, namespace: str) -> tuple[int, bool, bool, bool] | None:
    row = _fetchone(
        _execute(
            connection,
            """SELECT n.oid, n.nspowner = (SELECT oid FROM pg_catalog.pg_roles
                   WHERE rolname = current_user),
                   EXISTS (
                       SELECT 1 FROM pg_catalog.aclexplode(
                           coalesce(n.nspacl, pg_catalog.acldefault('n', n.nspowner))
                       ) AS acl WHERE acl.grantee = 0 AND acl.privilege_type = 'USAGE'
                   ),
                   EXISTS (
                       SELECT 1 FROM pg_catalog.aclexplode(
                           coalesce(n.nspacl, pg_catalog.acldefault('n', n.nspowner))
                       ) AS acl WHERE acl.grantee = 0 AND acl.privilege_type = 'CREATE'
                   )
                 FROM pg_catalog.pg_namespace AS n WHERE n.nspname = %s""",
            (namespace,),
        )
    )
    if row is None:
        return None
    if (
        len(row) != 4
        or type(row[0]) is not int
        or not all(type(value) is bool for value in row[1:])
    ):
        raise StorageMigrationError()
    return cast(tuple[int, bool, bool, bool], tuple(row))


def _relations(connection: object, schema_oid: int) -> tuple[tuple[str, str], ...]:
    rows = _fetchall(
        _execute(
            connection,
            """SELECT c.relname, c.relkind
                 FROM pg_catalog.pg_class AS c
                 WHERE c.relnamespace = %s AND c.relkind IN ('r', 'p', 'i', 'S', 'v', 'm', 'f')
                 ORDER BY c.relname""",
            (schema_oid,),
        )
    )
    return tuple((str(name), str(kind)) for name, kind in rows)


def _relations_match(relations: tuple[tuple[str, str], ...]) -> bool:
    expected = {
        **{name: "r" for name in TABLE_COLUMNS},
        **{name: "i" for name in INDEXES},
        **{name: "S" for name in SEQUENCES},
    }
    reserved = {name: kind for name, kind in relations if name.startswith(RESERVED_PREFIX)}
    return reserved == expected


def _owners_and_public_acl_match(connection: object, schema_oid: int) -> bool:
    rows = _fetchall(
        _execute(
            connection,
            """SELECT c.relname,
                       c.relowner = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user),
                       EXISTS (
                           SELECT 1 FROM pg_catalog.aclexplode(
                               coalesce(c.relacl, pg_catalog.acldefault('r', c.relowner))
                           ) AS acl
                           WHERE acl.grantee = 0
                       )
                 FROM pg_catalog.pg_class AS c
                 WHERE c.relnamespace = %s AND c.relname LIKE '__aipcs_%%'
                   AND c.relkind IN ('r', 'p', 'S')""",
            (schema_oid,),
        )
    )
    return len(rows) == len(TABLE_COLUMNS) + len(SEQUENCES) and all(
        type(owner) is bool and owner and type(public_access) is bool and not public_access
        for _name, owner, public_access in rows
    )


def _columns_match(connection: object, schema_oid: int) -> bool:
    rows = _fetchall(
        _execute(
            connection,
            """SELECT c.relname, a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod),
                       a.attnotnull, a.attidentity, a.atthasdef, attribute_collation.collname
                 FROM pg_catalog.pg_class AS c
                 JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.oid
                 LEFT JOIN pg_catalog.pg_collation AS attribute_collation
                   ON attribute_collation.oid = a.attcollation
                 WHERE c.relnamespace = %s AND c.relname LIKE '__aipcs_%%'
                   AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
                 ORDER BY c.relname, a.attnum""",
            (schema_oid,),
        )
    )
    actual: dict[str, list[tuple[str, str, bool, str, bool, str | None]]] = {}
    for table, name, data_type, not_null, identity, has_default, collation in rows:
        if not all(
            (
                type(table) is str,
                type(name) is str,
                type(data_type) is str,
                type(not_null) is bool,
                type(identity) is str,
                type(has_default) is bool,
                collation is None or type(collation) is str,
            )
        ):
            return False
        actual.setdefault(table, []).append(
            (name, data_type, not_null, identity, has_default, collation)
        )
    expected = {
        table: [
            (name, data_type, not_null, identity, False, collation)
            for (name, data_type, not_null, identity), collation in zip(
                columns, COLUMN_COLLATIONS[table], strict=True
            )
        ]
        for table, columns in TABLE_COLUMNS.items()
    }
    return actual == expected


def _constraints_match(connection: object, schema_oid: int) -> bool:
    # PostgreSQL 18 projects NOT NULL attributes into pg_constraint as
    # contype='n'. _columns_match remains the exact authority for attnotnull;
    # this query admits only the constraint kinds owned by the R1 descriptor.
    rows = _fetchall(
        _execute(
            connection,
            """SELECT table_relation.relname, con.conname, con.contype,
                       ARRAY(
                           SELECT attribute.attname
                           FROM unnest(con.conkey) WITH ORDINALITY AS key_column(attnum, ordinal)
                           JOIN pg_catalog.pg_attribute AS attribute
                             ON attribute.attrelid = con.conrelid
                            AND attribute.attnum = key_column.attnum
                           ORDER BY key_column.ordinal
                       ),
                       referenced_relation.relname,
                       ARRAY(
                           SELECT attribute.attname
                           FROM unnest(con.confkey) WITH ORDINALITY AS key_column(attnum, ordinal)
                           JOIN pg_catalog.pg_attribute AS attribute
                             ON attribute.attrelid = con.confrelid
                            AND attribute.attnum = key_column.attnum
                           ORDER BY key_column.ordinal
                       ),
                       con.confupdtype, con.confdeltype,
                       pg_catalog.pg_get_expr(con.conbin, con.conrelid, false)
                 FROM pg_catalog.pg_constraint AS con
                 JOIN pg_catalog.pg_class AS table_relation ON table_relation.oid = con.conrelid
                 LEFT JOIN pg_catalog.pg_class AS referenced_relation ON referenced_relation.oid = con.confrelid
                 WHERE table_relation.relnamespace = %s AND con.conname LIKE '__aipcs_%%'
                   AND con.contype IN ('c', 'p', 'u', 'f')
                 ORDER BY con.conname""",
            (schema_oid,),
        )
    )
    names = {str(row[1]) for row in rows}
    if names != CONSTRAINTS:
        return False
    for (
        table,
        name,
        kind,
        key_columns,
        referenced_table,
        referenced_columns,
        update_action,
        delete_action,
        expression,
    ) in rows:
        if type(table) is not str or type(name) is not str or type(kind) is not str:
            return False
        if kind == "c":
            expected_expression = CHECK_EXPRESSIONS.get(name)
            if expected_expression is None or expression != expected_expression:
                return False
            continue
        expected = CONSTRAINT_KEYS.get(name)
        if expected is None:
            return False
        (
            expected_table,
            expected_kind,
            expected_keys,
            expected_reference,
            expected_reference_keys,
        ) = expected
        if (
            table != expected_table
            or kind != expected_kind
            or _catalog_columns(key_columns) != expected_keys
            or referenced_table != expected_reference
            or _catalog_columns(referenced_columns) != expected_reference_keys
        ):
            return False
        if kind == "f" and (update_action != "r" or delete_action != "r"):
            return False
    return True


def _indexes_match(connection: object, schema_oid: int) -> bool:
    rows = _fetchall(
        _execute(
            connection,
            """SELECT index_relation.relname, table_relation.relname, idx.indisunique,
                       idx.indisprimary, idx.indpred IS NOT NULL, access_method.amname,
                       ARRAY(
                           SELECT attribute.attname
                           FROM unnest(idx.indkey) WITH ORDINALITY AS key_column(attnum, ordinal)
                           JOIN pg_catalog.pg_attribute AS attribute
                             ON attribute.attrelid = idx.indrelid
                            AND attribute.attnum = key_column.attnum
                           ORDER BY key_column.ordinal
                       ),
                       ARRAY(
                           SELECT operator_class.opcname
                           FROM unnest(idx.indclass) WITH ORDINALITY AS key_class(class_oid, ordinal)
                           JOIN pg_catalog.pg_opclass AS operator_class ON operator_class.oid = key_class.class_oid
                           ORDER BY key_class.ordinal
                       ),
                       ARRAY(
                           SELECT index_collation.collname
                           FROM unnest(idx.indcollation) WITH ORDINALITY AS key_collation(collation_oid, ordinal)
                           LEFT JOIN pg_catalog.pg_collation AS index_collation
                             ON index_collation.oid = key_collation.collation_oid
                           ORDER BY key_collation.ordinal
                       ),
                       ARRAY(
                           SELECT key_option.option_value
                           FROM unnest(idx.indoption) WITH ORDINALITY AS key_option(option_value, ordinal)
                           ORDER BY key_option.ordinal
                       ),
                       pg_catalog.pg_get_expr(idx.indpred, idx.indrelid, false)
                 FROM pg_catalog.pg_index AS idx
                 JOIN pg_catalog.pg_class AS index_relation ON index_relation.oid = idx.indexrelid
                 JOIN pg_catalog.pg_class AS table_relation ON table_relation.oid = idx.indrelid
                 JOIN pg_catalog.pg_am AS access_method ON access_method.oid = index_relation.relam
                 WHERE index_relation.relnamespace = %s AND index_relation.relname LIKE '__aipcs_%%'
                 ORDER BY index_relation.relname""",
            (schema_oid,),
        )
    )
    actual: dict[str, tuple[str, bool, bool, bool]] = {}
    for (
        name,
        table,
        unique,
        primary,
        partial,
        access_method,
        columns,
        opclasses,
        collations,
        options,
        predicate,
    ) in rows:
        if not (
            type(name) is str
            and type(table) is str
            and type(unique) is bool
            and type(primary) is bool
            and type(partial) is bool
            and access_method == "btree"
        ):
            return False
        if (
            _catalog_columns(columns) != INDEX_COLUMNS.get(name)
            or _catalog_columns(opclasses) != INDEX_OPCLASSES.get(name)
            or _catalog_collations(collations) != INDEX_COLLATIONS.get(name)
            or _catalog_options(options) != INDEX_OPTIONS.get(name)
            or predicate != INDEX_PREDICATES.get(name)
        ):
            return False
        actual[name] = (table, unique, primary, partial)
    return actual == INDEXES


def _catalog_columns(value: object) -> tuple[str, ...] | None:
    """Normalise a PostgreSQL catalog array without accepting expression keys."""

    if not isinstance(value, (list, tuple)) or not all(type(column) is str for column in value):
        return None
    return tuple(value)


def _catalog_collations(value: object) -> tuple[str | None, ...] | None:
    if not isinstance(value, (list, tuple)) or not all(
        column is None or type(column) is str for column in value
    ):
        return None
    return tuple(value)


def _catalog_options(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, (list, tuple)) or not all(type(option) is int for option in value):
        return None
    return tuple(value)


def _revision_hint(
    connection: object, namespace: str, relations: tuple[tuple[str, str], ...]
) -> int:
    if (META, "r") not in relations:
        return 0
    try:
        row = _fetchone(
            _execute(connection, f"SELECT applied_revision FROM {qualified(namespace, META)}")
        )
        return row[0] if row is not None and type(row[0]) is int and row[0] >= 0 else 0
    except Exception as error:
        if is_postgresql_busy(error) or _is_connection_error(error):
            raise
        return 0


def _metadata_matches(connection: object, namespace: str) -> bool:
    meta_rows = _fetchall(
        _execute(
            connection,
            f"SELECT singleton, adapter_id, component, namespace, applied_revision "
            f"FROM {qualified(namespace, META)}",
        )
    )
    if meta_rows != [(True, ADAPTER_ID, COMPONENT, namespace, TARGET_REVISION)]:
        return False
    rows = _fetchall(
        _execute(
            connection,
            f"SELECT component, revision, migration_id, checksum, "
            f"CASE WHEN pg_catalog.isfinite(applied_at) THEN applied_at ELSE NULL END, "
            f"pg_catalog.isfinite(applied_at), "
            f"date_trunc('microseconds', applied_at) = applied_at "
            f"FROM {qualified(namespace, MIGRATION)} ORDER BY revision",
        )
    )
    if len(rows) != 1:
        return False
    component, revision, migration_id, checksum, applied_at, finite, microsecond_precise = rows[0]
    if (component, revision, migration_id, checksum) != (
        COMPONENT,
        TARGET_REVISION,
        MIGRATION_ID,
        CHECKSUM,
    ):
        return False
    if (
        type(applied_at) is not datetime
        or type(finite) is not bool
        or type(microsecond_precise) is not bool
        or not finite
        or not microsecond_precise
        or applied_at.tzinfo is None
        or applied_at.utcoffset() is None
    ):
        return False
    try:
        return applied_at.astimezone(UTC).tzinfo is UTC
    except (OverflowError, ValueError):
        return False


def _create_r1(connection: object, namespace: str) -> None:
    schema = quote_identifier(namespace)
    _execute(connection, f"CREATE SCHEMA {schema} AUTHORIZATION CURRENT_USER")
    _execute(connection, f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC")
    for statement in ddl(namespace):
        _execute(connection, statement)
    _execute(connection, f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM PUBLIC")
    _execute(connection, f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM PUBLIC")
    _execute(
        connection,
        f"INSERT INTO {qualified(namespace, META)} "
        "(singleton, adapter_id, component, namespace, applied_revision) VALUES (%s, %s, %s, %s, %s)",
        (True, ADAPTER_ID, COMPONENT, namespace, TARGET_REVISION),
    )
    _execute(
        connection,
        f"INSERT INTO {qualified(namespace, MIGRATION)} "
        "(component, revision, migration_id, checksum, applied_at) "
        "VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)",
        (COMPONENT, TARGET_REVISION, MIGRATION_ID, CHECKSUM),
    )


def _execute(connection: object, statement: str, parameters: tuple[object, ...] = ()) -> object:
    execute = getattr(connection, "execute", None)
    if not callable(execute):
        raise StorageUnavailable()
    return execute(statement, parameters)


def _fetchone(result: object) -> tuple[object, ...] | None:
    fetchone = getattr(result, "fetchone", None)
    if not callable(fetchone):
        raise StorageMigrationError()
    row = fetchone()
    return None if row is None else tuple(row)


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
    if connection is None:
        return
    rollback = getattr(connection, "rollback", None)
    if callable(rollback):
        with suppress(Exception):
            rollback()


def _close(connection: object | None) -> None:
    if connection is None:
        return
    close = getattr(connection, "close", None)
    if callable(close):
        with suppress(Exception):
            close()


def _inspection_failure(
    error: BaseException,
) -> StorageMigrationError | StorageBusy | StorageUnavailable:
    if _is_connection_error(error):
        return StorageUnavailable()
    return StorageBusy() if is_postgresql_busy(error) else StorageMigrationError()


def _migration_failure(
    error: BaseException,
) -> StorageMigrationError | StorageBusy | StorageUnavailable:
    if _is_connection_error(error):
        return StorageUnavailable()
    return StorageBusy() if is_postgresql_busy(error) else StorageMigrationError()


def _is_connection_error(error: BaseException) -> bool:
    state = getattr(error, "sqlstate", None)
    return type(state) is str and state.startswith("08")
