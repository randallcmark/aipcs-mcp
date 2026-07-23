"""Read-only structured PostgreSQL registry inspection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from aipcs_mcp.application.models import Service
from aipcs_mcp.storage.codecs import (
    decode_service,
    decode_time,
    encode_time,
    validate_audit_row,
    validate_r2_mutation_row,
)
from aipcs_mcp.storage.contracts import MigrationState
from aipcs_mcp.storage.errors import StorageMigrationError, StorageUnavailable

from .registry_migrations import (
    ADAPTER_ID,
    CHECK_EXPRESSION_DIGEST,
    CHECK_TOKENS,
    CHECKSUM,
    CONSTRAINT_KEYS,
    CONSTRAINT_TYPES,
    INDEX_COLLATIONS,
    INDEX_COLUMNS,
    INDEX_OPCLASSES,
    INDEX_OPTIONS,
    INDEX_PREDICATE_TOKENS,
    INDEX_PREDICATES,
    INDEX_SIGNATURES,
    MIGRATION_ID,
    SCHEMA,
    SEQUENCES,
    TABLE_COLUMNS,
    TARGET_REVISION,
)

_TABLES = frozenset(TABLE_COLUMNS)


def inspect_registry(connection: object) -> MigrationState:
    """Inspect only catalog and registry rows; never create or repair storage."""

    version = _execute(
        connection,
        "SELECT pg_catalog.current_setting('server_version_num')",
    ).fetchone()
    if (
        not isinstance(version, (tuple, list))
        or len(version) != 1
        or type(version[0]) is not str
        or not version[0].isdigit()
        or int(version[0]) // 10_000 not in {16, 17, 18}
    ):
        raise StorageUnavailable()

    schema = _execute(
        connection,
        "SELECT n.oid,"
        "(n.nspowner=(SELECT r.oid FROM pg_catalog.pg_roles AS r "
        "WHERE r.rolname=CURRENT_USER)),"
        "NOT EXISTS ("
        "SELECT 1 FROM pg_catalog.aclexplode("
        "COALESCE(n.nspacl,pg_catalog.acldefault('n',n.nspowner))) AS a "
        "WHERE a.grantee=0) "
        "FROM pg_catalog.pg_namespace AS n WHERE n.nspname=%s",
        (SCHEMA,),
    ).fetchone()
    if schema is None:
        return _state(0, "uninitialised")
    if (
        not isinstance(schema, (tuple, list))
        or len(schema) != 3
        or type(schema[0]) is not int
        or schema[0] <= 0
        or type(schema[1]) is not bool
        or type(schema[2]) is not bool
    ):
        return _state(0, "incompatible")
    schema_secure = schema[1] and schema[2]

    relations = _execute(
        connection,
        "SELECT c.relname,c.relkind,"
        "(c.relowner=(SELECT r.oid FROM pg_catalog.pg_roles AS r "
        "WHERE r.rolname=CURRENT_USER)) "
        "FROM pg_catalog.pg_class AS c "
        "JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relkind IN ('r','p','i','S','v','m','f') "
        "ORDER BY c.relname",
        (SCHEMA,),
    ).fetchall()
    relation_map: dict[str, str] = {}
    for row in relations:
        if (
            not isinstance(row, (tuple, list))
            or len(row) != 3
            or type(row[0]) is not str
            or type(row[1]) is not str
            or row[2] is not True
            or row[0] in relation_map
        ):
            return _state(0, "incompatible")
        relation_map[row[0]] = row[1]
    revision_hint = _revision_hint(connection, relation_map)
    expected_relations = {
        **{name: "r" for name in _TABLES},
        **{name: "i" for name in INDEX_SIGNATURES},
        **{name: "S" for name in SEQUENCES},
    }
    if not schema_secure or relation_map != expected_relations:
        return _state(revision_hint, "incompatible")
    if not _relation_acls_match(connection):
        return _state(revision_hint, "incompatible")

    if not _columns_match(connection):
        return _state(revision_hint, "incompatible")
    if not _constraints_match(connection):
        return _state(revision_hint, "incompatible")
    if not _indexes_match(connection):
        return _state(revision_hint, "incompatible")

    meta = _execute(
        connection,
        'SELECT "singleton","adapter_id","component","applied_revision","dirty" '
        'FROM "aipcs_registry"."aipcs_registry_meta"',
    ).fetchall()
    if len(meta) != 1 or len(meta[0]) != 5:
        return _state(0, "incompatible")
    singleton, adapter_id, component, revision, dirty = meta[0]
    if (
        type(singleton) is not int
        or singleton != 1
        or adapter_id != ADAPTER_ID
        or component != "registry"
        or type(revision) is not int
        or isinstance(revision, bool)
        or revision < 0
        or type(dirty) is not bool
    ):
        return _state(0, "incompatible")
    if dirty:
        return _state(revision, "dirty")
    if revision != TARGET_REVISION:
        status = "outdated" if 0 < revision < TARGET_REVISION else "incompatible"
        return _state(revision, status)

    history = _execute(
        connection,
        'SELECT "component","revision","migration_id","checksum","applied_at" '
        'FROM "aipcs_registry"."aipcs_registry_migration" ORDER BY "revision"',
    ).fetchall()
    if len(history) != 1 or len(history[0]) != 5:
        return _state(revision, "incompatible")
    h_component, h_revision, migration_id, checksum, applied_at = history[0]
    try:
        decode_time(_canonical_time(applied_at))
    except StorageMigrationError:
        return _state(revision, "incompatible")
    if (
        h_component != "registry"
        or h_revision != TARGET_REVISION
        or migration_id != MIGRATION_ID
        or checksum != CHECKSUM
    ):
        return _state(revision, "incompatible")

    try:
        _validate_registry_rows(connection)
    except StorageMigrationError:
        return _state(revision, "incompatible")
    return _state(revision, "ready")


def canonical_row(cursor: object, row: object) -> dict[str, Any]:
    """Detach one driver row and normalize native PostgreSQL registry values."""

    if isinstance(row, Mapping):
        source = dict(row)
    else:
        description = getattr(cursor, "description", None)
        if description is None or not isinstance(row, (tuple, list)):
            raise StorageMigrationError()
        names: list[str] = []
        for item in description:
            name = getattr(item, "name", None)
            if name is None and isinstance(item, (tuple, list)) and item:
                name = item[0]
            if type(name) is not str:
                raise StorageMigrationError()
            names.append(name)
        if len(names) != len(row):
            raise StorageMigrationError()
        source = dict(zip(names, row, strict=True))
    try:
        return {key: _canonical_value(value) for key, value in source.items()}
    except Exception:
        raise StorageMigrationError() from None


def canonical_rows(cursor: object, rows: object) -> list[dict[str, Any]]:
    if not isinstance(rows, (tuple, list)):
        raise StorageMigrationError()
    return [canonical_row(cursor, row) for row in rows]


def _columns_match(connection: object) -> bool:
    rows = _execute(
        connection,
        "SELECT c.relname,a.attname,"
        "pg_catalog.format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,"
        "a.atthasdef,COALESCE(coll.collname,'') "
        "FROM pg_catalog.pg_attribute AS a "
        "JOIN pg_catalog.pg_class AS c ON c.oid=a.attrelid "
        "JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace "
        "LEFT JOIN pg_catalog.pg_collation AS coll ON coll.oid=a.attcollation "
        "WHERE n.nspname=%s AND c.relkind IN ('r','p') "
        "AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum",
        (SCHEMA,),
    ).fetchall()
    actual: dict[str, list[tuple[object, ...]]] = {}
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 7 or type(row[0]) is not str:
            return False
        actual.setdefault(row[0], []).append(tuple(row[1:]))
    expected = {
        table: tuple((*column, "C" if column[1] == "text" else "") for column in columns)
        for table, columns in TABLE_COLUMNS.items()
    }
    return {name: tuple(values) for name, values in actual.items()} == expected


def _constraints_match(connection: object) -> bool:
    rows = _constraint_evidence(connection)
    names: set[str] = set()
    check_expressions: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 14:
            return False
        (
            table,
            name,
            kind,
            key_columns,
            reference_schema,
            reference_table,
            reference_columns,
            update_action,
            delete_action,
            match_type,
            deferrable,
            deferred,
            validated,
            expression,
        ) = row
        if (
            type(table) is not str
            or type(name) is not str
            or type(kind) is not str
            or name in names
            or type(deferrable) is not bool
            or deferrable
            or type(deferred) is not bool
            or deferred
            or validated is not True
        ):
            return False
        names.add(name)
        if kind == "c":
            tokens = CHECK_TOKENS.get(name)
            if (
                tokens is None
                or type(expression) is not str
                or not _contains_tokens(expression, tokens)
            ):
                return False
            check_expressions[name] = expression
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
            or reference_table != expected_reference
            or _catalog_columns(reference_columns) != expected_reference_keys
            or expression is not None
        ):
            return False
        if kind == "f":
            if (
                reference_schema != SCHEMA
                or update_action != "r"
                or delete_action != "r"
                or match_type != "s"
            ):
                return False
        elif reference_schema is not None:
            return False
    expected_names = {
        name for constraints in CONSTRAINT_TYPES.values() for name in constraints
    }
    expression_digest = hashlib.sha256(
        "\n".join(
            f"{name}\0{expression}"
            for name, expression in sorted(check_expressions.items())
        ).encode()
    ).hexdigest()
    return names == expected_names and expression_digest == CHECK_EXPRESSION_DIGEST


def _constraint_evidence(connection: object) -> list[tuple[object, ...]]:
    rows = _execute(
        connection,
        """SELECT c.relname,k.conname,k.contype,
        ARRAY(
            SELECT a.attname
            FROM unnest(k.conkey) WITH ORDINALITY AS key_column(attnum,ordinal)
            JOIN pg_catalog.pg_attribute AS a
              ON a.attrelid=k.conrelid AND a.attnum=key_column.attnum
            ORDER BY key_column.ordinal
        ),
        rn.nspname,rc.relname,
        ARRAY(
            SELECT a.attname
            FROM unnest(k.confkey) WITH ORDINALITY AS key_column(attnum,ordinal)
            JOIN pg_catalog.pg_attribute AS a
              ON a.attrelid=k.confrelid AND a.attnum=key_column.attnum
            ORDER BY key_column.ordinal
        ),
        k.confupdtype,k.confdeltype,k.confmatchtype,
        k.condeferrable,k.condeferred,k.convalidated,
        pg_catalog.pg_get_expr(k.conbin,k.conrelid,false) """
        "FROM pg_catalog.pg_constraint AS k "
        "JOIN pg_catalog.pg_class AS c ON c.oid=k.conrelid "
        "JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace "
        "LEFT JOIN pg_catalog.pg_class AS rc ON rc.oid=k.confrelid "
        "LEFT JOIN pg_catalog.pg_namespace AS rn ON rn.oid=rc.relnamespace "
        "WHERE n.nspname=%s AND k.contype<>'n' ORDER BY c.relname,k.conname",
        (SCHEMA,),
    ).fetchall()
    return [tuple(row) for row in rows]


def _indexes_match(connection: object) -> bool:
    rows = _execute(
        connection,
        """SELECT t.relname,i.relname,x.indisunique,x.indisprimary,
        (x.indpred IS NOT NULL),
        ARRAY(
            SELECT a.attname
            FROM unnest(x.indkey) WITH ORDINALITY AS key_column(attnum,ordinal)
            JOIN pg_catalog.pg_attribute AS a
              ON a.attrelid=x.indrelid AND a.attnum=key_column.attnum
            ORDER BY key_column.ordinal
        ),
        x.indoption::smallint[],
        pg_catalog.pg_get_expr(x.indpred,x.indrelid,false),
        am.amname,
        ARRAY(
            SELECT op.opcname
            FROM unnest(x.indclass) WITH ORDINALITY AS index_opclass(oid,ordinal)
            JOIN pg_catalog.pg_opclass AS op ON op.oid=index_opclass.oid
            ORDER BY index_opclass.ordinal
        ),
        ARRAY(
            SELECT COALESCE(coll.collname,'')
            FROM unnest(x.indcollation) WITH ORDINALITY AS index_collation(oid,ordinal)
            LEFT JOIN pg_catalog.pg_collation AS coll ON coll.oid=index_collation.oid
            ORDER BY index_collation.ordinal
        ) """
        "FROM pg_catalog.pg_index AS x "
        "JOIN pg_catalog.pg_class AS i ON i.oid=x.indexrelid "
        "JOIN pg_catalog.pg_class AS t ON t.oid=x.indrelid "
        "JOIN pg_catalog.pg_namespace AS n ON n.oid=t.relnamespace "
        "JOIN pg_catalog.pg_am AS am ON am.oid=i.relam "
        "WHERE n.nspname=%s ORDER BY i.relname",
        (SCHEMA,),
    ).fetchall()
    actual: dict[str, tuple[object, ...]] = {}
    for row in rows:
        if (
            not isinstance(row, (tuple, list))
            or len(row) != 11
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not bool
            or type(row[3]) is not bool
            or type(row[4]) is not bool
        ):
            return False
        if (
            _catalog_columns(row[5]) != INDEX_COLUMNS.get(row[1])
            or _catalog_integers(row[6]) != INDEX_OPTIONS.get(row[1])
            or row[8] != "btree"
            or _catalog_columns(row[9]) != INDEX_OPCLASSES.get(row[1])
            or _catalog_columns(row[10]) != INDEX_COLLATIONS.get(row[1])
        ):
            return False
        predicate_tokens = INDEX_PREDICATE_TOKENS.get(row[1])
        if predicate_tokens is None:
            if row[7] is not None:
                return False
        elif (
            type(row[7]) is not str
            or row[7] != INDEX_PREDICATES.get(row[1])
            or not _contains_tokens(row[7], predicate_tokens)
        ):
            return False
        actual[row[1]] = (row[0], row[2], row[3], row[4])
    return actual == INDEX_SIGNATURES


def _relation_acls_match(connection: object) -> bool:
    rows = _execute(
        connection,
        """SELECT c.relname,
        (c.relowner=(SELECT r.oid FROM pg_catalog.pg_roles AS r
                     WHERE r.rolname=CURRENT_USER)),
        EXISTS(
            SELECT 1 FROM pg_catalog.aclexplode(
                COALESCE(
                    c.relacl,
                    pg_catalog.acldefault(
                        CASE WHEN c.relkind='S' THEN 's'::"char" ELSE 'r'::"char" END,
                        c.relowner
                    )
                )
            ) AS a WHERE a.grantee=0
        )
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace
        WHERE n.nspname=%s AND c.relkind IN ('r','p','S')
        ORDER BY c.relname""",
        (SCHEMA,),
    ).fetchall()
    expected = _TABLES | SEQUENCES
    if len(rows) != len(expected):
        return False
    names: set[str] = set()
    for row in rows:
        if (
            not isinstance(row, (tuple, list))
            or len(row) != 3
            or type(row[0]) is not str
            or row[0] in names
            or row[1] is not True
            or row[2] is not False
        ):
            return False
        names.add(row[0])
    return names == expected


def _revision_hint(connection: object, relations: Mapping[str, str]) -> int:
    if relations.get("aipcs_registry_meta") != "r":
        return 0
    try:
        row = _execute(
            connection,
            'SELECT "applied_revision" FROM '
            '"aipcs_registry"."aipcs_registry_meta" WHERE "singleton"=1',
        ).fetchone()
    except Exception:
        return 0
    if (
        isinstance(row, (tuple, list))
        and len(row) == 1
        and type(row[0]) is int
        and row[0] >= 0
    ):
        return row[0]
    return 0


def _catalog_columns(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, (tuple, list)) or not all(type(item) is str for item in value):
        return None
    return tuple(value)


def _catalog_integers(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, (tuple, list)) or not all(type(item) is int for item in value):
        return None
    return tuple(value)


def _contains_tokens(expression: str, tokens: tuple[str, ...]) -> bool:
    normalised = expression.casefold().replace('"', "")
    return all(token.casefold().replace('"', "") in normalised for token in tokens)


def _validate_registry_rows(connection: object) -> None:
    service_values: dict[tuple[str, str], Service] = {}
    services = _execute(
        connection,
        'SELECT * FROM "aipcs_registry"."aipcs_registry_service" '
        'ORDER BY "principal_id","service_id"',
    )
    for row in canonical_rows(services, services.fetchall()):
        service = decode_service(row)
        service_values[(service.principal_id, str(service.service_id))] = service

    mutations = _execute(
        connection,
        'SELECT * FROM "aipcs_registry"."aipcs_registry_mutation" '
        'ORDER BY "principal_id","idempotency_key"',
    )
    for row in canonical_rows(mutations, mutations.fetchall()):
        intent, completion = validate_r2_mutation_row(row)
        if intent is not None:
            current = service_values.get((intent.principal_id, str(intent.service_id)))
            if (
                current is None
                or current.schema_version is None
                or intent.expected_service_revision > current.service_revision
                or intent.expected_schema_version > current.schema_version
            ):
                raise StorageMigrationError()
        if completion is not None:
            current = service_values.get(
                (completion.principal_id, str(completion.service_id))
            )
            if (
                current is None
                or _immutable_service_identity(completion)
                != _immutable_service_identity(current)
                or completion.service_revision > current.service_revision
                or completion.updated_at > current.updated_at
                or (
                    completion.schema_version is not None
                    and (
                        current.schema_version is None
                        or completion.schema_version > current.schema_version
                    )
                )
                or (
                    row["operation_kind"] != "legacy"
                    and completion.service_revision == current.service_revision
                    and completion != current
                )
                or (
                    row["operation_kind"] in {"materialise", "evolve"}
                    and (completion.materialised_at, completion.storage)
                    != (current.materialised_at, current.storage)
                )
            ):
                raise StorageMigrationError()

    audits = _execute(
        connection,
        'SELECT * FROM "aipcs_registry"."aipcs_registry_audit" '
        'ORDER BY "principal_id","audit_id"',
    )
    for row in canonical_rows(audits, audits.fetchall()):
        validate_audit_row(row)
    over_cap = _execute(
        connection,
        'SELECT 1 FROM "aipcs_registry"."aipcs_registry_audit" '
        'GROUP BY "principal_id" HAVING count(*)>1000 LIMIT 1',
    ).fetchone()
    if over_cap is not None:
        raise StorageMigrationError()


def _immutable_service_identity(service: Service) -> tuple[object, ...]:
    return (
        service.principal_id,
        service.service_id,
        service.domain_name,
        service.domain_class,
        service.intent_description,
        service.created_at,
    )


def _canonical_value(value: object) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _canonical_time(value)
    if type(value) in {dict, list}:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    return value


def _canonical_time(value: object) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise StorageMigrationError()
    return encode_time(value.astimezone(UTC).replace(tzinfo=UTC))


def _execute(connection: object, sql: str, params: tuple[object, ...] = ()) -> Any:
    execute = getattr(connection, "execute", None)
    if not callable(execute):
        raise StorageMigrationError()
    return execute(sql, params)


def _state(applied_revision: int, status: str) -> MigrationState:
    return MigrationState("registry", applied_revision, TARGET_REVISION, status)  # type: ignore[arg-type]
