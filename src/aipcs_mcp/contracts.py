"""Public request, response, capability, and transport contracts for AIPCS MCP."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from . import __version__
from .errors import AipcsContractError, ErrorCode, error_from_validation
from .manifest_v2 import RETIRED_INPUT_FIELDS, ManifestV2

CONTRACT_VERSION = "1.2.0"
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
_SERVICE_STORE_NAMESPACE = re.compile(r"^svc_[0-9a-f]{32}$")
_ZERO_SERVICE_STORE_NAMESPACE = f"svc_{'0' * 32}"


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


class ServiceSeedRequest(PublicModel):
    """Flat transport request for the durable seed operation."""

    domain_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$", max_length=63)
    domain_class: str = Field(min_length=1, max_length=64)
    intent_description: str = Field(min_length=1, max_length=1_000)
    idempotency_key: str = Field(min_length=1, max_length=128)


class ServiceListRequest(PublicModel):
    """Flat, deliberately small list request."""

    limit: StrictInt = Field(default=100, ge=1, le=100)


class ServiceInspectRequest(PublicModel):
    """Flat transport request for one service projection."""

    service_id: UUID

    @field_validator("service_id", mode="before")
    @classmethod
    def require_canonical_uuid(cls, value: object) -> object:
        _require_canonical_service_id(value)
        return value


class ServiceDesignRequest(PublicDesignRequest):
    """Extended durable-design request; legacy design intake remains unchanged."""

    idempotency_key: str = Field(min_length=1, max_length=128)


class ServiceMaterialiseRequest(PublicModel):
    """Strict public input for one idempotent initial materialisation."""

    service_id: UUID
    expected_service_revision: StrictInt = Field(ge=1, le=2**63 - 1)
    expected_schema_version: StrictInt = Field(ge=1, le=1)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("service_id", mode="before")
    @classmethod
    def require_canonical_uuid(cls, value: object) -> object:
        _require_canonical_service_id(value)
        return value


class ServiceEvolveRequest(PublicModel):
    """Strict public input for one complete, adjacent manifest evolution."""

    service_id: UUID
    expected_service_revision: StrictInt = Field(ge=1, le=2**63 - 1)
    expected_schema_version: StrictInt = Field(ge=1, le=64)
    idempotency_key: str = Field(min_length=1, max_length=128)
    schema_: ManifestV2 = Field(alias="schema", serialization_alias="schema")

    @field_validator("service_id", mode="before")
    @classmethod
    def require_canonical_uuid(cls, value: object) -> object:
        _require_canonical_service_id(value)
        return value

    @model_validator(mode="after")
    def require_adjacent_target(self) -> ServiceEvolveRequest:
        if self.schema_.schema_version != self.expected_schema_version + 1:
            raise ValueError("Evolve target must advance schema version by exactly one.")
        return self


class StorageSummary(PublicModel):
    """Safe storage metadata; a namespace is intentionally opaque to callers."""

    backend: Literal["sqlite", "postgresql"]
    namespace: str

    @field_validator("backend", mode="before")
    @classmethod
    def require_known_backend(cls, value: object) -> object:
        if type(value) is not str or value not in {"sqlite", "postgresql"}:
            raise ValueError("storage backend is invalid")
        return value

    @field_validator("namespace", mode="before")
    @classmethod
    def require_service_store_namespace(cls, value: object) -> object:
        if (
            type(value) is not str
            or not _SERVICE_STORE_NAMESPACE.fullmatch(value)
            or value == _ZERO_SERVICE_STORE_NAMESPACE
        ):
            raise ValueError("storage namespace is invalid")
        return value


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
    service_revision: StrictInt = Field(ge=1, le=2**63 - 1)
    recovery_state: Literal["clear", "pending", "recovery_required"]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    last_activity_at: AwareDatetime
    materialised_at: AwareDatetime | None = None
    storage: StorageSummary | None = None


class ServiceListResult(PublicModel):
    """Ordered, principal-scoped service projections without pagination metadata."""

    services: list[ServiceMetadata]


class ServerFeatures(PublicModel):
    server_info: bool = True
    manifest_v2_validation: bool = True
    legacy_v1_importer: bool = True
    stdio_preflight: bool = True
    registry_lifecycle: bool = False
    materialisation_lifecycle: bool = False
    record_runtime: bool = False
    discovery_topology: bool = False


class ServerInfo(PublicModel):
    server_name: Literal["aipcs-mcp"] = "aipcs-mcp"
    package_version: str = PACKAGE_VERSION
    aipcs_mcp_contract: str = CONTRACT_VERSION
    supported_manifest_versions: list[Literal[2]] = Field(default_factory=lambda: [2])
    transports: list[Literal["stdio", "streamable-http"]] = Field(
        default_factory=lambda: ["stdio", "streamable-http"]
    )
    features: ServerFeatures = Field(default_factory=ServerFeatures)
    operational_statuses: list[Literal["active"]] = Field(default_factory=lambda: ["active"])


def public_server_info(
    *,
    registry_lifecycle: bool = False,
    materialisation_lifecycle: bool = False,
    record_runtime: bool = False,
    discovery_topology: bool = False,
) -> ServerInfo:
    """Return a safe, snapshot-testable capability document."""

    if record_runtime != discovery_topology or (
        record_runtime and not (registry_lifecycle and materialisation_lifecycle)
    ):
        raise ValueError("The record and discovery runtime capabilities are all-or-nothing.")
    return ServerInfo(
        features=ServerFeatures(
            registry_lifecycle=registry_lifecycle,
            materialisation_lifecycle=materialisation_lifecycle,
            record_runtime=record_runtime,
            discovery_topology=discovery_topology,
        )
    )


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


def parse_service_design(payload: object) -> ServiceDesignRequest:
    """Parse durable design input with frozen compatibility-error precedence."""

    _validate_design_prefix(payload)
    try:
        return ServiceDesignRequest.model_validate(payload)
    except ValidationError as error:
        raise error_from_validation(error) from None


def parse_service_seed(payload: object) -> ServiceSeedRequest:
    return _parse_strict(ServiceSeedRequest, payload)


def parse_service_list(payload: object) -> ServiceListRequest:
    return _parse_strict(ServiceListRequest, payload)


def parse_service_inspect(payload: object) -> ServiceInspectRequest:
    if not isinstance(payload, Mapping):
        raise AipcsContractError(ErrorCode.INVALID_REQUEST, "Request input must be a JSON object.")
    if "service_id" in payload:
        _require_canonical_service_id(payload["service_id"])
    return _parse_strict(ServiceInspectRequest, payload)


def parse_service_materialise(payload: object) -> ServiceMaterialiseRequest:
    """Parse a complete materialise request before any lifecycle admission."""

    _validate_lifecycle_prefix(payload, require_schema=False)
    return _parse_lifecycle(ServiceMaterialiseRequest, payload)


def parse_service_evolve(payload: object) -> ServiceEvolveRequest:
    """Parse a detached, complete manifest target before lifecycle admission."""

    _validate_lifecycle_prefix(payload, require_schema=True)
    return _parse_lifecycle(ServiceEvolveRequest, payload)


def _parse_strict[T: PublicModel](model: type[T], payload: object) -> T:
    if not isinstance(payload, Mapping):
        raise AipcsContractError(ErrorCode.INVALID_REQUEST, "Request input must be a JSON object.")
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise error_from_validation(error) from None


def _parse_lifecycle[T: PublicModel](model: type[T], payload: object) -> T:
    """Validate and deep-detach lifecycle input at the public boundary."""

    try:
        return model.model_validate(payload).model_copy(deep=True)
    except ValidationError as error:
        raise error_from_validation(error) from None


def _validate_design_prefix(payload: object) -> None:
    if not isinstance(payload, Mapping):
        raise AipcsContractError(ErrorCode.INVALID_REQUEST, "Design input must be a JSON object.")
    if "service_id" in payload:
        _require_canonical_service_id(payload["service_id"])
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


def _validate_lifecycle_prefix(payload: object, *, require_schema: bool) -> None:
    """Apply manifest compatibility precedence without design-only restrictions."""

    if not isinstance(payload, Mapping):
        raise AipcsContractError(ErrorCode.INVALID_REQUEST, "Request input must be a JSON object.")
    if "service_id" in payload:
        _require_canonical_service_id(payload["service_id"])
    _reject_retired_fields(payload)
    if not require_schema:
        return
    schema = payload.get("schema")
    if not isinstance(schema, Mapping):
        return
    manifest_version = schema.get("manifest_version")
    if manifest_version == 1:
        raise AipcsContractError(
            ErrorCode.LEGACY_IMPORT_REQUIRED,
            "Manifest version 1 is private legacy input; use the explicit legacy importer.",
        )
    if manifest_version != 2:
        raise AipcsContractError(
            ErrorCode.MANIFEST_VERSION_UNSUPPORTED,
            "Public lifecycle intake supports manifest_version 2 only.",
        )


def _require_canonical_service_id(value: object) -> None:
    try:
        canonical = str(UUID(value)) if isinstance(value, str) else None
    except ValueError:
        canonical = None
    if canonical != value or canonical == str(UUID(int=0)):
        raise AipcsContractError(
            ErrorCode.INVALID_IDENTIFIER,
            "service_id must be a non-zero lowercase canonical UUID.",
        )


def validate_transport_environment(
    transport: str | None = None, environ: Mapping[str, str] | None = None
) -> Literal["stdio", "streamable-http"] | None:
    """Reject undocumented listener settings before configuration resolution.

    The AIPCS configuration resolver owns the supported transport and listener
    settings.  Ambient FastMCP and retired AIPCS listener variables must never
    silently alter a process launched by an editor, container, or service manager.
    """

    environment = os.environ if environ is None else environ
    invalid_cli_transport = transport not in {None, "stdio", "streamable-http"}
    retired_transport_configured = environment.get("AIPCS_MCP_TRANSPORT") not in {None, ""}
    listener_configured = any(environment.get(key) for key in LISTENER_ENV_KEYS)
    if invalid_cli_transport or retired_transport_configured or listener_configured:
        raise AipcsContractError(
            ErrorCode.TRANSPORT_NOT_SUPPORTED,
            "Use documented AIPCS transport configuration; legacy listener settings are disabled.",
        )
    return transport  # type: ignore[return-value]


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
