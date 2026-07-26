"""Safe, transport-neutral configuration failures."""

from __future__ import annotations

import re

from aipcs_mcp.errors import AipcsContractError, ErrorCode, ErrorIssue

_SQLITE_VERSION = re.compile(r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$")


class ConfigurationError(Exception):
    """Never retains untrusted values, paths, TOML, or secrets."""

    def __init__(
        self,
        code: ErrorCode = ErrorCode.VALIDATION_FAILED,
        path: str = "configuration",
        *,
        sqlite_version: str | None = None,
    ):
        self.code = code
        self.message = "Configuration is invalid or unavailable."
        self.path = path if path.isidentifier() and len(path) <= 64 else "configuration"
        self.sqlite_version = (
            sqlite_version if isinstance(sqlite_version, str) and _SQLITE_VERSION.fullmatch(sqlite_version) else None
        )
        super().__init__(self.message)


def to_contract_error(error: Exception) -> AipcsContractError:
    """Map configuration failures without retaining or exposing their input."""

    if isinstance(error, ConfigurationError):
        if error.code == ErrorCode.TRANSPORT_NOT_SUPPORTED:
            return AipcsContractError(
                error.code,
                "The selected transport is not supported by the documented configuration.",
            )
        if error.code == ErrorCode.UNSUPPORTED_OPERATION and error.path == "sqlite_runtime":
            detected = error.sqlite_version or "an unrecognised version"
            return AipcsContractError(
                error.code,
                "SQLite storage requires SQLite 3.51.3 or newer because older runtimes contain the WAL-reset data-integrity defect.",
                issues=[
                    ErrorIssue(
                        code="sqlite_runtime_unsupported",
                        path="sqlite_runtime",
                        message=f"Detected SQLite {detected}; SQLite 3.51.3 or newer is required.",
                        remediation="Use a managed Python runtime with SQLite 3.51.3 or newer, or select the PostgreSQL profile. See the SQLite troubleshooting guidance.",
                    )
                ],
            )
        if error.code == ErrorCode.UNSUPPORTED_OPERATION and error.path == "sqlite_platform":
            return AipcsContractError(
                error.code,
                "SQLite storage is unavailable on this platform.",
                issues=[
                    ErrorIssue(
                        code="sqlite_platform_unsupported",
                        path="sqlite_platform",
                        message="SQLite storage is supported only on Linux or macOS local POSIX filesystems.",
                        remediation="Use the PostgreSQL profile or a supported SQLite host.",
                    )
                ],
            )
        if error.code == ErrorCode.UNSUPPORTED_OPERATION:
            message = "The selected configuration profile is unavailable in this release."
            issue_code = "profile_unavailable"
            remediation = "Select the stateless profile and retry."
        else:
            message = "Configuration failed public validation."
            issue_code = "invalid_configuration"
            remediation = "Use a documented value and retry."
        return AipcsContractError(
            error.code,
            message,
            issues=[
                ErrorIssue(
                    code=issue_code,
                    path=error.path,
                    message="The configuration value is invalid or unavailable.",
                    remediation=remediation,
                )
            ],
        )
    return AipcsContractError(
        ErrorCode.INTERNAL_ERROR,
        "Configuration could not be processed safely.",
    )
