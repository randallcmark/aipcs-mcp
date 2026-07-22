"""Pure relational projection and additive-transition classification.

The values in this module are deliberately backend-neutral.  They describe the
physical relational shape that an adapter must honour, but never contain SQL,
connection details, paths, registry state, or mutable Pydantic input models.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .manifest_v2 import (
    IDENTIFIER_PATTERN,
    SERVER_MANAGED_FIELDS,
    AttributeDefinition,
    EntityDefinition,
    ManifestV2,
)

_MAX_SCHEMA_VERSION = 65
_MAX_ENTITIES = 32
_MAX_ATTRIBUTES_PER_ENTITY = 64
_MAX_RELATIONSHIPS = 128
_MAX_INDICES = 128
_MAX_INDEX_FIELDS = 8
_MAX_RELATIONSHIP_NAME = 48
_MAX_INDEX_NAME = 48
_TRANSITION_FACTORY_KEY = object()
_IDENTIFIER = re.compile(IDENTIFIER_PATTERN)
_LOGICAL_TYPES = frozenset(
    {"string", "integer", "number", "boolean", "datetime", "uuid", "string_list"}
)
_METADATA_KINDS = frozenset(
    {
        "entity_description",
        "attribute_description",
        "retrieval_guidance",
        "query_patterns",
        "discovery_facets",
        "retrieval_mode",
        "allowed_values_expansion",
    }
)

LogicalType = Literal[
    "string", "integer", "number", "boolean", "datetime", "uuid", "string_list"
]
MetadataChangeKind = Literal[
    "entity_description",
    "attribute_description",
    "retrieval_guidance",
    "query_patterns",
    "discovery_facets",
    "retrieval_mode",
    "allowed_values_expansion",
]


class RelationalContractError(ValueError):
    """A bounded failure for invalid pure relational contract values."""

    message = "The relational contract value is invalid."

    def __init__(self) -> None:
        super().__init__(self.message)


@dataclass(frozen=True)
class RelationalField:
    name: str
    logical_type: LogicalType
    required: bool
    primary_key: bool

    def __post_init__(self) -> None:
        if (
            not _identifier(self.name)
            or type(self.logical_type) is not str
            or self.logical_type not in _LOGICAL_TYPES
            or type(self.required) is not bool
            or type(self.primary_key) is not bool
            or (self.primary_key and not self.required)
        ):
            raise RelationalContractError()


@dataclass(frozen=True)
class RelationalEntity:
    name: str
    fields: tuple[RelationalField, ...]

    def __post_init__(self) -> None:
        if (
            not _identifier(self.name)
            or type(self.fields) is not tuple
            or not self.fields
            or len(self.fields) > _MAX_ATTRIBUTES_PER_ENTITY
            or any(type(field) is not RelationalField for field in self.fields)
            or len({field.name for field in self.fields}) != len(self.fields)
        ):
            raise RelationalContractError()
        fields = {field.name: field for field in self.fields}
        for name, expected in SERVER_MANAGED_FIELDS.items():
            actual = fields.get(name)
            if actual is None or (
                actual.logical_type,
                actual.required,
                actual.primary_key,
            ) != (expected["type"], expected["required"], expected["primary_key"]):
                raise RelationalContractError()
        if any(field.primary_key and field.name != "id" for field in self.fields):
            raise RelationalContractError()


@dataclass(frozen=True)
class RelationalRelationship:
    name: str
    source_entity: str
    source_field: str
    target_entity: str
    target_field: str
    on_delete: Literal["restrict"]
    on_update: Literal["restrict"] = "restrict"
    constraint_timing: Literal["deferred"] = "deferred"

    def __post_init__(self) -> None:
        if (
            not all(
                _identifier(value)
                for value in (
                    self.name,
                    self.source_entity,
                    self.source_field,
                    self.target_entity,
                    self.target_field,
                )
            )
            or len(self.name) > _MAX_RELATIONSHIP_NAME
            or type(self.on_delete) is not str
            or self.on_delete != "restrict"
            or type(self.on_update) is not str
            or self.on_update != "restrict"
            or type(self.constraint_timing) is not str
            or self.constraint_timing != "deferred"
        ):
            raise RelationalContractError()


@dataclass(frozen=True)
class RelationalIndex:
    name: str
    entity: str
    fields: tuple[str, ...]
    unique: bool

    def __post_init__(self) -> None:
        if (
            not _identifier(self.name)
            or len(self.name) > _MAX_INDEX_NAME
            or not _identifier(self.entity)
            or type(self.fields) is not tuple
            or not self.fields
            or len(self.fields) > _MAX_INDEX_FIELDS
            or any(not _identifier(field) for field in self.fields)
            or len(set(self.fields)) != len(self.fields)
            or type(self.unique) is not bool
        ):
            raise RelationalContractError()


@dataclass(frozen=True)
class RelationalSpecification:
    """Immutable physical relational facts compiled from one manifest snapshot."""

    schema_version: int
    entities: tuple[RelationalEntity, ...]
    relationships: tuple[RelationalRelationship, ...]
    indices: tuple[RelationalIndex, ...]

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version < 1
            or self.schema_version > _MAX_SCHEMA_VERSION
            or type(self.entities) is not tuple
            or type(self.relationships) is not tuple
            or type(self.indices) is not tuple
            or not self.entities
            or len(self.entities) > _MAX_ENTITIES
            or len(self.relationships) > _MAX_RELATIONSHIPS
            or len(self.indices) > _MAX_INDICES
            or any(type(entity) is not RelationalEntity for entity in self.entities)
            or any(type(relationship) is not RelationalRelationship for relationship in self.relationships)
            or any(type(index) is not RelationalIndex for index in self.indices)
            or tuple(entity.name for entity in self.entities)
            != tuple(sorted(entity.name for entity in self.entities))
            or tuple(relationship.name for relationship in self.relationships)
            != tuple(sorted(relationship.name for relationship in self.relationships))
            or tuple(index.name for index in self.indices)
            != tuple(sorted(index.name for index in self.indices))
        ):
            raise RelationalContractError()
        _validate_specification_shape(self)

    def same_structure_as(self, other: object) -> bool:
        """Compare physical shape while deliberately ignoring only schema version."""

        return (
            type(other) is RelationalSpecification
            and self.entities == other.entities
            and self.relationships == other.relationships
            and self.indices == other.indices
        )


@dataclass(frozen=True)
class FieldAddition:
    entity: str
    field: RelationalField

    def __post_init__(self) -> None:
        if not _identifier(self.entity) or type(self.field) is not RelationalField:
            raise RelationalContractError()


@dataclass(frozen=True)
class RelationalAdditions:
    entities: tuple[RelationalEntity, ...]
    fields: tuple[FieldAddition, ...]
    relationships: tuple[RelationalRelationship, ...]
    indices: tuple[RelationalIndex, ...]

    def __post_init__(self) -> None:
        if (
            type(self.entities) is not tuple
            or type(self.fields) is not tuple
            or type(self.relationships) is not tuple
            or type(self.indices) is not tuple
            or any(type(value) is not RelationalEntity for value in self.entities)
            or any(type(value) is not FieldAddition for value in self.fields)
            or any(type(value) is not RelationalRelationship for value in self.relationships)
            or any(type(value) is not RelationalIndex for value in self.indices)
        ):
            raise RelationalContractError()


@dataclass(frozen=True)
class MetadataChange:
    kind: MetadataChangeKind
    entity: str | None = None
    field: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in _METADATA_KINDS:
            raise RelationalContractError()
        if self.kind in {"retrieval_guidance", "query_patterns", "discovery_facets"}:
            valid_shape = self.entity is None and self.field is None
        elif self.kind == "entity_description":
            valid_shape = _identifier(self.entity) and self.field is None
        else:
            valid_shape = _identifier(self.entity) and _identifier(self.field)
        if not valid_shape:
            raise RelationalContractError()


@dataclass(frozen=True, init=False)
class RelationalTransition:
    """One factory-built, internally consistent accepted additive transition."""

    current: RelationalSpecification
    target: RelationalSpecification
    additions: RelationalAdditions
    metadata_changes: tuple[MetadataChange, ...]

    def __init__(self) -> None:
        raise RelationalContractError()

    @classmethod
    def _build(
        cls,
        *,
        current: RelationalSpecification,
        target: RelationalSpecification,
        additions: RelationalAdditions,
        metadata_changes: tuple[MetadataChange, ...],
        _factory_key: object | None = None,
    ) -> RelationalTransition:
        if (
            _factory_key is not _TRANSITION_FACTORY_KEY
            or type(current) is not RelationalSpecification
            or type(target) is not RelationalSpecification
            or type(additions) is not RelationalAdditions
            or type(metadata_changes) is not tuple
            or any(type(change) is not MetadataChange for change in metadata_changes)
            or metadata_changes != tuple(sorted(metadata_changes, key=_metadata_sort_key))
            or len(set(metadata_changes)) != len(metadata_changes)
            or target.schema_version != current.schema_version + 1
        ):
            raise RelationalContractError()
        if additions != _expected_additions(current, target):
            raise RelationalContractError()
        if not (
            additions.entities
            or additions.fields
            or additions.relationships
            or additions.indices
            or metadata_changes
        ):
            raise RelationalContractError()
        result = object.__new__(cls)
        object.__setattr__(result, "current", current)
        object.__setattr__(result, "target", target)
        object.__setattr__(result, "additions", additions)
        object.__setattr__(result, "metadata_changes", metadata_changes)
        return result


def compile_manifest(manifest: ManifestV2) -> RelationalSpecification:
    """Compile one exact, detached manifest into a backend-neutral specification."""

    return _compile_checked_manifest(_detached_manifest(manifest))


def _compile_checked_manifest(checked: ManifestV2) -> RelationalSpecification:
    return RelationalSpecification(
        schema_version=checked.schema_version,
        entities=tuple(
            _entity(entity.name, entity.attributes)
            for entity in sorted(checked.entities, key=lambda item: item.name)
        ),
        relationships=tuple(
            RelationalRelationship(
                name=relationship.name,
                source_entity=relationship.from_.entity,
                source_field=relationship.from_.field,
                target_entity=relationship.to.entity,
                target_field=relationship.to.field,
                on_delete=relationship.on_delete,
            )
            for relationship in sorted(checked.relationships, key=lambda item: item.name)
        ),
        indices=tuple(
            RelationalIndex(
                name=index.name,
                entity=index.entity,
                fields=tuple(index.fields),
                unique=index.unique,
            )
            for index in sorted(checked.indices, key=lambda item: item.name)
        ),
    )


def classify_transition(current: ManifestV2, target: ManifestV2) -> RelationalTransition:
    """Return the only accepted append-only transition between two exact manifests."""

    source = _detached_manifest(current)
    destination = _detached_manifest(target)
    if destination.schema_version != source.schema_version + 1:
        raise RelationalContractError()
    if not _history_is_one_entry_extension(source, destination):
        raise RelationalContractError()

    current_specification = _compile_checked_manifest(source)
    target_specification = _compile_checked_manifest(destination)
    current_entities = {entity.name: entity for entity in source.entities}
    target_entities = {entity.name: entity for entity in destination.entities}
    if not set(current_entities) <= set(target_entities):
        raise RelationalContractError()
    new_entity_names = set(target_entities) - set(current_entities)

    metadata_changes: list[MetadataChange] = []
    new_fields: list[FieldAddition] = []
    target_specification_entities = {entity.name: entity for entity in target_specification.entities}

    for name in sorted(current_entities):
        source_entity = current_entities[name]
        destination_entity = target_entities[name]
        _classify_entity_metadata(source_entity, destination_entity, metadata_changes)
        source_attributes = source_entity.attributes
        destination_attributes = destination_entity.attributes
        if len(destination_attributes) < len(source_attributes):
            raise RelationalContractError()
        for source_attribute, destination_attribute in zip(
            source_attributes, destination_attributes[: len(source_attributes)], strict=True
        ):
            _classify_attribute_change(
                name, source_attribute, destination_attribute, metadata_changes
            )
        for attribute in destination_attributes[len(source_attributes) :]:
            if attribute.required or attribute.primary_key:
                raise RelationalContractError()
            new_fields.append(
                FieldAddition(
                    entity=name,
                    field=_field(attribute),
                )
            )

    _classify_global_metadata(source, destination, metadata_changes)
    new_relationships = _classify_relationships(source, destination, new_entity_names)
    new_indices = _classify_indices(source, destination)

    additions = RelationalAdditions(
        entities=tuple(
            target_specification_entities[name] for name in sorted(new_entity_names)
        ),
        fields=tuple(new_fields),
        relationships=tuple(new_relationships),
        indices=tuple(new_indices),
    )
    frozen_metadata = tuple(sorted(metadata_changes, key=_metadata_sort_key))
    if not (
        additions.entities
        or additions.fields
        or additions.relationships
        or additions.indices
        or frozen_metadata
    ):
        raise RelationalContractError()
    return RelationalTransition._build(
        current=current_specification,
        target=target_specification,
        additions=additions,
        metadata_changes=frozen_metadata,
        _factory_key=_TRANSITION_FACTORY_KEY,
    )


def _expected_additions(
    current: RelationalSpecification, target: RelationalSpecification
) -> RelationalAdditions:
    current_entities = {entity.name: entity for entity in current.entities}
    target_entities = {entity.name: entity for entity in target.entities}
    if not set(current_entities) <= set(target_entities):
        raise RelationalContractError()
    new_names = set(target_entities) - set(current_entities)
    fields: list[FieldAddition] = []
    for name in sorted(current_entities):
        source = current_entities[name]
        destination = target_entities[name]
        if destination.fields[: len(source.fields)] != source.fields:
            raise RelationalContractError()
        for field in destination.fields[len(source.fields) :]:
            if field.required or field.primary_key:
                raise RelationalContractError()
            fields.append(FieldAddition(name, field))

    source_relationships = {item.name: item for item in current.relationships}
    target_relationships = {item.name: item for item in target.relationships}
    if not set(source_relationships) <= set(target_relationships):
        raise RelationalContractError()
    if any(
        source_relationships[name] != target_relationships[name]
        for name in source_relationships
    ):
        raise RelationalContractError()
    relationships = tuple(
        target_relationships[name]
        for name in sorted(set(target_relationships) - set(source_relationships))
        if target_relationships[name].source_entity in new_names
    )
    if len(relationships) != len(target_relationships) - len(source_relationships):
        raise RelationalContractError()

    source_indices = {item.name: item for item in current.indices}
    target_indices = {item.name: item for item in target.indices}
    if not set(source_indices) <= set(target_indices):
        raise RelationalContractError()
    if any(source_indices[name] != target_indices[name] for name in source_indices):
        raise RelationalContractError()
    return RelationalAdditions(
        entities=tuple(target_entities[name] for name in sorted(new_names)),
        fields=tuple(fields),
        relationships=relationships,
        indices=tuple(
            target_indices[name]
            for name in sorted(set(target_indices) - set(source_indices))
        ),
    )


def _validate_specification_shape(specification: RelationalSpecification) -> None:
    entities = {entity.name: entity for entity in specification.entities}
    if len(entities) != len(specification.entities):
        raise RelationalContractError()
    if any(entity.name.startswith("sqlite_") for entity in specification.entities):
        raise RelationalContractError()
    if len({relationship.name for relationship in specification.relationships}) != len(
        specification.relationships
    ):
        raise RelationalContractError()
    if len({index.name for index in specification.indices}) != len(specification.indices):
        raise RelationalContractError()
    if any(index.name.startswith("sqlite_") for index in specification.indices):
        raise RelationalContractError()
    if {entity.name for entity in specification.entities} & {
        index.name for index in specification.indices
    }:
        raise RelationalContractError()

    endpoints: set[tuple[str, str]] = set()
    required_graph = {name: set[str]() for name in entities}
    for relationship in specification.relationships:
        source_entity = entities.get(relationship.source_entity)
        target_entity = entities.get(relationship.target_entity)
        if source_entity is None or target_entity is None:
            raise RelationalContractError()
        source_field = _entity_field(source_entity, relationship.source_field)
        target_field = _entity_field(target_entity, relationship.target_field)
        endpoint = (relationship.source_entity, relationship.source_field)
        if (
            source_field is None
            or target_field is None
            or source_field.logical_type != "uuid"
            or source_field.primary_key
            or relationship.target_field != "id"
            or target_field.logical_type != "uuid"
            or not target_field.primary_key
            or endpoint in endpoints
        ):
            raise RelationalContractError()
        endpoints.add(endpoint)
        if source_field.required:
            required_graph[relationship.source_entity].add(relationship.target_entity)
    if _has_directed_cycle(required_graph):
        raise RelationalContractError()

    index_definitions: set[tuple[str, tuple[str, ...], bool]] = set()
    for index in specification.indices:
        entity = entities.get(index.entity)
        if entity is None:
            raise RelationalContractError()
        if any(
            (field := _entity_field(entity, name)) is None or field.logical_type == "string_list"
            for name in index.fields
        ):
            raise RelationalContractError()
        definition = (index.entity, index.fields, index.unique)
        if definition in index_definitions:
            raise RelationalContractError()
        index_definitions.add(definition)


def _entity_field(entity: RelationalEntity, name: str) -> RelationalField | None:
    return next((field for field in entity.fields if field.name == name), None)


def _has_directed_cycle(graph: dict[str, set[str]]) -> bool:
    active: set[str] = set()
    complete: set[str] = set()

    def visit(node: str) -> bool:
        if node in active:
            return True
        if node in complete:
            return False
        active.add(node)
        if any(visit(target) for target in graph[node]):
            return True
        active.remove(node)
        complete.add(node)
        return False

    return any(visit(node) for node in graph)


def _detached_manifest(manifest: ManifestV2) -> ManifestV2:
    if type(manifest) is not ManifestV2:
        raise RelationalContractError()
    try:
        public = manifest.model_dump(mode="json", by_alias=True, warnings="error")
        detached = ManifestV2.model_validate(public)
    except Exception:
        raise RelationalContractError() from None
    if type(detached) is not ManifestV2:
        raise RelationalContractError()
    return detached


def _entity(name: str, attributes: list[AttributeDefinition]) -> RelationalEntity:
    return RelationalEntity(name=name, fields=tuple(_field(attribute) for attribute in attributes))


def _field(attribute: AttributeDefinition) -> RelationalField:
    return RelationalField(
        name=attribute.name,
        logical_type=attribute.type,
        required=attribute.required,
        primary_key=attribute.primary_key,
    )


def _history_is_one_entry_extension(current: ManifestV2, target: ManifestV2) -> bool:
    source_history = current.model_dump(mode="json", by_alias=True)["migration_history"]
    target_history = target.model_dump(mode="json", by_alias=True)["migration_history"]
    return (
        target_history[:-1] == source_history
        and target_history[-1]["from_schema_version"] == current.schema_version
        and target_history[-1]["to_schema_version"] == target.schema_version
    )


def _classify_entity_metadata(
    current: EntityDefinition,
    target: EntityDefinition,
    changes: list[MetadataChange],
) -> None:
    if current.description != target.description:
        changes.append(MetadataChange("entity_description", entity=current.name))


def _classify_attribute_change(
    entity: str,
    current: AttributeDefinition,
    target: AttributeDefinition,
    changes: list[MetadataChange],
) -> None:
    if (
        current.name != target.name
        or current.type != target.type
        or current.required != target.required
        or current.primary_key != target.primary_key
    ):
        raise RelationalContractError()
    if current.description != target.description:
        changes.append(MetadataChange("attribute_description", entity, current.name))
    if current.retrieval_mode != target.retrieval_mode:
        changes.append(MetadataChange("retrieval_mode", entity, current.name))
    _classify_allowed_values(entity, current, target, changes)


def _classify_allowed_values(
    entity: str,
    current: AttributeDefinition,
    target: AttributeDefinition,
    changes: list[MetadataChange],
) -> None:
    source_values = current.allowed_values
    target_values = target.allowed_values
    if source_values is None and target_values is None:
        return
    if source_values is None or target_values is None:
        raise RelationalContractError()
    source_set = {_typed_value(value) for value in source_values}
    target_set = {_typed_value(value) for value in target_values}
    if source_set == target_set:
        return
    if source_set < target_set:
        changes.append(MetadataChange("allowed_values_expansion", entity, current.name))
        return
    raise RelationalContractError()


def _typed_value(value: object) -> tuple[type[object], object]:
    return (type(value), value)


def _classify_global_metadata(
    current: ManifestV2, target: ManifestV2, changes: list[MetadataChange]
) -> None:
    if current.retrieval_guidance != target.retrieval_guidance:
        changes.append(MetadataChange("retrieval_guidance"))
    if current.query_patterns != target.query_patterns:
        changes.append(MetadataChange("query_patterns"))
    current_facets = [(facet.entity, facet.field) for facet in current.discovery_facets]
    target_facets = [(facet.entity, facet.field) for facet in target.discovery_facets]
    if current_facets != target_facets:
        changes.append(MetadataChange("discovery_facets"))


def _classify_relationships(
    current: ManifestV2, target: ManifestV2, new_entity_names: set[str]
) -> list[RelationalRelationship]:
    source_relationships = {relationship.name: relationship for relationship in current.relationships}
    target_relationships = {relationship.name: relationship for relationship in target.relationships}
    if not set(source_relationships) <= set(target_relationships):
        raise RelationalContractError()
    additions: list[RelationalRelationship] = []
    for name, source in source_relationships.items():
        target_relationship = target_relationships[name]
        if source.model_dump(mode="json", by_alias=True) != target_relationship.model_dump(
            mode="json", by_alias=True
        ):
            raise RelationalContractError()
    for name in sorted(set(target_relationships) - set(source_relationships)):
        relationship = target_relationships[name]
        if relationship.from_.entity not in new_entity_names:
            raise RelationalContractError()
        additions.append(
            RelationalRelationship(
                name=relationship.name,
                source_entity=relationship.from_.entity,
                source_field=relationship.from_.field,
                target_entity=relationship.to.entity,
                target_field=relationship.to.field,
                on_delete=relationship.on_delete,
            )
        )
    return additions


def _classify_indices(current: ManifestV2, target: ManifestV2) -> list[RelationalIndex]:
    source_indices = {index.name: index for index in current.indices}
    target_indices = {index.name: index for index in target.indices}
    if not set(source_indices) <= set(target_indices):
        raise RelationalContractError()
    additions: list[RelationalIndex] = []
    for name, source in source_indices.items():
        target_index = target_indices[name]
        if source.model_dump(mode="json") != target_index.model_dump(mode="json"):
            raise RelationalContractError()
    for name in sorted(set(target_indices) - set(source_indices)):
        index = target_indices[name]
        additions.append(
            RelationalIndex(
                name=index.name,
                entity=index.entity,
                fields=tuple(index.fields),
                unique=index.unique,
            )
        )
    return additions


def _metadata_sort_key(change: MetadataChange) -> tuple[str, str, str]:
    return (change.kind, change.entity or "", change.field or "")


def _identifier(value: object) -> bool:
    return type(value) is str and _IDENTIFIER.fullmatch(value) is not None
