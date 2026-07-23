"""Read-only PostgreSQL discovery and mechanical maintenance queries.

The caller supplies an already-started read transaction and owns its complete
lifecycle.  These helpers issue bounded, schema-qualified ``SELECT`` statements
only; they never connect, begin, commit, roll back, migrate, or change session
state.
"""

from __future__ import annotations

import re
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

from .data_core import canonical_json_value, qualified, quote
from .service_store_migrations import BRANCH, RECORD_BRANCH

_NAMESPACE = re.compile(r"^svc_[0-9a-f]{32}$")
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
    connection: object,
    namespace: str,
    principal_id: str,
    specification: RecordSpecification,
    query: SummaryQuery,
) -> ServiceSummary:
    """Read one principal-scoped, deterministic service summary."""

    _require_inputs(connection, namespace, principal_id, specification)
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
                    f'SELECT count(*) FROM {qualified(namespace, entity.name)} WHERE "owner_id"=%s',
                    (principal_id,),
                ),
            )
            for entity in specification.entities
        )
        facets = tuple(
            _facet_observation(
                connection,
                namespace,
                principal_id,
                specification,
                facet.entity_name,
                facet.field_name,
            )
            for facet in specification.discovery_facets
            if (
                (entity := specification.entity(facet.entity_name)) is not None
                and any(field.name == facet.field_name for field in entity.domain_fields)
            )
        )
        branches = _branch_cards(connection, namespace, principal_id)
        unbranched = sum(
            _scalar_count(
                connection,
                f"SELECT count(*) FROM {qualified(namespace, entity.name)} AS e "
                'WHERE e."owner_id"=%s AND NOT EXISTS ('
                f"SELECT 1 FROM {qualified(namespace, RECORD_BRANCH)} AS rb "
                'WHERE rb."principal_id"=%s AND rb."entity_name"=%s '
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
                    namespace,
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
    connection: object,
    namespace: str,
    principal_id: str,
    specification: RecordSpecification,
    query: MaintenanceQuery,
    *,
    now: datetime,
) -> MaintenanceResult:
    """Return bounded mechanical candidates without ranking truth or mutating data."""

    _require_inputs(connection, namespace, principal_id, specification)
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
                        namespace,
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
    connection: object,
    namespace: str,
    principal_id: str,
    specification: RecordSpecification,
    entity_name: str,
    field_name: str,
) -> FacetObservation:
    field = specification.field(entity_name, field_name)
    if field is None:
        raise StorageUnavailable()
    table, column = qualified(namespace, entity_name), quote(field_name)
    if field.filter_mode == "membership":
        rows = _execute(
            connection,
            f'SELECT members.value COLLATE "C",count(*) FROM {table} AS e '
            f"CROSS JOIN LATERAL jsonb_array_elements_text(e.{column}) AS members(value) "
            'WHERE e."owner_id"=%s GROUP BY members.value COLLATE "C" '
            'ORDER BY count(*) DESC,members.value COLLATE "C" ASC LIMIT %s',
            (principal_id, MAX_FACET_VALUES),
        ).fetchall()
        values = tuple(FacetValue(_text(value), _count(count)) for value, count in rows)
    else:
        expression = f'{column} COLLATE "C"' if field.logical_type == "string" else column
        rows = _execute(
            connection,
            f"SELECT {expression},count(*) FROM {table} "
            f'WHERE "owner_id"=%s AND {column} IS NOT NULL '
            f"GROUP BY {expression} ORDER BY count(*) DESC,{expression} ASC LIMIT %s",
            (principal_id, MAX_FACET_VALUES),
        ).fetchall()
        values = tuple(
            FacetValue(_decode_field(field, value), _count(count)) for value, count in rows
        )
    return FacetObservation(entity_name, field_name, values)


def _branch_cards(connection: object, namespace: str, principal_id: str) -> tuple[BranchCard, ...]:
    rows = _execute(
        connection,
        f'SELECT b."id",b."slug",b."title",b."intent",b."branch_type",'
        'b."parent_branch_id",b."status",b."retrieval_summary",b."branch_revision",'
        'b."created_at",b."updated_at",'
        "sum(CASE WHEN rb.\"role\"='primary' THEN 1 ELSE 0 END),"
        "sum(CASE WHEN rb.\"role\"='related' THEN 1 ELSE 0 END) "
        f"FROM {qualified(namespace, BRANCH)} AS b "
        f"LEFT JOIN {qualified(namespace, RECORD_BRANCH)} AS rb "
        'ON rb."branch_id"=b."id" AND rb."principal_id"=%s '
        'WHERE b."principal_id"=%s GROUP BY b."id" '
        'ORDER BY b."updated_at" DESC,b."id" ASC LIMIT %s',
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
                _time(row[9]),
                _time(row[10]),
                _revision(row[8]),
            ),
            _count(row[11]),
            _count(row[12]),
        )
        for row in rows
    )


def _sample_records(
    connection: object,
    namespace: str,
    principal_id: str,
    specification: RecordSpecification,
    entity_name: str,
    size: int,
) -> tuple[RecordValue, ...]:
    if size == 0:
        return ()
    cursor = _execute(
        connection,
        f"SELECT * FROM {qualified(namespace, entity_name)} "
        'WHERE "owner_id"=%s ORDER BY "id" ASC LIMIT %s',
        (principal_id, size),
    )
    return tuple(
        _decode_record(row, principal_id, specification, entity_name) for row in _rows(cursor)
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
    connection: object,
    namespace: str,
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
            namespace,
            principal_id,
            entity,
            query.branch_id,
            scan_type,
            'e."valid_until" < %s',
            (now,),
            limit,
            lambda _row: {"field_name": "valid_until"},
        )
    if scan_type == "stale":
        cutoff = now - timedelta(days=query.stale_after_days)
        return _simple_candidates(
            connection,
            namespace,
            principal_id,
            entity,
            query.branch_id,
            scan_type,
            'e."updated_at" <= %s',
            (cutoff,),
            limit,
            lambda row: {
                "age_days": max(0, (now - _time(row["value_0"])).days),
                "threshold_days": query.stale_after_days,
            },
            ('e."updated_at"',),
        )
    if scan_type == "low_confidence":
        return _simple_candidates(
            connection,
            namespace,
            principal_id,
            entity,
            query.branch_id,
            scan_type,
            'e."confidence" < %s',
            (float(query.low_confidence_below),),
            limit,
            lambda _row: {"threshold": float(query.low_confidence_below)},
        )
    if scan_type == "superseded":
        return _superseded_candidates(
            connection, namespace, principal_id, entity, query.branch_id, limit
        )
    if scan_type == "missing_authority":
        return _missing_authority_candidates(
            connection, namespace, principal_id, entity, query.branch_id, limit
        )
    if scan_type == "unbranched":
        return _simple_candidates(
            connection,
            namespace,
            principal_id,
            entity,
            query.branch_id,
            scan_type,
            f"NOT EXISTS (SELECT 1 FROM {qualified(namespace, RECORD_BRANCH)} AS primary_branch "
            'WHERE primary_branch."principal_id"=%s '
            'AND primary_branch."entity_name"=%s '
            'AND primary_branch."record_id"=e."id" '
            "AND primary_branch.\"role\"='primary')",
            (principal_id, entity.name),
            limit,
            lambda _row: {"missing_role": "primary"},
        )
    if scan_type == "duplicate_authority":
        return _duplicate_candidates(
            connection, namespace, principal_id, entity, query.branch_id, limit
        )
    if scan_type == "blob_candidate":
        return _blob_candidates(connection, namespace, principal_id, entity, query.branch_id, limit)
    raise RecordContractError()


def _simple_candidates(
    connection: object,
    namespace: str,
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
    scope, parameters = _scope(namespace, entity.name, principal_id, branch_id)
    aliases = [f"{expression} AS value_{index}" for index, expression in enumerate(selected)]
    select = 'e."id"' + ("," + ",".join(aliases) if aliases else "")
    cursor = _execute(
        connection,
        f"SELECT {select} FROM {qualified(namespace, entity.name)} AS e "
        f'WHERE {scope} AND ({predicate}) ORDER BY e."id" ASC LIMIT %s',
        (*parameters, *predicate_parameters, limit),
    )
    return [
        MaintenanceCandidate(
            scan_type,
            entity.name,
            _uuid(row["id"]),
            FrozenJsonObject.from_mapping(details(row)),
        )
        for row in _rows(cursor)
    ]


def _superseded_candidates(
    connection: object,
    namespace: str,
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
        namespace,
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
    connection: object,
    namespace: str,
    principal_id: str,
    entity: RecordEntity,
    branch_id: UUID | None,
    limit: int,
) -> list[MaintenanceCandidate]:
    fields = _authority_fields(entity)
    predicate = " AND ".join(f"NOT ({_present_sql('e', field)})" for field in fields)
    return _simple_candidates(
        connection,
        namespace,
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
    connection: object,
    namespace: str,
    principal_id: str,
    entity: RecordEntity,
    branch_id: UUID | None,
    limit: int,
) -> list[MaintenanceCandidate]:
    found: dict[UUID, MaintenanceCandidate] = {}
    for field in _duplicate_fields(entity):
        scope, parameters = _scope(namespace, entity.name, principal_id, branch_id)
        grouped_scope, grouped_parameters = _scope(
            namespace, entity.name, principal_id, branch_id, alias="grouped"
        )
        column = quote(field.name)
        rows = _execute(
            connection,
            f'SELECT e."id",duplicates.duplicate_count '
            f"FROM {qualified(namespace, entity.name)} AS e "
            f"JOIN (SELECT grouped.{column} AS authority_value,count(*) AS duplicate_count "
            f"FROM {qualified(namespace, entity.name)} AS grouped "
            f"WHERE {grouped_scope} AND {_present_sql('grouped', field)} "
            f"GROUP BY grouped.{column} HAVING count(*)>1) AS duplicates "
            f"ON duplicates.authority_value=e.{column} "
            f'WHERE {scope} ORDER BY e."id" ASC LIMIT %s',
            (*grouped_parameters, *parameters, limit),
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
    return [found[key] for key in sorted(found, key=lambda value: value.bytes)[:limit]]


def _blob_candidates(
    connection: object,
    namespace: str,
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
        scope, parameters = _scope(namespace, entity.name, principal_id, branch_id)
        column = quote(field.name)
        rows = _execute(
            connection,
            f"SELECT e.\"id\",octet_length(convert_to(e.{column},'UTF8')) "
            f"FROM {qualified(namespace, entity.name)} AS e WHERE {scope} "
            f"AND octet_length(convert_to(e.{column},'UTF8'))>%s "
            f'ORDER BY e."id" ASC LIMIT %s',
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
    return [found[key] for key in sorted(found, key=lambda value: value.bytes)[:limit]]


def _scope(
    namespace: str,
    entity_name: str,
    principal_id: str,
    branch_id: UUID | None,
    *,
    alias: str = "e",
) -> tuple[str, tuple[object, ...]]:
    clauses = [f'{alias}."owner_id"=%s']
    parameters: list[object] = [principal_id]
    if branch_id is not None:
        clauses.append(
            f"EXISTS (SELECT 1 FROM {qualified(namespace, RECORD_BRANCH)} AS scoped_branch "
            'WHERE scoped_branch."principal_id"=%s AND scoped_branch."entity_name"=%s '
            f'AND scoped_branch."record_id"={alias}."id" AND scoped_branch."branch_id"=%s)'
        )
        parameters.extend((principal_id, entity_name, branch_id))
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
    value = f"{alias}.{quote(field.name)}"
    if field.logical_type == "string":
        return f"{value} IS NOT NULL AND btrim({value})<>''"
    if field.logical_type == "string_list":
        return f"{value} IS NOT NULL AND jsonb_array_length({value})>0"
    return f"{value} IS NOT NULL"


def _present_value(field: RecordField, value: object) -> bool:
    if value is None:
        return False
    if field.logical_type == "string":
        return type(value) is str and bool(value.strip())
    if field.logical_type == "string_list":
        decoded = canonical_json_value(value)
        return type(decoded) is list and bool(decoded)
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
            _time(row["created_at"]),
            _time(row["updated_at"]),
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
        if type(value) is not bool:
            raise StorageUnavailable()
        return value
    if field.logical_type == "integer":
        if not is_signed_64_integer(value):
            raise StorageUnavailable()
        return value
    if field.logical_type == "number":
        if not is_exact_number(value):
            raise StorageUnavailable()
        return float(value)
    if field.logical_type == "string_list":
        raw = canonical_json_value(value)
        if type(raw) is not list or any(type(item) is not str for item in raw):
            raise StorageUnavailable()
        return raw
    if field.logical_type == "uuid":
        return str(_uuid(value))
    if field.logical_type == "datetime":
        return _time_text(value)
    return _text(value)


def _scalar_count(connection: object, statement: str, parameters: tuple[object, ...]) -> int:
    row = _execute(connection, statement, parameters).fetchone()
    if row is None:
        raise StorageUnavailable()
    return _count(row[0])


def _require_inputs(
    connection: object,
    namespace: str,
    principal_id: str,
    specification: RecordSpecification,
) -> None:
    if (
        not callable(getattr(connection, "execute", None))
        or type(namespace) is not str
        or _NAMESPACE.fullmatch(namespace) is None
        or namespace == f"svc_{'0' * 32}"
        or type(principal_id) is not str
        or not 1 <= len(principal_id) <= 128
        or "\x00" in principal_id
        or type(specification) is not RecordSpecification
    ):
        raise RecordContractError()


def _execute(connection: object, statement: str, parameters: tuple[object, ...] = ()) -> Any:
    return connection.execute(statement, parameters)  # type: ignore[attr-defined,no-any-return]


def _rows(cursor: object) -> list[dict[str, object]]:
    try:
        description = cursor.description  # type: ignore[attr-defined]
        names = tuple(item.name if hasattr(item, "name") else item[0] for item in description)
        values = cursor.fetchall()  # type: ignore[attr-defined]
        return [
            dict(value) if isinstance(value, Mapping) else dict(zip(names, value, strict=True))
            for value in values
        ]
    except Exception:
        raise StorageUnavailable() from None


def _count(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise StorageUnavailable()
    return value


def _revision(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or not 1 <= value <= 2**63 - 1:
        raise StorageUnavailable()
    return value


def _uuid(value: object) -> UUID:
    try:
        parsed = value if type(value) is UUID else UUID(value)  # type: ignore[arg-type]
    except Exception:
        raise StorageUnavailable() from None
    if parsed.int == 0:
        raise StorageUnavailable()
    return parsed


def _optional_uuid(value: object) -> UUID | None:
    return None if value is None else _uuid(value)


def _time(value: object) -> datetime:
    if type(value) is str:
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise StorageUnavailable() from None
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise StorageUnavailable()
    return value.astimezone(UTC).replace(tzinfo=UTC)


def _time_text(value: object) -> str:
    return _time(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _text(value: object) -> str:
    if type(value) is not str:
        raise StorageUnavailable()
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _utc_datetime(value: object) -> bool:
    return type(value) is datetime and value.tzinfo is UTC and value.utcoffset() == timedelta(0)
