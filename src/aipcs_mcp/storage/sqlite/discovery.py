"""Read-only SQLite discovery and mechanical maintenance queries.

The caller owns connection acquisition and exact R3 readiness checks.  These
helpers accept only a ready connection plus pure record-contract values, issue
bounded ``SELECT`` statements, and return detached pure values.  They never
create, migrate, repair, or mutate storage.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from aipcs_mcp.numeric_domain import is_exact_number, is_signed_64_integer
from aipcs_mcp.records import (
    MAX_FACET_VALUES,
    MAX_SUMMARY_BRANCHES,
    BranchCard,
    BranchValue,
    FacetObservation,
    FacetValue,
    FrozenJsonObject,
    MaintenanceCandidate,
    MaintenanceQuery,
    MaintenanceResult,
    RecordContractError,
    RecordEntity,
    RecordField,
    RecordSpecification,
    RecordValue,
    ServiceSummary,
    SummaryQuery,
)
from aipcs_mcp.storage.errors import StorageUnavailable

from .codecs import decode_time, encode_time
from .service_store_migrations import R3_BRANCH_TABLE, R3_RECORD_BRANCH_TABLE

_AUTHORITY_FIELDS = frozenset(
    {
        "provenance_type",
        "provenance_note",
        "provenance_source",
        "source_ref",
        "source_kind",
        "scope",
        "scope_ref",
        "confidence",
        "status",
        "valid_from",
        "valid_until",
        "supersedes",
        "superseded_by",
    }
)
_DUPLICATE_AUTHORITY_FIELDS = ("provenance_source", "source_ref")
_BLOB_THRESHOLD_BYTES = 1_024


def service_summary(
    connection: sqlite3.Connection,
    principal_id: str,
    specification: RecordSpecification,
    query: SummaryQuery,
) -> ServiceSummary:
    """Read one principal-scoped, deterministic service summary."""

    _require_inputs(connection, principal_id, specification)
    if type(query) is not SummaryQuery:
        raise RecordContractError()
    requested_samples = dict(query.sample_sizes)
    if set(requested_samples) - {entity.name for entity in specification.entities}:
        raise RecordContractError()
    try:
        counts = tuple(
            (
                entity.name,
                _scalar_count(
                    connection,
                    f'SELECT count(*) FROM {_quote(entity.name)} WHERE "owner_id"=?',
                    (principal_id,),
                ),
            )
            for entity in specification.entities
        )
        facets = tuple(
            _facet_observation(
                connection, principal_id, specification, facet.entity_name, facet.field_name
            )
            for facet in specification.discovery_facets
            if (
                (entity := specification.entity(facet.entity_name)) is not None
                and any(field.name == facet.field_name for field in entity.domain_fields)
            )
        )
        branches = _branch_cards(connection, principal_id)
        unbranched = sum(
            _scalar_count(
                connection,
                f"SELECT count(*) FROM {_quote(entity.name)} AS e "
                f'WHERE e."owner_id"=? AND NOT EXISTS ('
                f'SELECT 1 FROM "{R3_RECORD_BRANCH_TABLE}" AS rb '
                'WHERE rb."principal_id"=? AND rb."entity_name"=? '
                'AND rb."record_id"=e."id" AND rb."role"=\'primary\')',
                (principal_id, principal_id, entity.name),
            )
            for entity in specification.entities
        )
        samples = tuple(
            (
                entity_name,
                _sample_records(
                    connection,
                    principal_id,
                    specification,
                    entity_name,
                    requested_samples[entity_name],
                ),
            )
            for entity_name in sorted(requested_samples)
        )
        return ServiceSummary("ready", counts, unbranched, facets, branches, samples)
    except (RecordContractError, StorageUnavailable):
        raise
    except Exception:
        raise StorageUnavailable() from None


def maintenance_scan(
    connection: sqlite3.Connection,
    principal_id: str,
    specification: RecordSpecification,
    query: MaintenanceQuery,
    *,
    now: datetime,
) -> MaintenanceResult:
    """Return bounded mechanical candidates without ranking truth or mutating data."""

    _require_inputs(connection, principal_id, specification)
    if type(query) is not MaintenanceQuery or not _utc_datetime(now):
        raise RecordContractError()
    selected = (
        specification.entities
        if query.entity_name is None
        else tuple(entity for entity in specification.entities if entity.name == query.entity_name)
    )
    if not selected:
        raise RecordContractError()
    availability = {
        scan_type: any(_scan_available(scan_type, entity) for entity in selected)
        for scan_type in query.scan_types
    }
    unavailable = tuple(sorted(name for name, available in availability.items() if not available))
    candidates: list[MaintenanceCandidate] = []
    try:
        for scan_type in sorted(query.scan_types):
            if not availability[scan_type]:
                continue
            for entity in selected:
                if not _scan_available(scan_type, entity):
                    continue
                remaining = query.max_candidates - len(candidates)
                if remaining == 0:
                    return MaintenanceResult(tuple(candidates), unavailable)
                candidates.extend(
                    _scan_entity(
                        connection,
                        principal_id,
                        entity,
                        scan_type,
                        query,
                        now,
                        remaining,
                    )
                )
        return MaintenanceResult(tuple(candidates), unavailable)
    except (RecordContractError, StorageUnavailable):
        raise
    except Exception:
        raise StorageUnavailable() from None


def _facet_observation(
    connection: sqlite3.Connection,
    principal_id: str,
    specification: RecordSpecification,
    entity_name: str,
    field_name: str,
) -> FacetObservation:
    field = specification.field(entity_name, field_name)
    if field is None:
        raise StorageUnavailable()
    entity_sql, field_sql = _quote(entity_name), _quote(field_name)
    if field.filter_mode == "membership":
        rows = connection.execute(
            f"SELECT members.value,count(*) FROM {entity_sql} AS e,"
            f"json_each(e.{field_sql}) AS members "
            "WHERE e.\"owner_id\"=? AND members.type='text' "
            "GROUP BY members.value ORDER BY count(*) DESC,members.value ASC LIMIT ?",
            (principal_id, MAX_FACET_VALUES),
        ).fetchall()
        values = tuple(FacetValue(_text(value), _count(count)) for value, count in rows)
    else:
        rows = connection.execute(
            f"SELECT {field_sql},count(*) FROM {entity_sql} "
            f'WHERE "owner_id"=? AND {field_sql} IS NOT NULL '
            f"GROUP BY {field_sql} ORDER BY count(*) DESC,{field_sql} ASC LIMIT ?",
            (principal_id, MAX_FACET_VALUES),
        ).fetchall()
        values = tuple(
            FacetValue(_decode_field(field, value), _count(count)) for value, count in rows
        )
    return FacetObservation(entity_name, field_name, values)


def _branch_cards(connection: sqlite3.Connection, principal_id: str) -> tuple[BranchCard, ...]:
    rows = connection.execute(
        f'SELECT b."id",b."slug",b."title",b."intent",b."branch_type",'
        'b."parent_branch_id",b."status",b."retrieval_summary",b."branch_revision",'
        'b."created_at",b."updated_at",'
        "sum(CASE WHEN rb.\"role\"='primary' THEN 1 ELSE 0 END),"
        "sum(CASE WHEN rb.\"role\"='related' THEN 1 ELSE 0 END) "
        f'FROM "{R3_BRANCH_TABLE}" AS b LEFT JOIN "{R3_RECORD_BRANCH_TABLE}" AS rb '
        'ON rb."branch_id"=b."id" AND rb."principal_id"=? '
        'WHERE b."principal_id"=? GROUP BY b."id" '
        'ORDER BY b."updated_at" DESC,b."id" ASC LIMIT ?',
        (principal_id, principal_id, MAX_SUMMARY_BRANCHES),
    ).fetchall()
    return tuple(
        BranchCard(
            BranchValue(
                _uuid(row[0]),
                _text(row[1]),
                _text(row[2]),
                _text(row[3]),
                _optional_text(row[4]),
                _optional_uuid(row[5]),
                _text(row[6]),  # type: ignore[arg-type]
                _optional_text(row[7]),
                decode_time(row[9]),
                decode_time(row[10]),
                _revision(row[8]),
            ),
            _count(row[11]),
            _count(row[12]),
        )
        for row in rows
    )


def _sample_records(
    connection: sqlite3.Connection,
    principal_id: str,
    specification: RecordSpecification,
    entity_name: str,
    size: int,
) -> tuple[RecordValue, ...]:
    if size == 0:
        return ()
    cursor = connection.execute(
        f'SELECT * FROM {_quote(entity_name)} WHERE "owner_id"=? ORDER BY "id" ASC LIMIT ?',
        (principal_id, size),
    )
    names = tuple(item[0] for item in cursor.description or ())
    return tuple(
        _decode_record(dict(zip(names, row, strict=True)), principal_id, specification, entity_name)
        for row in cursor.fetchall()
    )


def _scan_available(scan_type: str, entity: RecordEntity) -> bool:
    fields = {field.name: field for field in entity.domain_fields}
    if scan_type in {"stale", "unbranched"}:
        return True
    if scan_type == "expired":
        return (field := fields.get("valid_until")) is not None and field.logical_type == "datetime"
    if scan_type == "low_confidence":
        return (field := fields.get("confidence")) is not None and field.logical_type in {
            "integer",
            "number",
        }
    if scan_type == "superseded":
        return (
            (field := fields.get("status")) is not None
            and field.logical_type == "string"
            or (field := fields.get("superseded_by")) is not None
            and field.logical_type in {"string", "uuid", "string_list"}
        )
    if scan_type == "missing_authority":
        return bool(_authority_fields(entity))
    if scan_type == "duplicate_authority":
        return bool(_duplicate_fields(entity))
    if scan_type == "blob_candidate":
        return any(
            field.logical_type == "string" and field.retrieval_mode == "annotation"
            for field in entity.domain_fields
        )
    raise RecordContractError()


def _scan_entity(
    connection: sqlite3.Connection,
    principal_id: str,
    entity: RecordEntity,
    scan_type: str,
    query: MaintenanceQuery,
    now: datetime,
    limit: int,
) -> list[MaintenanceCandidate]:
    if scan_type == "expired":
        return _simple_candidates(
            connection,
            principal_id,
            entity,
            query.branch_id,
            scan_type,
            'julianday(e."valid_until") < julianday(?)',
            (encode_time(now),),
            limit,
            lambda _row: {"field_name": "valid_until"},
        )
    if scan_type == "stale":
        cutoff = now - timedelta(days=query.stale_after_days)
        return _simple_candidates(
            connection,
            principal_id,
            entity,
            query.branch_id,
            scan_type,
            'e."updated_at" <= ?',
            (encode_time(cutoff),),
            limit,
            lambda row: {
                "age_days": max(0, (now - decode_time(row["value_0"])).days),
                "threshold_days": query.stale_after_days,
            },
            ('e."updated_at"',),
        )
    if scan_type == "low_confidence":
        return _simple_candidates(
            connection,
            principal_id,
            entity,
            query.branch_id,
            scan_type,
            'e."confidence" < ?',
            (float(query.low_confidence_below),),
            limit,
            lambda _row: {"threshold": float(query.low_confidence_below)},
        )
    if scan_type == "superseded":
        return _superseded_candidates(connection, principal_id, entity, query.branch_id, limit)
    if scan_type == "missing_authority":
        return _missing_authority_candidates(
            connection, principal_id, entity, query.branch_id, limit
        )
    if scan_type == "unbranched":
        return _simple_candidates(
            connection,
            principal_id,
            entity,
            query.branch_id,
            scan_type,
            f'NOT EXISTS (SELECT 1 FROM "{R3_RECORD_BRANCH_TABLE}" AS primary_branch '
            'WHERE primary_branch."principal_id"=? '
            'AND primary_branch."entity_name"=? '
            'AND primary_branch."record_id"=e."id" '
            "AND primary_branch.\"role\"='primary')",
            (principal_id, entity.name),
            limit,
            lambda _row: {"missing_role": "primary"},
        )
    if scan_type == "duplicate_authority":
        return _duplicate_candidates(connection, principal_id, entity, query.branch_id, limit)
    if scan_type == "blob_candidate":
        return _blob_candidates(connection, principal_id, entity, query.branch_id, limit)
    raise RecordContractError()


def _simple_candidates(
    connection: sqlite3.Connection,
    principal_id: str,
    entity: RecordEntity,
    branch_id: UUID | None,
    scan_type: str,
    predicate: str,
    predicate_parameters: tuple[object, ...],
    limit: int,
    details: Any,
    selected: tuple[str, ...] = (),
) -> list[MaintenanceCandidate]:
    scope, parameters = _scope(entity.name, principal_id, branch_id)
    aliases = [f"{expression} AS value_{index}" for index, expression in enumerate(selected)]
    select = 'e."id"' + ("," + ",".join(aliases) if aliases else "")
    cursor = connection.execute(
        f"SELECT {select} FROM {_quote(entity.name)} AS e "
        f'WHERE {scope} AND ({predicate}) ORDER BY e."id" ASC LIMIT ?',
        (*parameters, *predicate_parameters, limit),
    )
    names = tuple(item[0] for item in cursor.description or ())
    return [
        MaintenanceCandidate(
            scan_type,
            entity.name,
            _uuid(row["id"]),
            FrozenJsonObject.from_mapping(details(row)),
        )
        for row in (dict(zip(names, values, strict=True)) for values in cursor.fetchall())
    ]


def _superseded_candidates(
    connection: sqlite3.Connection,
    principal_id: str,
    entity: RecordEntity,
    branch_id: UUID | None,
    limit: int,
) -> list[MaintenanceCandidate]:
    fields = {field.name: field for field in entity.domain_fields}
    predicates: list[str] = []
    selected: list[str] = []
    if (status := fields.get("status")) is not None and status.logical_type == "string":
        predicates.append("e.\"status\"='superseded'")
        selected.append('e."status"')
    if (superseded_by := fields.get("superseded_by")) is not None:
        predicates.append(_present_sql("e", superseded_by))
        selected.append('e."superseded_by"')

    def details(row: Mapping[str, object]) -> dict[str, object]:
        signals: list[str] = []
        offset = 0
        if "status" in fields and fields["status"].logical_type == "string":
            if row.get(f"value_{offset}") == "superseded":
                signals.append("status")
            offset += 1
        if "superseded_by" in fields and _present_value(
            fields["superseded_by"], row.get(f"value_{offset}")
        ):
            signals.append("superseded_by")
        return {"signals": signals}

    return _simple_candidates(
        connection,
        principal_id,
        entity,
        branch_id,
        "superseded",
        " OR ".join(predicates),
        (),
        limit,
        details,
        tuple(selected),
    )


def _missing_authority_candidates(
    connection: sqlite3.Connection,
    principal_id: str,
    entity: RecordEntity,
    branch_id: UUID | None,
    limit: int,
) -> list[MaintenanceCandidate]:
    fields = _authority_fields(entity)
    predicate = " AND ".join(f"NOT ({_present_sql('e', field)})" for field in fields)
    return _simple_candidates(
        connection,
        principal_id,
        entity,
        branch_id,
        "missing_authority",
        predicate,
        (),
        limit,
        lambda _row: {"declared_field_count": len(fields)},
    )


def _duplicate_candidates(
    connection: sqlite3.Connection,
    principal_id: str,
    entity: RecordEntity,
    branch_id: UUID | None,
    limit: int,
) -> list[MaintenanceCandidate]:
    found: dict[UUID, MaintenanceCandidate] = {}
    for field in _duplicate_fields(entity):
        scope, parameters = _scope(entity.name, principal_id, branch_id)
        field_sql = _quote(field.name)
        rows = connection.execute(
            f'SELECT e."id",duplicates.duplicate_count FROM {_quote(entity.name)} AS e '
            f"JOIN (SELECT grouped.{field_sql} AS authority_value,count(*) AS duplicate_count "
            f"FROM {_quote(entity.name)} AS grouped WHERE "
            f"{_scope(entity.name, principal_id, branch_id, alias='grouped')[0]} "
            f"AND {_present_sql('grouped', field)} GROUP BY grouped.{field_sql} HAVING count(*)>1"
            f") AS duplicates ON duplicates.authority_value=e.{field_sql} "
            f'WHERE {scope} ORDER BY e."id" ASC LIMIT ?',
            (
                *_scope(entity.name, principal_id, branch_id, alias="grouped")[1],
                *parameters,
                limit,
            ),
        ).fetchall()
        for record_id, duplicate_count in rows:
            identity = _uuid(record_id)
            found.setdefault(
                identity,
                MaintenanceCandidate(
                    "duplicate_authority",
                    entity.name,
                    identity,
                    FrozenJsonObject.from_mapping(
                        {"duplicate_count": _count(duplicate_count), "field_name": field.name}
                    ),
                ),
            )
    return [found[key] for key in sorted(found, key=str)[:limit]]


def _blob_candidates(
    connection: sqlite3.Connection,
    principal_id: str,
    entity: RecordEntity,
    branch_id: UUID | None,
    limit: int,
) -> list[MaintenanceCandidate]:
    fields = tuple(
        field
        for field in entity.domain_fields
        if field.logical_type == "string" and field.retrieval_mode == "annotation"
    )
    found: dict[UUID, MaintenanceCandidate] = {}
    for field in fields:
        scope, parameters = _scope(entity.name, principal_id, branch_id)
        field_sql = _quote(field.name)
        rows = connection.execute(
            f'SELECT e."id",length(CAST(e.{field_sql} AS BLOB)) '
            f"FROM {_quote(entity.name)} AS e WHERE {scope} "
            f'AND length(CAST(e.{field_sql} AS BLOB))>? ORDER BY e."id" ASC LIMIT ?',
            (*parameters, _BLOB_THRESHOLD_BYTES, limit),
        ).fetchall()
        for record_id, byte_length in rows:
            identity = _uuid(record_id)
            found.setdefault(
                identity,
                MaintenanceCandidate(
                    "blob_candidate",
                    entity.name,
                    identity,
                    FrozenJsonObject.from_mapping(
                        {
                            "byte_length": _count(byte_length),
                            "field_name": field.name,
                            "threshold_bytes": _BLOB_THRESHOLD_BYTES,
                        }
                    ),
                ),
            )
    return [found[key] for key in sorted(found, key=str)[:limit]]


def _scope(
    entity_name: str,
    principal_id: str,
    branch_id: UUID | None,
    *,
    alias: str = "e",
) -> tuple[str, tuple[object, ...]]:
    clauses = [f'{alias}."owner_id"=?']
    parameters: list[object] = [principal_id]
    if branch_id is not None:
        clauses.append(
            f'EXISTS (SELECT 1 FROM "{R3_RECORD_BRANCH_TABLE}" AS scoped_branch '
            'WHERE scoped_branch."principal_id"=? AND scoped_branch."entity_name"=? '
            f'AND scoped_branch."record_id"={alias}."id" AND scoped_branch."branch_id"=?)'
        )
        parameters.extend((principal_id, entity_name, str(branch_id)))
    return " AND ".join(clauses), tuple(parameters)


def _authority_fields(entity: RecordEntity) -> tuple[RecordField, ...]:
    return tuple(field for field in entity.domain_fields if field.name in _AUTHORITY_FIELDS)


def _duplicate_fields(entity: RecordEntity) -> tuple[RecordField, ...]:
    fields = {field.name: field for field in entity.domain_fields}
    return tuple(
        fields[name]
        for name in _DUPLICATE_AUTHORITY_FIELDS
        if name in fields and fields[name].logical_type in {"string", "uuid"}
    )


def _present_sql(alias: str, field: RecordField) -> str:
    value = f"{alias}.{_quote(field.name)}"
    if field.logical_type == "string":
        return f"{value} IS NOT NULL AND trim({value})<>''"
    if field.logical_type == "string_list":
        return f"{value} IS NOT NULL AND {value}<>'[]'"
    return f"{value} IS NOT NULL"


def _present_value(field: RecordField, value: object) -> bool:
    if value is None:
        return False
    if field.logical_type == "string":
        return type(value) is str and bool(value.strip())
    if field.logical_type == "string_list":
        return value != "[]"
    return True


def _decode_record(
    row: Mapping[str, Any],
    principal_id: str,
    specification: RecordSpecification,
    entity_name: str,
) -> RecordValue:
    entity = specification.entity(entity_name)
    if entity is None or row.get("owner_id") != principal_id:
        raise StorageUnavailable()
    try:
        fields = {
            field.name: _decode_field(field, row[field.name]) for field in entity.domain_fields
        }
        return RecordValue(
            entity_name,
            _uuid(row["id"]),
            _revision(row["record_version"]),
            decode_time(row["created_at"]),
            decode_time(row["updated_at"]),
            _text(row["created_via"]),
            specification.validate_create(entity_name, fields),
        )
    except (RecordContractError, StorageUnavailable):
        raise
    except Exception:
        raise StorageUnavailable() from None


def _decode_field(field: RecordField, value: object) -> object:
    if value is None:
        return None
    if field.logical_type == "boolean":
        if type(value) is not int or value not in {0, 1}:
            raise StorageUnavailable()
        return bool(value)
    if field.logical_type == "integer":
        if not is_signed_64_integer(value):
            raise StorageUnavailable()
        return value
    if field.logical_type == "number":
        if not is_exact_number(value):
            raise StorageUnavailable()
        return float(value)
    if field.logical_type == "string_list":
        if type(value) is not str:
            raise StorageUnavailable()
        raw = json.loads(value)
        if (
            type(raw) is not list
            or json.dumps(raw, ensure_ascii=False, separators=(",", ":"), sort_keys=True) != value
        ):
            raise StorageUnavailable()
        return raw
    if type(value) is not str:
        raise StorageUnavailable()
    return value


def _scalar_count(
    connection: sqlite3.Connection, statement: str, parameters: tuple[object, ...]
) -> int:
    row = connection.execute(statement, parameters).fetchone()
    if row is None:
        raise StorageUnavailable()
    return _count(row[0])


def _require_inputs(
    connection: sqlite3.Connection,
    principal_id: str,
    specification: RecordSpecification,
) -> None:
    if (
        not isinstance(connection, sqlite3.Connection)
        or type(principal_id) is not str
        or not 1 <= len(principal_id) <= 128
        or "\x00" in principal_id
        or type(specification) is not RecordSpecification
    ):
        raise RecordContractError()


def _count(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise StorageUnavailable()
    return value


def _revision(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or not 1 <= value <= 2**63 - 1:
        raise StorageUnavailable()
    return value


def _uuid(value: object) -> UUID:
    if type(value) is not str:
        raise StorageUnavailable()
    try:
        parsed = UUID(value)
    except ValueError:
        raise StorageUnavailable() from None
    if str(parsed) != value or parsed.int == 0:
        raise StorageUnavailable()
    return parsed


def _optional_uuid(value: object) -> UUID | None:
    return None if value is None else _uuid(value)


def _text(value: object) -> str:
    if type(value) is not str:
        raise StorageUnavailable()
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _utc_datetime(value: object) -> bool:
    return type(value) is datetime and value.tzinfo is UTC and value.utcoffset() == timedelta(0)


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'
