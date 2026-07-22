"""Canonical SQLite layout derived from one relational specification.

This module is intentionally pure: it neither opens SQLite nor owns a service
location.  The future domain-schema store uses its immutable output for both
DDL and exact physical inspection.
"""

from __future__ import annotations

from dataclasses import dataclass

from aipcs_mcp.relational import (
    RelationalEntity,
    RelationalField,
    RelationalIndex,
    RelationalRelationship,
    RelationalSpecification,
)
from aipcs_mcp.storage.errors import StorageContractError

_SQLITE_TYPES = {
    "string": "TEXT",
    "datetime": "TEXT",
    "uuid": "TEXT",
    "string_list": "TEXT",
    "integer": "INTEGER",
    "boolean": "INTEGER",
    "number": "REAL",
}


@dataclass(frozen=True)
class SQLiteDomainIndexLayout:
    """One expected explicit or implicit SQLite index signature."""

    name: str
    table: str
    sql: str | None
    unique: int
    origin: str
    partial: int
    xinfo: tuple[tuple[int, int, str | None, int, str, int], ...]


@dataclass(frozen=True)
class SQLiteDomainTableLayout:
    """One expected domain table plus its SQLite inspection signatures."""

    name: str
    sql: str
    xinfo: tuple[tuple[int, str, str, int, None, int, int], ...]
    foreign_keys: tuple[tuple[int, int, str, str, str, str, str, str], ...]
    implicit_primary_key_index: SQLiteDomainIndexLayout


@dataclass(frozen=True)
class SQLiteDomainSchemaLayout:
    """The complete immutable initial SQLite physical target."""

    schema_version: int
    tables: tuple[SQLiteDomainTableLayout, ...]
    indices: tuple[SQLiteDomainIndexLayout, ...]
    ddl: tuple[str, ...]

    @property
    def table_sql(self) -> tuple[tuple[str, str], ...]:
        """Canonical domain table SQL in deterministic creation order."""

        return tuple((table.name, table.sql) for table in self.tables)

    @property
    def index_sql(self) -> tuple[tuple[str, str], ...]:
        """Canonical explicit-index SQL in deterministic creation order."""

        return tuple((index.name, index.sql) for index in self.indices if index.sql is not None)

    @property
    def all_indices(self) -> tuple[SQLiteDomainIndexLayout, ...]:
        """Explicit indexes followed by one deterministic primary-key index per table."""

        return self.indices + tuple(table.implicit_primary_key_index for table in self.tables)


def build_domain_schema_layout(specification: RelationalSpecification) -> SQLiteDomainSchemaLayout:
    """Return the exact SQLite layout for one deeply revalidated specification.

    Reconstituting every nested relational value avoids treating a frozen
    dataclass instance as trustworthy merely because its outer type matches.
    Invalid, subclassed, incomplete, or structurally forged inputs are all a
    bounded storage-contract failure.
    """

    failure: StorageContractError | None = None
    try:
        checked = _detached_specification(specification)
        tables = tuple(_table_layout(entity, checked.relationships) for entity in checked.entities)
        indices = tuple(_index_layout(index, checked.entities) for index in checked.indices)
        return SQLiteDomainSchemaLayout(
            schema_version=checked.schema_version,
            tables=tables,
            indices=indices,
            ddl=tuple(table.sql for table in tables) + tuple(index.sql for index in indices),
        )
    except Exception:
        failure = StorageContractError()
    raise failure from None


def _detached_specification(value: object) -> RelationalSpecification:
    if type(value) is not RelationalSpecification:
        raise StorageContractError()
    if (
        type(value.entities) is not tuple
        or type(value.relationships) is not tuple
        or type(value.indices) is not tuple
    ):
        raise StorageContractError()
    entities = tuple(_detached_entity(entity) for entity in value.entities)
    relationships = tuple(_detached_relationship(item) for item in value.relationships)
    indices = tuple(_detached_index(item) for item in value.indices)
    try:
        return RelationalSpecification(value.schema_version, entities, relationships, indices)
    except Exception:
        raise StorageContractError() from None


def _detached_entity(value: object) -> RelationalEntity:
    if type(value) is not RelationalEntity:
        raise StorageContractError()
    try:
        if type(value.fields) is not tuple:
            raise StorageContractError()
        fields = tuple(_detached_field(field) for field in value.fields)
        return RelationalEntity(value.name, fields)
    except StorageContractError:
        raise
    except Exception:
        raise StorageContractError() from None


def _detached_field(value: object) -> RelationalField:
    if type(value) is not RelationalField:
        raise StorageContractError()
    try:
        return RelationalField(value.name, value.logical_type, value.required, value.primary_key)
    except Exception:
        raise StorageContractError() from None


def _detached_relationship(value: object) -> RelationalRelationship:
    if type(value) is not RelationalRelationship:
        raise StorageContractError()
    try:
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
    except Exception:
        raise StorageContractError() from None


def _detached_index(value: object) -> RelationalIndex:
    if type(value) is not RelationalIndex:
        raise StorageContractError()
    try:
        if type(value.fields) is not tuple:
            raise StorageContractError()
        return RelationalIndex(value.name, value.entity, tuple(value.fields), value.unique)
    except Exception:
        raise StorageContractError() from None


def _table_layout(
    entity: RelationalEntity,
    relationships: tuple[RelationalRelationship, ...],
) -> SQLiteDomainTableLayout:
    table_relationships = tuple(
        relationship for relationship in relationships if relationship.source_entity == entity.name
    )
    declarations = [_field_declaration(field) for field in entity.fields]
    declarations.extend(
        _foreign_key_declaration(relationship) for relationship in table_relationships
    )
    sql = (
        f"CREATE TABLE {_quote(entity.name)} (\n    " + ",\n    ".join(declarations) + "\n) STRICT"
    )
    fields = {field.name: (position, field) for position, field in enumerate(entity.fields)}
    return SQLiteDomainTableLayout(
        name=entity.name,
        sql=sql,
        xinfo=tuple(
            (
                position,
                field.name,
                _SQLITE_TYPES[field.logical_type],
                int(field.required),
                None,
                int(field.primary_key),
                0,
            )
            for position, field in enumerate(entity.fields)
        ),
        foreign_keys=tuple(
            (
                foreign_key_id,
                0,
                relationship.target_entity,
                relationship.source_field,
                relationship.target_field,
                "RESTRICT",
                "RESTRICT",
                "NONE",
            )
            for foreign_key_id, relationship in enumerate(reversed(table_relationships))
        ),
        implicit_primary_key_index=SQLiteDomainIndexLayout(
            name=f"sqlite_autoindex_{entity.name}_1",
            table=entity.name,
            sql=None,
            unique=1,
            origin="pk",
            partial=0,
            xinfo=(
                (
                    0,
                    fields["id"][0],
                    "id",
                    0,
                    "BINARY",
                    1,
                ),
                (1, -1, None, 0, "BINARY", 0),
            ),
        ),
    )


def _index_layout(
    index: RelationalIndex, entities: tuple[RelationalEntity, ...]
) -> SQLiteDomainIndexLayout:
    entity = next(entity for entity in entities if entity.name == index.entity)
    positions = {field.name: position for position, field in enumerate(entity.fields)}
    qualifier = "UNIQUE " if index.unique else ""
    fields = ", ".join(_quote(name) for name in index.fields)
    sql = f"CREATE {qualifier}INDEX {_quote(index.name)} ON {_quote(index.entity)} ({fields})"
    return SQLiteDomainIndexLayout(
        name=index.name,
        table=index.entity,
        sql=sql,
        unique=int(index.unique),
        origin="c",
        partial=0,
        xinfo=tuple(
            (position, positions[name], name, 0, "BINARY", 1)
            for position, name in enumerate(index.fields)
        )
        + ((len(index.fields), -1, None, 0, "BINARY", 0),),
    )


def _field_declaration(field: RelationalField) -> str:
    suffix = " PRIMARY KEY NOT NULL" if field.primary_key else " NOT NULL" if field.required else ""
    return f"{_quote(field.name)} {_SQLITE_TYPES[field.logical_type]}{suffix}"


def _foreign_key_declaration(relationship: RelationalRelationship) -> str:
    return (
        f"CONSTRAINT {_quote(relationship.name)} FOREIGN KEY ({_quote(relationship.source_field)}) "
        f"REFERENCES {_quote(relationship.target_entity)} ({_quote(relationship.target_field)}) "
        "ON DELETE RESTRICT ON UPDATE RESTRICT"
    )


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'
