"""Safe, transport-neutral configuration failures."""

from __future__ import annotations

from aipcs_mcp.errors import AipcsContractError, ErrorCode, ErrorIssue


class ConfigurationError(Exception):
    """Never retains untrusted values, paths, TOML, or secrets."""

    def __init__(self, code: ErrorCode = ErrorCode.VALIDATION_FAILED, path: str = "configuration"):
        self.code = code
        self.message = "Configuration is invalid or unavailable."
        self.path = path if path.isidentifier() and len(path) <= 64 else "configuration"
        super().__init__(self.message)


def to_contract_error(error: Exception) -> AipcsContractError:
    """Map configuration failures without retaining or exposing their input."""

    if isinstance(error, ConfigurationError):
        if error.code == ErrorCode.TRANSPORT_NOT_SUPPORTED:
            return AipcsContractError(
                error.code,
                "The selected transport is not supported by the documented configuration.",
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
