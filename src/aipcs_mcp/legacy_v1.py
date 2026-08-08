"""One-way conversion of private, synthetic manifest-v1 documents into v2."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import AipcsContractError, ErrorCode, ErrorIssue, error_from_validation
from .manifest_v2 import (
    RETIRED_INPUT_FIELDS,
    SERVER_MANAGED_FIELDS,
    ManifestV2,
)


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LegacyWarning(PublicModel):
    code: str = Field(min_length=1, max_length=96)
    message: str = Field(min_length=1, max_length=500)


class LegacyImportProvenance(PublicModel):
    source: Literal["legacy_v1"] = "legacy_v1"
    source_manifest_version: Literal[1] = 1
    importer: Literal["aipcs"] = "aipcs"


class LegacyImportResult(PublicModel):
    manifest: ManifestV2
    provenance: LegacyImportProvenance = Field(default_factory=LegacyImportProvenance)
    warnings: list[LegacyWarning] = Field(default_factory=list, max_length=64)
    discarded_fields: list[str] = Field(default_factory=list, max_length=64)


_CONVERTED_SOURCE_FIELDS = {
    "manifest_version",
    "schema_version",
    "entities",
    "relationships",
    "indices",
    "query_patterns",
    "discovery_facets",
    "retrieval_guidance",
}
_RETIRED_OR_PRIVATE_FIELDS = set(RETIRED_INPUT_FIELDS) | {
    "tool_definitions",
    "tools",
    "aliases",
    "classification_confidence",
    "parent_service_id",
    "session_count",
    "endpoint",
    "endpoints",
    "database_url",
    "sqlite_path",
    "storage_path",
    "materialisation",
    "materialization",
    "migration_history",
}
_TYPE_ALIASES = {"float": "number"}


def import_legacy_v1(source: Mapping[str, Any]) -> LegacyImportResult:
    """Convert a private v1 manifest without making v1 a public runtime shape.

    This intentionally accepts only synthetic mapping input. It does not open a
    database, execute embedded tool definitions, retain endpoint details, or infer
    relational constraints/migration history.
    """

    if not isinstance(source, Mapping):
        raise AipcsContractError(ErrorCode.INVALID_REQUEST, "Legacy input must be a JSON object.")
    if source.get("manifest_version") != 1:
        raise AipcsContractError(
            ErrorCode.MANIFEST_VERSION_UNSUPPORTED,
            "The legacy importer accepts manifest_version 1 only.",
        )
    legacy = deepcopy(dict(source))
    unknown_fields = sorted(
        str(key)
        for key in legacy
        if key not in _CONVERTED_SOURCE_FIELDS and key not in _RETIRED_OR_PRIVATE_FIELDS
    )
    if unknown_fields:
        raise AipcsContractError(
            ErrorCode.VALIDATION_FAILED,
            "Legacy input contains an unrecognised field and was not converted.",
            issues=[
                ErrorIssue(
                    code="legacy_field_unrecognised",
                    path="legacy_manifest",
                    message="Every legacy field must be converted, explicitly discarded, or removed.",
                    remediation="Remove unrecognised fields and retry the explicit legacy import.",
                )
            ],
        )
    discarded = sorted(key for key in legacy if key in _RETIRED_OR_PRIVATE_FIELDS)
    for key in discarded:
        legacy.pop(key, None)

    relationships = _as_list(legacy.get("relationships", []), "relationships")
    if relationships:
        raise AipcsContractError(
            ErrorCode.VALIDATION_FAILED,
            "Legacy relationships are not imported because their constraints are ambiguous.",
            issues=[
                ErrorIssue(
                    code="legacy_relationship_ambiguous",
                    path="relationships",
                    message="Non-empty private-v1 relationships have no portable v2 interpretation.",
                    remediation="Remove the relationship or recreate it as an explicit v2 foreign key.",
                )
            ],
        )

    warnings: list[LegacyWarning] = []
    legacy_schema_version = legacy.get("schema_version", 1)
    if type(legacy_schema_version) is not int or legacy_schema_version < 1:
        raise AipcsContractError(
            ErrorCode.VALIDATION_FAILED,
            "Legacy schema_version must be a positive integer.",
        )
    if legacy_schema_version != 1:
        warnings.append(
            LegacyWarning(
                code="legacy_schema_version_reset",
                message=(
                    "Legacy schema_version was reset to 1 because the importer does not invent "
                    "public-v2 migration history."
                ),
            )
        )
    if "migration_history" in discarded:
        warnings.append(
            LegacyWarning(
                code="legacy_migration_history_discarded",
                message="Legacy migration history was discarded; the importer does not invent v2 history.",
            )
        )

    try:
        entities = [
            _convert_entity(raw, warnings)
            for raw in _as_list(legacy.get("entities", []), "entities")
        ]
        facets = _convert_facets(legacy.get("discovery_facets", []), entities)
        manifest = ManifestV2.model_validate(
                {
                    "manifest_version": 2,
                    "schema_version": 1,
                "entities": entities,
                "relationships": [],
                "indices": legacy.get("indices", []),
                "query_patterns": legacy.get("query_patterns", []),
                "discovery_facets": facets,
                "retrieval_guidance": legacy.get("retrieval_guidance"),
                "migration_history": [],
            }
        )
    except ValidationError as error:
        raise error_from_validation(error) from None

    if discarded:
        warnings.append(
            LegacyWarning(
                code="legacy_fields_discarded",
                message="Private-v1-only fields were removed during one-way import.",
            )
        )
    return LegacyImportResult(manifest=manifest, warnings=warnings, discarded_fields=discarded)


def _as_list(value: object, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise AipcsContractError(
            ErrorCode.VALIDATION_FAILED, f"Legacy {field_name} must be a list."
        )
    return value


def _convert_entity(raw: object, warnings: list[LegacyWarning]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise AipcsContractError(
            ErrorCode.VALIDATION_FAILED, "Every legacy entity must be an object."
        )
    attributes = raw.get("attributes", raw.get("fields"))
    if not isinstance(attributes, list):
        raise AipcsContractError(
            ErrorCode.VALIDATION_FAILED,
            "Every legacy entity must provide an attributes or fields list.",
        )
    converted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_attribute in attributes:
        if not isinstance(raw_attribute, Mapping):
            raise AipcsContractError(
                ErrorCode.VALIDATION_FAILED, "Legacy attributes must be objects."
            )
        name = raw_attribute.get("name")
        field_type = raw_attribute.get("type", raw_attribute.get("data_type"))
        if isinstance(field_type, str) and field_type in _TYPE_ALIASES:
            field_type = _TYPE_ALIASES[field_type]
            warnings.append(
                LegacyWarning(
                    code="legacy_type_normalised",
                    message=f"Converted legacy float field {name!r} to public number.",
                )
            )
        if not isinstance(name, str) or not isinstance(field_type, str):
            raise AipcsContractError(
                ErrorCode.VALIDATION_FAILED,
                "Legacy attributes require string name and type/data_type values.",
            )
        seen.add(name)
        if name in SERVER_MANAGED_FIELDS:
            normalised = {"name": name, **SERVER_MANAGED_FIELDS[name]}
            converted.append(normalised)
            comparable_legacy = {
                "name": name,
                "type": field_type,
                "required": bool(raw_attribute.get("required", False)),
                "primary_key": bool(raw_attribute.get("primary_key", name == "id")),
            }
            if comparable_legacy != normalised or any(
                raw_attribute.get(key) is not None
                for key in ("description", "allowed_values", "retrieval_mode")
            ):
                warnings.append(
                    LegacyWarning(
                        code="server_managed_field_normalised",
                        message=f"Normalised legacy server-managed field {name!r} to its v2 declaration.",
                    )
                )
            continue
        converted_attribute = {
            "name": name,
            "type": field_type,
            "required": bool(raw_attribute.get("required", False)),
            "primary_key": bool(raw_attribute.get("primary_key", False)),
            "description": raw_attribute.get("description"),
        }
        for optional_key in ("allowed_values", "retrieval_mode"):
            if optional_key in raw_attribute:
                converted_attribute[optional_key] = raw_attribute[optional_key]
        converted.append(converted_attribute)
    for name, definition in SERVER_MANAGED_FIELDS.items():
        if name not in seen:
            converted.append({"name": name, **definition})
            warnings.append(
                LegacyWarning(
                    code="server_managed_field_injected",
                    message=f"Injected required public server-managed field {name!r}.",
                )
            )
    return {"name": raw.get("name"), "attributes": converted, "description": raw.get("description")}


def _convert_facets(raw_facets: object, entities: list[dict[str, Any]]) -> list[dict[str, str]]:
    facets = _as_list(raw_facets, "discovery_facets")
    candidates: dict[str, list[str]] = {}
    for entity in entities:
        for attribute in entity["attributes"]:
            candidates.setdefault(attribute["name"], []).append(entity["name"])
    converted: list[dict[str, str]] = []
    for facet in facets:
        if isinstance(facet, Mapping):
            converted.append({"entity": facet.get("entity"), "field": facet.get("field")})
            continue
        if not isinstance(facet, str):
            raise AipcsContractError(
                ErrorCode.VALIDATION_FAILED, "Legacy discovery facets must be strings or objects."
            )
        if "." in facet:
            entity, field = facet.split(".", 1)
            converted.append({"entity": entity, "field": field})
            continue
        matches = candidates.get(facet, [])
        if len(matches) != 1:
            raise AipcsContractError(
                ErrorCode.VALIDATION_FAILED,
                "A legacy discovery facet is ambiguous; use an explicit entity.field object.",
            )
        converted.append({"entity": matches[0], "field": facet})
    return converted
