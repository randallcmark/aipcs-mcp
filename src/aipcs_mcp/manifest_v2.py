"""Strict, backend-neutral public manifest-v2 models."""

from __future__ import annotations

from collections import Counter
from math import isfinite
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

MAX_ENTITIES = 32
MAX_ATTRIBUTES_PER_ENTITY = 64
MAX_RELATIONSHIPS = 128
MAX_INDICES = 128
MAX_QUERY_PATTERNS = 32
MAX_DISCOVERY_FACETS = 64
IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"

# These fields are visible in every manifest for portable inspection, but record
# create/update input must not provide their values. V1-08 adds CAS semantics for
# record_version; its declaration is already part of the public manifest shape.
SERVER_MANAGED_FIELDS: dict[str, dict[str, object]] = {
    "id": {"type": "uuid", "required": True, "primary_key": True},
    "owner_id": {"type": "string", "required": True, "primary_key": False},
    "created_at": {"type": "datetime", "required": True, "primary_key": False},
    "updated_at": {"type": "datetime", "required": True, "primary_key": False},
    "created_via": {"type": "string", "required": True, "primary_key": False},
    "record_version": {"type": "integer", "required": True, "primary_key": False},
}
RETIRED_INPUT_FIELDS = frozenset(
    {
        "tool_definitions",
        "tools",
        "aliases",
        "classification_confidence",
        "parent_service_id",
        "session_count",
        "endpoint",
        "endpoints",
        "database_url",
        "database_path",
        "dsn",
        "sqlite_path",
        "storage_path",
        "materialisation",
        "materialization",
    }
)

AllowedValue = StrictStr | StrictInt | StrictFloat | StrictBool
MigrationOperation = Annotated[StrictStr, Field(min_length=1, max_length=240)]


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, serialize_by_alias=True)


class AttributeDefinition(PublicModel):
    name: str = Field(pattern=IDENTIFIER_PATTERN)
    type: Literal["string", "integer", "number", "boolean", "datetime", "uuid", "string_list"]
    required: bool = False
    primary_key: bool = False
    description: str | None = Field(default=None, max_length=500)
    allowed_values: list[AllowedValue] | None = Field(default=None, min_length=1, max_length=32)
    retrieval_mode: Literal["annotation", "membership"] | None = None

    @model_validator(mode="after")
    def validate_type_metadata(self) -> AttributeDefinition:
        if self.primary_key and not self.required:
            raise ValueError("Primary-key attributes must be required.")
        if self.allowed_values is not None:
            typed_values = [(type(value), value) for value in self.allowed_values]
            if len(typed_values) != len(set(typed_values)):
                raise ValueError("allowed_values must not contain duplicates.")
            if not all(
                _allowed_value_matches_type(value, self.type) for value in self.allowed_values
            ):
                raise ValueError(
                    "allowed_values must contain values compatible with the attribute type."
                )
            if any(
                isinstance(value, str) and (not value or len(value) > 96)
                for value in self.allowed_values
            ):
                raise ValueError("String allowed_values must contain 1 to 96 characters.")
            if any(type(value) is float and not isfinite(value) for value in self.allowed_values):
                raise ValueError("Numeric allowed_values must be finite.")
            if any(type(value) is str and "\x00" in value for value in self.allowed_values):
                raise ValueError("String allowed_values must not contain NUL characters.")
        if self.retrieval_mode == "membership" and self.type != "string_list":
            raise ValueError("membership retrieval_mode requires a string_list attribute.")
        if self.retrieval_mode == "annotation" and self.type != "string":
            raise ValueError("annotation retrieval_mode requires a string attribute.")
        return self


class EntityDefinition(PublicModel):
    name: str = Field(pattern=IDENTIFIER_PATTERN)
    attributes: list[AttributeDefinition] = Field(
        min_length=6, max_length=MAX_ATTRIBUTES_PER_ENTITY
    )
    description: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def validate_server_managed_fields(self) -> EntityDefinition:
        names = [attribute.name for attribute in self.attributes]
        duplicates = [name for name, count in Counter(names).items() if count > 1]
        if duplicates:
            raise ValueError(
                f"Entity attributes must be unique; duplicate names: {', '.join(duplicates)}."
            )
        by_name = {attribute.name: attribute for attribute in self.attributes}
        for name, expected in SERVER_MANAGED_FIELDS.items():
            actual = by_name.get(name)
            if actual is None:
                raise ValueError(f"Entity must declare server-managed field {name!r}.")
            if (
                actual.type != expected["type"]
                or actual.required is not expected["required"]
                or actual.primary_key is not expected["primary_key"]
                or actual.description is not None
                or actual.allowed_values is not None
                or actual.retrieval_mode is not None
            ):
                raise ValueError(
                    f"Server-managed field {name!r} does not match its required declaration."
                )
        unexpected_primary_keys = [
            attribute.name
            for attribute in self.attributes
            if attribute.primary_key and attribute.name != "id"
        ]
        if unexpected_primary_keys:
            raise ValueError("The server-managed id field is the only supported primary key.")
        return self


class RelationshipEndpoint(PublicModel):
    entity: str = Field(pattern=IDENTIFIER_PATTERN)
    field: str = Field(pattern=IDENTIFIER_PATTERN)


class RelationshipDefinition(PublicModel):
    name: str = Field(pattern=IDENTIFIER_PATTERN, max_length=48)
    from_: RelationshipEndpoint = Field(alias="from", serialization_alias="from")
    to: RelationshipEndpoint
    on_delete: Literal["restrict"]


class IndexDefinition(PublicModel):
    name: str = Field(pattern=IDENTIFIER_PATTERN, max_length=48)
    entity: str = Field(pattern=IDENTIFIER_PATTERN)
    fields: list[Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]] = Field(
        min_length=1, max_length=8
    )
    unique: bool = False

    @model_validator(mode="after")
    def validate_distinct_fields(self) -> IndexDefinition:
        if len(self.fields) != len(set(self.fields)):
            raise ValueError("Index fields must not contain duplicates.")
        return self


class DiscoveryFacet(PublicModel):
    entity: str = Field(pattern=IDENTIFIER_PATTERN)
    field: str = Field(pattern=IDENTIFIER_PATTERN)


class MigrationHistoryEntry(PublicModel):
    from_schema_version: int = Field(ge=1)
    to_schema_version: int = Field(ge=1)
    operations: list[MigrationOperation] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_version_direction(self) -> MigrationHistoryEntry:
        if self.to_schema_version <= self.from_schema_version:
            raise ValueError("Migration history must advance schema_version.")
        return self


class ManifestV2(PublicModel):
    """The complete public schema document; manifest v1 never validates here."""

    manifest_version: Literal[2]
    schema_version: int = Field(ge=1, le=65)
    entities: list[EntityDefinition] = Field(min_length=1, max_length=MAX_ENTITIES)
    relationships: list[RelationshipDefinition] = Field(
        default_factory=list, max_length=MAX_RELATIONSHIPS
    )
    indices: list[IndexDefinition] = Field(default_factory=list, max_length=MAX_INDICES)
    query_patterns: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(
        default_factory=list, max_length=MAX_QUERY_PATTERNS
    )
    discovery_facets: list[DiscoveryFacet] = Field(
        default_factory=list, max_length=MAX_DISCOVERY_FACETS
    )
    retrieval_guidance: str | None = Field(default=None, max_length=2_000)
    migration_history: list[MigrationHistoryEntry] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_references(self) -> ManifestV2:
        expected_history_length = self.schema_version - 1
        if len(self.migration_history) != expected_history_length:
            raise ValueError(
                "migration_history must contain exactly one entry per schema transition "
                "from version 1."
            )
        for expected_from, entry in enumerate(self.migration_history, start=1):
            if (
                entry.from_schema_version != expected_from
                or entry.to_schema_version != expected_from + 1
            ):
                raise ValueError(
                    "migration_history must advance continuously one version at a time from "
                    "version 1."
                )

        entities = {entity.name: entity for entity in self.entities}
        if len(entities) != len(self.entities):
            raise ValueError("Entity names must be unique.")
        if any(_is_sqlite_reserved(name) for name in entities):
            raise ValueError("Entity names must not use SQLite-reserved names.")
        relation_names = [relationship.name for relationship in self.relationships]
        index_names = [index.name for index in self.indices]
        if len(relation_names) != len(set(relation_names)):
            raise ValueError("Relationship names must be unique.")
        if len(index_names) != len(set(index_names)):
            raise ValueError("Index names must be unique.")
        if any(_is_sqlite_reserved(name) for name in index_names):
            raise ValueError("Index names must not use SQLite-reserved names.")
        if set(entities).intersection(index_names):
            raise ValueError("Entity and index names must not collide.")
        index_definitions = [
            (index.entity, tuple(index.fields), index.unique) for index in self.indices
        ]
        if len(index_definitions) != len(set(index_definitions)):
            raise ValueError("Duplicate index definitions are not allowed.")
        relationship_definitions = [
            (
                relationship.from_.entity,
                relationship.from_.field,
                relationship.to.entity,
                relationship.to.field,
                relationship.on_delete,
            )
            for relationship in self.relationships
        ]
        if len(relationship_definitions) != len(set(relationship_definitions)):
            raise ValueError("Duplicate relationship definitions are not allowed.")
        relationship_sources = [
            (relationship.from_.entity, relationship.from_.field)
            for relationship in self.relationships
        ]
        if len(relationship_sources) != len(set(relationship_sources)):
            raise ValueError("Relationship source endpoints must be unique.")
        facet_definitions = [(facet.entity, facet.field) for facet in self.discovery_facets]
        if len(facet_definitions) != len(set(facet_definitions)):
            raise ValueError("Duplicate discovery facets are not allowed.")
        for facet in self.discovery_facets:
            _attribute_for(entities, facet.entity, facet.field, "Discovery facet")
        for index in self.indices:
            for field in index.fields:
                attribute = _attribute_for(entities, index.entity, field, "Index")
                if attribute.type == "string_list":
                    raise ValueError("Indexes cannot target string_list fields.")
        required_relationship_edges: set[tuple[str, str]] = set()
        for relationship in self.relationships:
            source = _attribute_for(
                entities, relationship.from_.entity, relationship.from_.field, "Relationship source"
            )
            target = _attribute_for(
                entities, relationship.to.entity, relationship.to.field, "Relationship target"
            )
            if source.primary_key or relationship.from_.field in SERVER_MANAGED_FIELDS:
                raise ValueError(
                    "Relationship sources must be agent-declared non-primary-key fields."
                )
            if source.type != "uuid":
                raise ValueError("Relationship source fields must have type uuid.")
            if relationship.to.field != "id" or target.type != "uuid" or not target.primary_key:
                raise ValueError("Relationships may target only the automatic id primary key.")
            if relationship.from_ == relationship.to:
                raise ValueError(
                    "A relationship may not use the identical source and target field."
                )
            if source.required:
                required_relationship_edges.add(
                    (relationship.from_.entity, relationship.to.entity)
                )
        if _has_directed_cycle(required_relationship_edges):
            raise ValueError("Required relationship edges must not contain a directed cycle.")
        return self

    def to_public_dict(self) -> dict[str, object]:
        """Serialize aliases so relationship source always appears as JSON key `from`."""

        return self.model_dump(mode="json", by_alias=True)


def _attribute_for(
    entities: dict[str, EntityDefinition], entity_name: str, field_name: str, label: str
) -> AttributeDefinition:
    entity = entities.get(entity_name)
    if entity is None:
        raise ValueError(f"{label} refers to unknown entity {entity_name!r}.")
    for attribute in entity.attributes:
        if attribute.name == field_name:
            return attribute
    raise ValueError(f"{label} refers to unknown field {entity_name}.{field_name}.")


def _allowed_value_matches_type(value: AllowedValue, attribute_type: str) -> bool:
    if attribute_type in {"string", "string_list"}:
        return type(value) is str
    if attribute_type == "integer":
        return type(value) is int
    if attribute_type == "number":
        return type(value) in {int, float}
    if attribute_type == "boolean":
        return type(value) is bool
    return False


def _is_sqlite_reserved(name: str) -> bool:
    return name.casefold().startswith("sqlite_")


def _has_directed_cycle(edges: set[tuple[str, str]]) -> bool:
    adjacency: dict[str, set[str]] = {}
    for source, target in edges:
        adjacency.setdefault(source, set()).add(target)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(target) for target in sorted(adjacency.get(node, ()))):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in sorted(adjacency))
