"""Immutable internal lifecycle values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from aipcs_mcp.contracts import ServiceMetadata
from aipcs_mcp.manifest_v2 import ManifestV2


@dataclass(frozen=True)
class ApplicationContext:
    principal_id: str
    created_via: str


@dataclass(frozen=True)
class SeedCommand:
    domain_name: str
    domain_class: str
    intent_description: str
    idempotency_key: str


@dataclass(frozen=True)
class DesignCommand:
    service_id: UUID
    manifest: ManifestV2
    idempotency_key: str


@dataclass(frozen=True)
class Service:
    service_id: UUID
    principal_id: str
    domain_name: str
    domain_class: str
    intent_description: str
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime
    manifest: ManifestV2 | None = None
    schema_version: int | None = None
    design_state: Literal["seeded", "materialised"] = "seeded"
    operational_status: Literal["active", "suspended", "archived"] = "active"


@dataclass(frozen=True)
class AuditEvent:
    action: str
    outcome: str
    service_id: UUID
    principal_id: str
    created_via: str
    at: datetime


@dataclass(frozen=True)
class MutationClaim:
    status: Literal["new", "replay", "conflict"]
    result: Service | None = None


def project(service: Service) -> ServiceMetadata:
    return ServiceMetadata(
        service_id=service.service_id,
        domain_name=service.domain_name,
        domain_class=service.domain_class,
        intent_description=service.intent_description,
        design_state=service.design_state,
        operational_status=service.operational_status,
        schema=service.manifest.model_copy(deep=True) if service.manifest is not None else None,
        schema_version=service.schema_version,
        created_at=service.created_at,
        updated_at=service.updated_at,
        last_activity_at=service.last_activity_at,
    )
