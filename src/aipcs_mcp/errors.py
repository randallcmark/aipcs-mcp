"""Structured, safe public error envelopes."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    INVALID_IDENTIFIER = "invalid_identifier"
    VALIDATION_FAILED = "validation_failed"
    RETIRED_FIELD = "retired_field"
    MANIFEST_VERSION_UNSUPPORTED = "manifest_version_unsupported"
    LEGACY_IMPORT_REQUIRED = "legacy_import_required"
    NOT_FOUND = "not_found"
    INVALID_STATE = "invalid_state"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    CONFLICT = "conflict"
    UNSUPPORTED_TRANSITION = "unsupported_transition"
    STALE_REVISION = "stale_revision"
    CHANGED_FINGERPRINT = "changed_fingerprint"
    OPERATION_IN_PROGRESS = "operation_in_progress"
    RECOVERY_REQUIRED = "recovery_required"
    STORAGE_BUSY = "storage_busy"
    OPERATION_UNCERTAIN = "operation_uncertain"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    FORBIDDEN = "forbidden"
    TRANSPORT_NOT_SUPPORTED = "transport_not_supported"
    INTERNAL_ERROR = "internal_error"


class ErrorIssue(PublicModel):
    code: str = Field(min_length=1, max_length=96)
    path: str = Field(min_length=1, max_length=240)
    message: str = Field(min_length=1, max_length=500)
    remediation: str | None = Field(default=None, max_length=500)


class ErrorDetail(PublicModel):
    code: ErrorCode
    message: str = Field(min_length=1, max_length=500)
    issues: list[ErrorIssue] = Field(default_factory=list, max_length=32)
    retryable: bool = False


class SuccessEnvelope(PublicModel):
    ok: Literal[True] = True
    result: dict[str, Any]
    error: None = None


class FailureEnvelope(PublicModel):
    ok: Literal[False] = False
    result: None = None
    error: ErrorDetail


class AipcsContractError(ValueError):
    """A boundary failure that can be safely returned to MCP or the CLI."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        issues: list[ErrorIssue] | None = None,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.message = _bounded_message(message)
        self.issues = issues or []
        self.retryable = retryable
        super().__init__(self.message)

    def envelope(self) -> FailureEnvelope:
        return FailureEnvelope(
            error=ErrorDetail(
                code=self.code,
                message=self.message,
                issues=self.issues,
                retryable=self.retryable,
            )
        )


def success(result: dict[str, Any]) -> SuccessEnvelope:
    return SuccessEnvelope(result=result)


def error_from_validation(error: ValidationError) -> AipcsContractError:
    """Remove Pydantic internals and input echoes from validation output."""

    issues = [_safe_issue(item) for item in error.errors()[:32]]
    return AipcsContractError(
        ErrorCode.VALIDATION_FAILED,
        "Request failed public contract validation.",
        issues=issues,
    )


def _safe_issue(item: dict[str, Any]) -> ErrorIssue:
    """Map validator-specific details onto the stable public issue vocabulary."""

    validator_code = str(item.get("type", ""))
    if validator_code == "missing":
        code, message, remediation = (
            "field_required",
            "A required field is missing.",
            "Provide the required field and retry.",
        )
    elif validator_code == "extra_forbidden":
        code, message, remediation = (
            "unknown_field",
            "The request contains an unsupported field.",
            "Remove the unsupported field and retry.",
        )
    elif "too_long" in validator_code or "too_short" in validator_code:
        code, message, remediation = (
            "invalid_length",
            "The field has an invalid number of items or characters.",
            "Adjust the field length to the documented bound and retry.",
        )
    else:
        code, message, remediation = (
            "invalid_value",
            "The field value is invalid.",
            "Use a value allowed by the public contract and retry.",
        )
    return ErrorIssue(
        code=code,
        path=_safe_path(item.get("loc", ())),
        message=message,
        remediation=remediation,
    )


def _safe_path(location: object) -> str:
    parts: list[str] = []
    if isinstance(location, (tuple, list)):
        for part in location:
            if isinstance(part, int):
                parts.append(str(part))
                continue
            text = str(part)
            parts.append(text if text.isidentifier() and len(text) <= 64 else "<field>")
    path = ".".join(parts) or "request"
    return path[:240]


def _bounded_message(message: object) -> str:
    normalised = " ".join(str(message).split())
    return (normalised or "The request could not be processed.")[:500]
