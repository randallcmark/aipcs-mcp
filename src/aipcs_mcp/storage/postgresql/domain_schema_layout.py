"""Pure native-PostgreSQL layout compiler for one relational domain schema."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from aipcs_mcp.relational import (
    MetadataChange,
    RelationalAdditions,
    RelationalEntity,
    RelationalField,
    RelationalIndex,
    RelationalRelationship,
    RelationalSpecification,
    RelationalTransition,
    _expected_additions,
)
from aipcs_mcp.storage.errors import StorageContractError

_TYPES = {
    "string": "text",
    "integer": "bigint",
    "number": "double precision",
    "boolean": "boolean",
    "datetime": "timestamp with time zone",
    "uuid": "uuid",
    "string_list": "jsonb",
}
_OPCLASSES = {
    "string": ("text_ops", "C"),
    "integer": ("int8_ops", None),
    "number": ("float8_ops", None),
    "boolean": ("bool_ops", None),
    "datetime": ("timestamptz_ops", None),
    "uuid": ("uuid_ops", None),
}
_NAMESPACE = re.compile(r"^svc_[0-9a-f]{32}$")


@dataclass(frozen=True)
class PostgreSQLDomainTable:
    name: str
    primary_key_name: str
    columns: tuple[tuple[str, str, bool, str | None], ...]
    primary_key_signature: tuple[str, str | None]
    relationships: tuple[RelationalRelationship, ...]
    ddl: str


@dataclass(frozen=True)
class PostgreSQLDomainIndex:
    name: str
    table: str
    fields: tuple[str, ...]
    unique: bool
    key_signatures: tuple[tuple[str, str | None], ...]
    ddl: str


@dataclass(frozen=True)
class PostgreSQLDomainLayout:
    schema_version: int
    tables: tuple[PostgreSQLDomainTable, ...]
    indices: tuple[PostgreSQLDomainIndex, ...]
    ddl: tuple[str, ...]


@dataclass(frozen=True)
class PostgreSQLDomainTransitionLayout:
    current: PostgreSQLDomainLayout
    target: PostgreSQLDomainLayout
    ddl: tuple[str, ...]


def build_postgresql_domain_layout(
    specification: RelationalSpecification, namespace: str
) -> PostgreSQLDomainLayout:
    """Deeply detach a relational specification and render deterministic native DDL."""

    try:
        return _build(_detached_specification(specification), _checked_namespace(namespace))
    except Exception:
        raise StorageContractError() from None


def build_postgresql_domain_transition_layout(
    transition: RelationalTransition, namespace: str
) -> PostgreSQLDomainTransitionLayout:
    """Compile exactly one accepted adjacent additive transition."""

    try:
        if type(transition) is not RelationalTransition:
            raise StorageContractError()
        current = _detached_specification(transition.current)
        target = _detached_specification(transition.target)
        if target.schema_version != current.schema_version + 1:
            raise StorageContractError()
        additions = transition.additions
        if type(additions) is not RelationalAdditions:
            raise StorageContractError()
        expected = _expected_additions(current, target)
        if additions != expected:
            raise StorageContractError()
        changes = transition.metadata_changes
        if (
            type(changes) is not tuple
            or any(type(item) is not MetadataChange for item in changes)
            or tuple(sorted(changes, key=_metadata_sort_key)) != changes
            or len(set(changes)) != len(changes)
            or not (expected.entities or expected.fields or expected.relationships or expected.indices or changes)
        ):
            raise StorageContractError()
        checked_namespace = _checked_namespace(namespace)
        current_layout = _build(current, checked_namespace)
        target_layout = _build(target, checked_namespace)
        tables = {table.name: table for table in target_layout.tables}
        indices = {index.name: index for index in target_layout.indices}
        ddl = tuple(tables[entity.name].ddl for entity in expected.entities)
        ddl += tuple(
            f"ALTER TABLE {_qualified(checked_namespace, item.entity)} ADD COLUMN {_column(item.field)}"
            for item in expected.fields
        )
        ddl += tuple(indices[index.name].ddl for index in expected.indices)
        return PostgreSQLDomainTransitionLayout(current_layout, target_layout, ddl)
    except Exception:
        raise StorageContractError() from None


def _build(specification: RelationalSpecification, namespace: str) -> PostgreSQLDomainLayout:
    tables = tuple(
        _table(entity, specification.relationships, namespace) for entity in specification.entities
    )
    indices = tuple(_index(value, namespace, specification.entities) for value in specification.indices)
    return PostgreSQLDomainLayout(
        specification.schema_version,
        tables,
        indices,
        tuple(table.ddl for table in tables) + tuple(index.ddl for index in indices),
    )


def _table(
    entity: RelationalEntity, relationships: tuple[RelationalRelationship, ...], namespace: str
) -> PostgreSQLDomainTable:
    local = tuple(item for item in relationships if item.source_entity == entity.name)
    columns = tuple(
        (
            field.name,
            _TYPES[field.logical_type],
            field.required,
            "C" if field.logical_type == "string" else None,
        )
        for field in entity.fields
    )
    primary_key_name = _primary_key_name(entity.name)
    declarations = [_column(field) for field in entity.fields]
    declarations.append(f"CONSTRAINT {_quote(primary_key_name)} PRIMARY KEY ({_quote('id')})")
    declarations.extend(_foreign_key(item, namespace) for item in local)
    ddl = (
        f"CREATE TABLE {_qualified(namespace, entity.name)} (\n    "
        + ",\n    ".join(declarations)
        + "\n)"
    )
    return PostgreSQLDomainTable(
        entity.name,
        primary_key_name,
        columns,
        _OPCLASSES["uuid"],
        local,
        ddl,
    )


def _index(
    index: RelationalIndex, namespace: str, entities: tuple[RelationalEntity, ...]
) -> PostgreSQLDomainIndex:
    unique = "UNIQUE " if index.unique else ""
    fields = ", ".join(_quote(name) for name in index.fields)
    entity = next(item for item in entities if item.name == index.entity)
    fields_by_name = {field.name: field for field in entity.fields}
    return PostgreSQLDomainIndex(
        index.name,
        index.entity,
        index.fields,
        index.unique,
        tuple(_OPCLASSES[fields_by_name[name].logical_type] for name in index.fields),
        # PostgreSQL grammar forbids a schema-qualified CREATE INDEX name.
        # The explicitly schema-qualified table selects the index namespace.
        f"CREATE {unique}INDEX {_quote(index.name)} "
        f"ON {_qualified(namespace, index.entity)} ({fields})",
    )


def _column(field: RelationalField) -> str:
    suffix = " NOT NULL" if field.required else ""
    collation = ' COLLATE "C"' if field.logical_type == "string" else ""
    return f"{_quote(field.name)} {_TYPES[field.logical_type]}{collation}{suffix}"


def _foreign_key(item: RelationalRelationship, namespace: str) -> str:
    return (
        f"CONSTRAINT {_quote(item.name)} FOREIGN KEY ({_quote(item.source_field)}) "
        f"REFERENCES {_qualified(namespace, item.target_entity)} ({_quote(item.target_field)}) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT NOT DEFERRABLE"
    )


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _qualified(namespace: str, identifier: str) -> str:
    return f"{_quote(namespace)}.{_quote(identifier)}"


def _checked_namespace(value: object) -> str:
    if type(value) is not str or _NAMESPACE.fullmatch(value) is None:
        raise StorageContractError()
    return value


def _primary_key_name(entity: str) -> str:
    return f"aipcs_domain_pk_{hashlib.sha256(entity.encode()).hexdigest()[:16]}"


def _detached_specification(value: object) -> RelationalSpecification:
    if type(value) is not RelationalSpecification:
        raise StorageContractError()
    try:
        entities = tuple(_entity(item) for item in value.entities)
        relationships = tuple(_relationship(item) for item in value.relationships)
        indices = tuple(_index_value(item) for item in value.indices)
        return RelationalSpecification(value.schema_version, entities, relationships, indices)
    except Exception:
        raise StorageContractError() from None


def _entity(value: object) -> RelationalEntity:
    if type(value) is not RelationalEntity or type(value.fields) is not tuple:
        raise StorageContractError()
    return RelationalEntity(value.name, tuple(_field(item) for item in value.fields))


def _field(value: object) -> RelationalField:
    if type(value) is not RelationalField:
        raise StorageContractError()
    return RelationalField(value.name, value.logical_type, value.required, value.primary_key)


def _relationship(value: object) -> RelationalRelationship:
    if type(value) is not RelationalRelationship:
        raise StorageContractError()
    return RelationalRelationship(
        value.name,
        value.source_entity,
        value.source_field,
        value.target_entity,
        value.target_field,
        value.on_delete,
        value.on_update,
        value.constraint_timing,
    )


def _index_value(value: object) -> RelationalIndex:
    if type(value) is not RelationalIndex or type(value.fields) is not tuple:
        raise StorageContractError()
    return RelationalIndex(value.name, value.entity, tuple(value.fields), value.unique)


def _metadata_sort_key(change: MetadataChange) -> tuple[str, str, str]:
    return (change.kind, change.entity or "", change.field or "")
