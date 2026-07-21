"""Public request, response, capability, and transport contracts for AIPCS MCP."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from . import __version__
from .errors import AipcsContractError, ErrorCode, error_from_validation
from .manifest_v2 import RETIRED_INPUT_FIELDS, ManifestV2

CONTRACT_VERSION = "1.0"
PACKAGE_VERSION = __version__
TRANSPORT_ENV_KEYS = ("AIPCS_TRANSPORT", "AIPCS_MCP_TRANSPORT")
LISTENER_ENV_KEYS = (
    "FASTMCP_HOST",
    "FASTMCP_PORT",
    "FASTMCP_MOUNT_PATH",
    "FASTMCP_SSE_PATH",
    "FASTMCP_MESSAGE_PATH",
    "FASTMCP_STREAMABLE_HTTP_PATH",
    "AIPCS_HOST",
    "AIPCS_PORT",
    "AIPCS_MOUNT_PATH",
    "AIPCS_MCP_HOST",
    "AIPCS_MCP_PORT",
    "AIPCS_MCP_MOUNT_PATH",
)


class PublicModel(BaseModel):
    """Base model for public JSON documents: no undocumented input is accepted."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, serialize_by_alias=True)


class PublicDesignRequest(PublicModel):
    """The only normal schema-design intake shape accepted by public v1."""

    service_id: UUID
    schema_: ManifestV2 = Field(alias="schema", serialization_alias="schema")

    @field_validator("service_id", mode="before")
    @classmethod
    def require_canonical_uuid(cls, value: object) -> object:
        if isinstance(value, str) and str(UUID(value)) != value:
            raise ValueError("service_id must be a lowercase canonical UUID.")
        return value

    @model_validator(mode="after")
    def require_initial_schema(self) -> PublicDesignRequest:
        if self.schema_.schema_version != 1 or self.schema_.migration_history:
            raise ValueError(
                "Initial public design requires schema_version 1 and empty migration_history."
            )
        return self


class StorageSummary(PublicModel):
    """Safe storage metadata; a namespace is intentionally opaque to callers."""

    backend: Literal["sqlite", "postgresql"]
    namespace: str = Field(pattern=r"^svc_[a-z0-9][a-z0-9_-]{0,95}$")


class ServiceMetadata(PublicModel):
    """Public service fields. Never add owner, DSN, path, credential, or endpoint here."""

    service_id: UUID
    domain_name: str = Field(min_length=1, max_length=96)
    domain_class: str = Field(min_length=1, max_length=64)
    intent_description: str = Field(min_length=1, max_length=1_000)
    design_state: Literal["seeded", "materialised"]
    operational_status: Literal["active", "suspended", "archived"] = "active"
    schema_: ManifestV2 | None = Field(default=None, alias="schema", serialization_alias="schema")
    schema_version: int | None = Field(default=None, ge=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    last_activity_at: AwareDatetime
    materialised_at: AwareDatetime | None = None
    storage: StorageSummary | None = None


class ServerFeatures(PublicModel):
    server_info: bool = True
    manifest_v2_validation: bool = True
    legacy_v1_importer: bool = True
    stdio_preflight: bool = True


class ServerInfo(PublicModel):
    server_name: Literal["aipcs-mcp"] = "aipcs-mcp"
    package_version: str = PACKAGE_VERSION
    aipcs_mcp_contract: str = CONTRACT_VERSION
    supported_manifest_versions: list[Literal[2]] = Field(default_factory=lambda: [2])
    transports: list[Literal["stdio"]] = Field(default_factory=lambda: ["stdio"])
    features: ServerFeatures = Field(default_factory=ServerFeatures)
    operational_statuses: list[Literal["active"]] = Field(default_factory=lambda: ["active"])


def public_server_info() -> ServerInfo:
    """Return a safe, snapshot-testable capability document."""

    return ServerInfo()


def parse_public_design(payload: object) -> PublicDesignRequest:
    """Parse normal design intake and turn boundary failures into stable errors.

    The legacy importer is deliberately not used here. Version 1 schemas must be
    explicitly imported before callers submit a public design request.
    """

    if not isinstance(payload, Mapping):
        raise AipcsContractError(ErrorCode.INVALID_REQUEST, "Design input must be a JSON object.")
    if "service_id" not in payload:
        raise AipcsContractError(ErrorCode.INVALID_REQUEST, "Design input requires service_id.")
    service_id = payload.get("service_id")
    try:
        canonical_service_id = str(UUID(service_id)) if isinstance(service_id, str) else None
    except ValueError:
        canonical_service_id = None
    if canonical_service_id != service_id:
        raise AipcsContractError(
            ErrorCode.INVALID_IDENTIFIER,
            "service_id must be a lowercase canonical UUID.",
        )
    _reject_retired_fields(payload)
    schema = payload.get("schema")
    if isinstance(schema, Mapping):
        manifest_version = schema.get("manifest_version")
        if manifest_version == 1:
            raise AipcsContractError(
                ErrorCode.LEGACY_IMPORT_REQUIRED,
                "Manifest version 1 is private legacy input; use the explicit legacy importer.",
            )
        if manifest_version != 2:
            raise AipcsContractError(
                ErrorCode.MANIFEST_VERSION_UNSUPPORTED,
                "Public design intake supports manifest_version 2 only.",
            )
    try:
        return PublicDesignRequest.model_validate(payload)
    except ValidationError as error:
        raise error_from_validation(error) from None


def validate_stdio_only(
    transport: str | None = None, environ: Mapping[str, str] | None = None
) -> Literal["stdio"]:
    """Reject all listener-oriented configuration before MCP server construction."""

    environment = os.environ if environ is None else environ
    invalid_cli_transport = transport not in {None, "stdio"}
    invalid_environment_transport = any(
        environment.get(key) not in {None, "", "stdio"} for key in TRANSPORT_ENV_KEYS
    )
    listener_configured = any(environment.get(key) for key in LISTENER_ENV_KEYS)
    if invalid_cli_transport or invalid_environment_transport or listener_configured:
        raise AipcsContractError(
            ErrorCode.TRANSPORT_NOT_SUPPORTED,
            "Public v1 supports stdio only; listener transports and listener settings are disabled.",
        )
    return "stdio"


def _reject_retired_fields(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key) in RETIRED_INPUT_FIELDS:
                raise AipcsContractError(
                    ErrorCode.RETIRED_FIELD,
                    "The request contains a field retired from the public v1 contract.",
                )
            _reject_retired_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_retired_fields(nested)
