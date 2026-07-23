"""Private canonical PostgreSQL data-core primitives; no topology or discovery."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from aipcs_mcp.records import FrozenJsonObject, PageCursor, RecordContractError
from aipcs_mcp.storage.errors import StorageBusy, StorageMigrationError, StorageUnavailable

from .result_codes import is_postgresql_busy


def canonical_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        raise StorageUnavailable() from None


def fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def cursor(identity: str, record_id: UUID) -> PageCursor:
    raw = canonical_json({"i": identity, "r": str(record_id)}).encode("utf-8")
    return PageCursor(base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii"))


def cursor_record(value: PageCursor | None, identity: str) -> UUID | None:
    if value is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(value.value + "=" * (-len(value.value) % 4))
        decoded = json.loads(raw.decode("utf-8"))
        if type(decoded) is not dict or decoded.get("i") != identity or type(decoded.get("r")) is not str:
            raise ValueError
        parsed = UUID(decoded["r"])
        if str(parsed) != decoded["r"] or parsed.int == 0:
            raise ValueError
        return parsed
    except Exception:
        raise RecordContractError() from None


def json_object(value: object) -> FrozenJsonObject:
    if type(value) is not dict:
        raise StorageUnavailable()
    try:
        return FrozenJsonObject.from_mapping(value)
    except Exception:
        raise StorageUnavailable() from None


def utc(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise StorageUnavailable()
    return value


def failure(error: BaseException, *, commit_started: bool) -> StorageBusy | StorageMigrationError | StorageUnavailable:
    if commit_started:
        return StorageMigrationError()
    state = getattr(error, "sqlstate", None)
    if type(state) is str and state.startswith("08"):
        return StorageUnavailable()
    return StorageBusy() if is_postgresql_busy(error) else StorageMigrationError()


def quote(identifier: str) -> str:
    """Quote an already contract-validated PostgreSQL identifier."""

    if type(identifier) is not str or not identifier or "\x00" in identifier:
        raise StorageUnavailable()
    return '"' + identifier.replace('"', '""') + '"'


def qualified(namespace: str, identifier: str) -> str:
    """Return one schema-qualified, never-search-path-selected relation."""

    return f"{quote(namespace)}.{quote(identifier)}"


def canonical_json_value(value: object) -> object:
    """Decode a database JSON value and prove its canonical wire form."""

    if type(value) is str:
        try:
            decoded = json.loads(value)
        except Exception:
            raise StorageUnavailable() from None
    else:
        decoded = value
    # psycopg normally returns native values for jsonb.  Text is accepted only
    # when it is the canonical representation we wrote.
    if type(value) is str and value != canonical_json(decoded):
        raise StorageUnavailable()
    return decoded
