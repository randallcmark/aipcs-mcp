"""Canonical detached storage codecs for the SQLite registry."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from aipcs_mcp.application.models import Service
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.storage.errors import StorageMigrationError

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_RESULT_KEYS = {
    "service_id",
    "principal_id",
    "domain_name",
    "domain_class",
    "intent_description",
    "created_at",
    "updated_at",
    "last_activity_at",
    "manifest",
    "schema_version",
    "design_state",
    "operational_status",
}


def encode_time(value: datetime) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is not UTC
        or value.utcoffset() != timedelta(0)
    ):
        raise StorageMigrationError()
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def decode_time(value: object) -> datetime:
    try:
        if type(value) is not str or len(value) != 27:
            raise ValueError
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        if encode_time(parsed) != value:
            raise ValueError
    except Exception:
        pass
    else:
        return parsed
    raise StorageMigrationError() from None


def encode_service_values(service: Service) -> tuple[Any, ...]:
    checked = validate_service(service)
    manifest = _manifest_json(checked.manifest)
    return (
        str(checked.service_id),
        checked.principal_id,
        checked.domain_name,
        checked.domain_class,
        checked.intent_description,
        encode_time(checked.created_at),
        encode_time(checked.updated_at),
        encode_time(checked.last_activity_at),
        manifest,
        checked.schema_version,
        checked.design_state,
        checked.operational_status,
    )


def decode_service(row: Mapping[str, Any]) -> Service:
    try:
        manifest_raw = row["manifest_json"]
        manifest = _decode_manifest(manifest_raw)
        value = Service(
            _uuid(row["service_id"]),
            _text(row["principal_id"], 128),
            _domain(row["domain_name"]),
            _text(row["domain_class"], 64),
            _text(row["intent_description"], 1000),
            decode_time(row["created_at"]),
            decode_time(row["updated_at"]),
            decode_time(row["last_activity_at"]),
            manifest,
            row["schema_version"],
            row["design_state"],
            row["operational_status"],
        )
        value = validate_service(value)
    except Exception:
        pass
    else:
        return value
    raise StorageMigrationError() from None


def encode_result(service: Service) -> str:
    value = validate_service(service)
    return _json(
        {
            "service_id": str(value.service_id),
            "principal_id": value.principal_id,
            "domain_name": value.domain_name,
            "domain_class": value.domain_class,
            "intent_description": value.intent_description,
            "created_at": encode_time(value.created_at),
            "updated_at": encode_time(value.updated_at),
            "last_activity_at": encode_time(value.last_activity_at),
            "manifest": None
            if value.manifest is None
            else value.manifest.model_dump(mode="json", by_alias=True, warnings="error"),
            "schema_version": value.schema_version,
            "design_state": value.design_state,
            "operational_status": value.operational_status,
        }
    )


def decode_result(text: object, principal_id: str, service_id: object) -> Service:
    try:
        if type(text) is not str:
            raise ValueError
        raw = json.loads(text)
        if type(raw) is not dict or set(raw) != _RESULT_KEYS:
            raise ValueError
        manifest = None if raw["manifest"] is None else ManifestV2.model_validate(raw["manifest"])
        value = Service(
            _uuid(raw["service_id"]),
            _text(raw["principal_id"], 128),
            _domain(raw["domain_name"]),
            _text(raw["domain_class"], 64),
            _text(raw["intent_description"], 1000),
            decode_time(raw["created_at"]),
            decode_time(raw["updated_at"]),
            decode_time(raw["last_activity_at"]),
            manifest,
            raw["schema_version"],
            raw["design_state"],
            raw["operational_status"],
        )
        value = validate_service(value)
        if value.principal_id != principal_id or value.service_id != _uuid(service_id):
            raise ValueError
        if encode_result(value) != text:
            raise ValueError
        result = value
    except Exception:
        pass
    else:
        return result
    raise StorageMigrationError() from None


def validate_service(value: Service) -> Service:
    try:
        if not isinstance(value, Service):
            raise ValueError
        if not isinstance(value.service_id, UUID):
            raise ValueError
        _uuid(str(value.service_id))
        _text(value.principal_id, 128)
        _domain(value.domain_name)
        _text(value.domain_class, 64)
        _text(value.intent_description, 1000)
        encode_time(value.created_at)
        encode_time(value.updated_at)
        encode_time(value.last_activity_at)
        if value.design_state not in {"seeded", "materialised"}:
            raise ValueError
        if value.operational_status not in {"active", "suspended", "archived"}:
            raise ValueError
        if (value.manifest is None) != (value.schema_version is None):
            raise ValueError
        if value.manifest is not None:
            ManifestV2.model_validate(value.manifest.model_dump(mode="json", by_alias=True))
            if value.schema_version != value.manifest.schema_version:
                raise ValueError
        if value.schema_version is not None and (
            type(value.schema_version) is not int or value.schema_version < 1
        ):
            raise ValueError
    except Exception:
        pass
    else:
        return value
    raise StorageMigrationError() from None


def _uuid(value: object) -> UUID:
    if type(value) is not str:
        raise ValueError
    parsed = UUID(value)
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError
    return parsed


def _text(value: object, maximum: int) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > maximum
        or not value.isprintable()
    ):
        raise ValueError
    return value


def _domain(value: object) -> str:
    value = _text(value, 63)
    if not _NAME.fullmatch(value):
        raise ValueError
    return value


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _manifest_json(value: ManifestV2 | None) -> str | None:
    if value is None:
        return None
    return _json(value.model_dump(mode="json", by_alias=True, warnings="error"))


def _decode_manifest(value: object) -> ManifestV2 | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError
    raw = json.loads(value)
    manifest = ManifestV2.model_validate(raw)
    if _manifest_json(manifest) != value:
        raise ValueError
    return manifest
