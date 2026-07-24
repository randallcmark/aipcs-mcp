"""Read-only structured PostgreSQL registry inspection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from aipcs_mcp.application.models import Service
from aipcs_mcp.application.registry_authority import (
    ReceiptKind,
    TransferReceipt,
)
from aipcs_mcp.storage.codecs import (
    decode_result,
    decode_service,
    decode_time,
    encode_time,
    validate_r2_mutation_row,
)
from aipcs_mcp.storage.contracts import MigrationState
from aipcs_mcp.storage.errors import StorageMigrationError, StorageUnavailable
from aipcs_mcp.storage.registry_authority_codecs import (
    validate_registry_authority_claim_row,
    validate_transfer_receipt_row,
)

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
    R1_CHECK_EXPRESSION_DIGEST,
    R1_CHECK_TOKENS,
    R1_CHECKSUM,
    R1_CONSTRAINT_KEYS,
    R1_CONSTRAINT_TYPES,
    R1_INDEX_COLLATIONS,
    R1_INDEX_COLUMNS,
    R1_INDEX_OPCLASSES,
    R1_INDEX_OPTIONS,
    R1_INDEX_PREDICATE_TOKENS,
    R1_INDEX_PREDICATES,
    R1_INDEX_SIGNATURES,
    R1_MIGRATION_ID,
    R1_SEQUENCES,
    R1_TABLE_COLUMNS,
    SCHEMA,
    SEQUENCES,
    TABLE_COLUMNS,
    TARGET_REVISION,
)

_TABLES = frozenset(TABLE_COLUMNS)


def _expectations(revision: int) -> tuple[object, ...] | None:
    if revision == 1:
        return (
            R1_TABLE_COLUMNS,
            R1_CONSTRAINT_TYPES,
            R1_CONSTRAINT_KEYS,
            R1_INDEX_SIGNATURES,
            R1_SEQUENCES,
            R1_INDEX_COLUMNS,
            R1_INDEX_OPTIONS,
            R1_INDEX_OPCLASSES,
            R1_INDEX_COLLATIONS,
            R1_CHECK_TOKENS,
            R1_INDEX_PREDICATE_TOKENS,
            R1_INDEX_PREDICATES,
            R1_CHECK_EXPRESSION_DIGEST,
            R1_MIGRATION_ID,
            R1_CHECKSUM,
        )
    if revision == TARGET_REVISION:
        return (
            TABLE_COLUMNS,
            CONSTRAINT_TYPES,
            CONSTRAINT_KEYS,
            INDEX_SIGNATURES,
            SEQUENCES,
            INDEX_COLUMNS,
            INDEX_OPTIONS,
            INDEX_OPCLASSES,
            INDEX_COLLATIONS,
            CHECK_TOKENS,
            INDEX_PREDICATE_TOKENS,
            INDEX_PREDICATES,
            CHECK_EXPRESSION_DIGEST,
            MIGRATION_ID,
            CHECKSUM,
        )
    return None


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
    expectations = _expectations(revision_hint)
    if expectations is None:
        return _state(revision_hint, "incompatible")
    (
        table_columns,
        constraint_types,
        constraint_keys,
        index_signatures,
        sequences,
        index_columns,
        index_options,
        index_opclasses,
        index_collations,
        check_tokens,
        index_predicate_tokens,
        index_predicates,
        check_expression_digest,
        _migration_id,
        _checksum,
    ) = expectations
    expected_relations = {
        **{name: "r" for name in table_columns},
        **{name: "i" for name in index_signatures},
        **{name: "S" for name in sequences},
    }
    if not schema_secure or relation_map != expected_relations:
        return _state(revision_hint, "incompatible")
    if not _relation_acls_match(connection, frozenset(table_columns), sequences):
        return _state(revision_hint, "incompatible")

    if not _columns_match(connection, table_columns):
        return _state(revision_hint, "incompatible")
    if not _constraints_match(
        connection,
        constraint_types,
        constraint_keys,
        check_tokens,
        check_expression_digest,
    ):
        return _state(revision_hint, "incompatible")
    if not _indexes_match(
        connection,
        index_signatures,
        index_columns,
        index_options,
        index_opclasses,
        index_collations,
        index_predicate_tokens,
        index_predicates,
    ):
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
    if revision != revision_hint:
        return _state(revision, "incompatible")

    history = _execute(
        connection,
        'SELECT "component","revision","migration_id","checksum","applied_at" '
        'FROM "aipcs_registry"."aipcs_registry_migration" ORDER BY "revision"',
    ).fetchall()
    expected_history = 1 if revision == 1 else TARGET_REVISION
    if len(history) != expected_history or any(len(row) != 5 for row in history):
        return _state(revision, "incompatible")
    expected_history_rows = ((1, R1_MIGRATION_ID, R1_CHECKSUM),)
    if revision == TARGET_REVISION:
        expected_history_rows += ((TARGET_REVISION, MIGRATION_ID, CHECKSUM),)
    for row, (expected_revision, expected_migration_id, expected_checksum) in zip(
        history, expected_history_rows, strict=True
    ):
        h_component, h_revision, migration_id, checksum, applied_at = row
        try:
            decode_time(_canonical_time(applied_at))
        except StorageMigrationError:
            return _state(revision, "incompatible")
        if (
            h_component != "registry"
            or h_revision != expected_revision
            or migration_id != expected_migration_id
            or checksum != expected_checksum
        ):
            return _state(revision, "incompatible")

    if revision != TARGET_REVISION:
        return _state(revision, "outdated")

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


def _columns_match(
    connection: object, table_columns: Mapping[str, tuple[tuple[object, ...], ...]]
) -> bool:
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
        for table, columns in table_columns.items()
    }
    return {name: tuple(values) for name, values in actual.items()} == expected


def _constraints_match(
    connection: object,
    constraint_types: Mapping[str, Mapping[str, str]],
    constraint_keys: Mapping[str, tuple[object, ...]],
    check_tokens: Mapping[str, tuple[str, ...]],
    check_expression_digest: str,
) -> bool:
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
            tokens = check_tokens.get(name)
            if (
                tokens is None
                or type(expression) is not str
                or not _contains_tokens(expression, tokens)
            ):
                return False
            check_expressions[name] = expression
            continue
        expected = constraint_keys.get(name)
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
        name for constraints in constraint_types.values() for name in constraints
    }
    if names != expected_names:
        return False
    expression_digest = hashlib.sha256(
        "\n".join(
            f"{name}\0{expression}"
            for name, expression in sorted(check_expressions.items())
        ).encode()
    ).hexdigest()
    return expression_digest == check_expression_digest


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


def _indexes_match(
    connection: object,
    index_signatures: Mapping[str, tuple[str, bool, bool, bool]],
    index_columns: Mapping[str, tuple[str, ...]],
    index_options: Mapping[str, tuple[int, ...]],
    index_opclasses: Mapping[str, tuple[str, ...]],
    index_collations: Mapping[str, tuple[str, ...]],
    index_predicate_tokens: Mapping[str, tuple[str, ...]],
    index_predicates: Mapping[str, str],
) -> bool:
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
            _catalog_columns(row[5]) != index_columns.get(row[1])
            or _catalog_integers(row[6]) != index_options.get(row[1])
            or row[8] != "btree"
            or _catalog_columns(row[9]) != index_opclasses.get(row[1])
            or _catalog_columns(row[10]) != index_collations.get(row[1])
        ):
            return False
        predicate_tokens = index_predicate_tokens.get(row[1])
        if predicate_tokens is None:
            if row[7] is not None:
                return False
        elif (
            type(row[7]) is not str
            or row[7] != index_predicates.get(row[1])
            or not _contains_tokens(row[7], predicate_tokens)
        ):
            return False
        actual[row[1]] = (row[0], row[2], row[3], row[4])
    return actual == index_signatures


def _relation_acls_match(
    connection: object, tables: frozenset[str], sequences: frozenset[str]
) -> bool:
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
    expected = tables | sequences
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
    for token in tokens:
        expected = token.casefold().replace('"', "")
        if expected in normalised:
            continue
        if expected.startswith((">= ", "<= ")):
            bound = expected[3:]
            if f"between {bound} and" in normalised or f"and {bound}" in normalised:
                continue
        return False
    return True


def _validate_registry_rows(connection: object) -> None:
    service_values: dict[str, Service] = {}
    services = _execute(
        connection,
        'SELECT * FROM "aipcs_registry"."aipcs_registry_service" '
        'ORDER BY "principal_id","service_id"',
    )
    for row in canonical_rows(services, services.fetchall()):
        service = decode_service(row)
        service_values[str(service.service_id)] = service

    identities = _execute(
        connection,
        'SELECT * FROM "aipcs_registry"."aipcs_registry_identity" '
        'ORDER BY "service_id"',
    )
    identities_by_service: dict[str, dict[str, Any]] = {}
    for row in canonical_rows(identities, identities.fetchall()):
        service_id = row.get("service_id")
        state = row.get("identity_state")
        principal_id = row.get("principal_id")
        namespace = row.get("storage_namespace")
        if (
            type(service_id) is not str
            or type(state) is not str
            or type(principal_id) is not str
            or type(namespace) is not str
            or service_id in identities_by_service
        ):
            raise StorageMigrationError()
        if not _valid_identity_basics(row):
            raise StorageMigrationError()
        if state == "live":
            service = service_values.get(service_id)
            if (
                service is None
                or service.principal_id != principal_id
                or service.domain_name != row.get("domain_name")
                or row.get("claim_idempotency_key") is not None
                or row.get("lifecycle_at") is not None
                or row.get("storage_backend")
                != (None if service.storage is None else service.storage.backend)
                or row.get("storage_namespace")
                != f"svc_{service.service_id.hex}"
            ):
                raise StorageMigrationError()
        elif state == "import_prepared":
            if service_id in service_values:
                raise StorageMigrationError()
        elif state == "tombstoned":
            if service_id in service_values or row.get("domain_name") is not None:
                raise StorageMigrationError()
        else:
            raise StorageMigrationError()
        identities_by_service[service_id] = row
    if set(service_values) != {
        service_id
        for service_id, row in identities_by_service.items()
        if row["identity_state"] == "live"
    }:
        raise StorageMigrationError()

    claims = _execute(
        connection,
        'SELECT * FROM "aipcs_registry"."aipcs_registry_claim" '
        'ORDER BY "principal_id","idempotency_key"',
    )
    claims_by_key: dict[tuple[str, str], tuple[dict[str, Any], object]] = {}
    for row in canonical_rows(claims, claims.fetchall()):
        claim = _validated_claim(row)
        if row["operation_kind"] in {"legacy", "seed", "design", "materialise", "evolve"}:
            intent, completion = validate_r2_mutation_row(_legacy_claim_row(row))
            if intent is not None:
                current = service_values.get(str(intent.service_id))
                if (
                    current is None
                    or current.schema_version is None
                    or intent.expected_service_revision > current.service_revision
                    or intent.expected_schema_version > current.schema_version
                ):
                    raise StorageMigrationError()
            if completion is not None:
                current = service_values.get(str(completion.service_id))
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
        key = (row["principal_id"], row["idempotency_key"])
        if key in claims_by_key:
            raise StorageMigrationError()
        if type(claim) is Service:
            current = service_values.get(str(claim.service_id))
            if (
                current is None
                or claim.principal_id != current.principal_id
                or claim.domain_name != current.domain_name
            ):
                raise StorageMigrationError()
        claims_by_key[key] = (row, claim)

    _validate_identity_claim_pairings(identities_by_service, claims_by_key)

    receipts = _execute(
        connection,
        'SELECT * FROM "aipcs_registry"."aipcs_registry_receipt" '
        'ORDER BY "service_id","bundle_root_sha256","receipt_kind"',
    )
    for row in canonical_rows(receipts, receipts.fetchall()):
        if not _valid_receipt_row(row, claims_by_key, service_values):
            raise StorageMigrationError()

    audits = _execute(
        connection,
        'SELECT * FROM "aipcs_registry"."aipcs_registry_audit" '
        'ORDER BY "principal_id","audit_id"',
    )
    for row in canonical_rows(audits, audits.fetchall()):
        if not _valid_audit_row(row):
            raise StorageMigrationError()
    over_cap = _execute(
        connection,
        'SELECT 1 FROM "aipcs_registry"."aipcs_registry_audit" '
        'GROUP BY "principal_id" HAVING count(*)>1000 LIMIT 1',
    ).fetchone()
    if over_cap is not None:
        raise StorageMigrationError()


def _validated_claim(row: Mapping[str, Any]) -> object:
    phase = row.get("phase")
    intent = row.get("intent_json")
    result = row.get("result_json")
    recovery = row.get("recovery_category")
    if (
        type(row.get("principal_id")) is not str
        or type(row.get("idempotency_key")) is not str
        or type(row.get("fingerprint")) is not str
        or type(row.get("service_id")) is not str
        or type(row.get("operation_kind")) is not str
        or phase not in {"prepared", "completed", "recovery_required", "tombstoned"}
    ):
        raise StorageMigrationError()
    if len(row["fingerprint"]) != 64 or any(
        char not in "0123456789abcdef" for char in row["fingerprint"]
    ):
        raise StorageMigrationError()
    operation_kind = row["operation_kind"]
    if phase == "tombstoned":
        if intent is not None or result is not None or recovery is not None:
            raise StorageMigrationError()
        if operation_kind == "purge":
            raise StorageMigrationError()
        return row
    if operation_kind in {"suspend", "resume", "archive", "restore", "export", "import", "purge"}:
        return _validated_authority_claim(row)
    if operation_kind not in {"legacy", "seed", "design", "materialise", "evolve"}:
        raise StorageMigrationError()
    if operation_kind in {"legacy", "seed", "design"} and phase != "completed":
        raise StorageMigrationError()
    if type(intent) is not str or not _is_json_object(intent):
        if operation_kind not in {"legacy", "seed", "design"}:
            raise StorageMigrationError()
    if phase == "completed":
        if type(result) is not str or not _is_json_object(result) or recovery is not None:
            raise StorageMigrationError()
        return decode_result(result, row["principal_id"], row["service_id"])
    if result is not None or (
        recovery is not None if phase == "prepared" else recovery != "recovery_required"
    ):
        raise StorageMigrationError()
    return row


def _immutable_service_identity(service: Service) -> tuple[object, ...]:
    return (
        service.principal_id,
        service.service_id,
        service.domain_name,
        service.domain_class,
        service.intent_description,
        service.created_at,
    )


def _validated_authority_claim(row: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the frozen physical claim columns without opening a UOW."""

    try:
        intent = json.loads(row["intent_json"])
        if type(intent) is not dict:
            raise ValueError
        value = validate_registry_authority_claim_row(row)
    except (TypeError, ValueError, json.JSONDecodeError, StorageMigrationError):
        raise StorageMigrationError() from None
    return {"intent": intent, "result_json": row["result_json"], "value": value}


def _legacy_claim_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project one R2 shared claim back through the frozen R1 mutation validator."""

    result = dict(row)
    operation_kind = row["operation_kind"]
    if operation_kind in {"legacy", "seed", "design"}:
        result.update(
            expected_service_revision=None,
            expected_schema_version=None,
            target_manifest_json=None,
        )
        return result
    try:
        intent = json.loads(row["intent_json"])
        if type(intent) is not dict or set(intent) != {
            "created_via",
            "expected_schema_version",
            "expected_service_revision",
            "kind",
            "principal",
            "service_id",
            "target_manifest",
        }:
            raise ValueError
        if (
            intent["created_via"] != row["created_via"]
            or intent["kind"] != operation_kind
            or intent["principal"] != row["principal_id"]
            or intent["service_id"] != row["service_id"]
        ):
            raise ValueError
        result.update(
            expected_service_revision=intent["expected_service_revision"],
            expected_schema_version=intent["expected_schema_version"],
            target_manifest_json=json.dumps(
                intent["target_manifest"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ),
        )
        return result
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise StorageMigrationError() from None


def _valid_identity_basics(row: Mapping[str, Any]) -> bool:
    try:
        service_id = UUID(row["service_id"])
        if service_id.int == 0 or row["storage_namespace"] != f"svc_{service_id.hex}":
            return False
        if row["identity_state"] not in {"live", "import_prepared", "tombstoned"}:
            return False
        if row["storage_backend"] not in {None, "sqlite", "postgresql"}:
            return False
        lifecycle_at = row["lifecycle_at"]
        if lifecycle_at is not None:
            decode_time(lifecycle_at)
        return True
    except (KeyError, TypeError, ValueError, StorageMigrationError):
        return False


def _validate_identity_claim_pairings(
    identities: Mapping[str, Mapping[str, Any]],
    claims: Mapping[tuple[str, str], tuple[Mapping[str, Any], object]],
) -> None:
    for service_id, identity in identities.items():
        state = identity["identity_state"]
        key = identity["claim_idempotency_key"]
        if state == "live":
            continue
        if type(key) is not str:
            raise StorageMigrationError()
        pairing = claims.get((identity["principal_id"], key))
        if pairing is None:
            raise StorageMigrationError()
        claim_row, claim = pairing
        if claim_row["service_id"] != service_id:
            raise StorageMigrationError()
        if state == "import_prepared":
            if claim_row["operation_kind"] != "import" or claim_row["phase"] not in {
                "prepared",
                "recovery_required",
            }:
                raise StorageMigrationError()
            intent = claim.get("intent") if isinstance(claim, dict) else None
            imported_identity = None if not isinstance(intent, dict) else intent.get("identity")
            if (
                not isinstance(imported_identity, dict)
                or identity["domain_name"] != imported_identity.get("domain_name")
                or identity["storage_namespace"] != imported_identity.get("logical_namespace")
                or identity["storage_backend"] != intent.get("destination_backend")
            ):
                raise StorageMigrationError()
        elif state == "tombstoned":
            if claim_row["operation_kind"] != "purge" or claim_row["phase"] != "completed":
                raise StorageMigrationError()
        else:
            raise StorageMigrationError()


def _valid_receipt_row(
    row: Mapping[str, Any],
    claims: Mapping[tuple[str, str], tuple[Mapping[str, Any], object]],
    services: Mapping[str, Service],
) -> bool:
    try:
        receipt = validate_transfer_receipt_row(row)
        claim_row, claim = claims[(receipt.principal_id, row["idempotency_key"])]
        return (
            claim_row["service_id"] == str(receipt.service_id)
            and claim_row["operation_kind"] == receipt.kind.value
            and claim_row["phase"] == "completed"
            and _receipt_matches_completion(receipt, claim, services)
        )
    except (KeyError, TypeError, ValueError, StorageMigrationError):
        return False


def _receipt_matches_completion(
    receipt: TransferReceipt,
    claim: object,
    services: Mapping[str, Service],
) -> bool:
    if not isinstance(claim, dict):
        return False
    value = claim.get("value")
    if receipt.kind is ReceiptKind.EXPORT:
        return value == receipt
    if receipt.kind is not ReceiptKind.IMPORT or type(value) is not Service:
        return False
    intent = claim.get("intent")
    if type(intent) is not dict:
        return False
    registered = services.get(str(receipt.service_id))
    return (
        intent.get("type") == "portable"
        and intent.get("kind") == "import"
        and intent.get("bundle_root_sha256") == receipt.bundle_root_sha256
        and intent.get("destination_backend") == receipt.storage_backend.value
        and registered is not None
        and _immutable_service_identity(value)
        == _immutable_service_identity(registered)
        and value.service_revision <= registered.service_revision
        and value.updated_at <= registered.updated_at
        and (
            value.service_revision != registered.service_revision
            or value == registered
        )
        and (value.materialised_at, value.storage)
        == (registered.materialised_at, registered.storage)
        and value.principal_id == receipt.principal_id
        and value.service_id == receipt.service_id
        and value.service_revision == receipt.service_revision
        and value.operational_status == receipt.operational_status
        and (
            value.storage is None
            or value.storage.backend == receipt.storage_backend.value
        )
    )


def _valid_audit_row(row: Mapping[str, Any]) -> bool:
    return all(
        type(row.get(name)) is str
        for name in ("action", "outcome", "service_id", "principal_id", "created_via", "at")
    )


def _is_json_object(value: str) -> bool:
    try:
        return type(json.loads(value)) is dict
    except (TypeError, ValueError):
        return False


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
