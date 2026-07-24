"""Shared logical encoding for the service-local write-admission control row."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from aipcs_mcp.application.portable_store import WriteAdmissionFence
from aipcs_mcp.storage.errors import StorageMigrationError

FENCE_IDEMPOTENCY_KEY = "__aipcs_write_admission"
FENCE_OPERATION_KIND = "write_admission"
FENCE_FINGERPRINT = hashlib.sha256(b"aipcs-write-admission-v1").hexdigest()
INITIAL_FENCE = WriteAdmissionFence(1, "open")


def encode_fence(value: WriteAdmissionFence) -> str:
    if type(value) is not WriteAdmissionFence:
        raise StorageMigrationError()
    return json.dumps(
        {"generation": value.generation, "state": value.state},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_fence(value: object) -> WriteAdmissionFence:
    try:
        if isinstance(value, str):
            raw = json.loads(value)
            if encode_fence(
                WriteAdmissionFence(raw["generation"], raw["state"])
            ) != value:
                raise ValueError
        elif isinstance(value, Mapping):
            raw = dict(value)
        else:
            raise ValueError
        if set(raw) != {"generation", "state"}:
            raise ValueError
        return WriteAdmissionFence(raw["generation"], raw["state"])
    except Exception:
        raise StorageMigrationError() from None
