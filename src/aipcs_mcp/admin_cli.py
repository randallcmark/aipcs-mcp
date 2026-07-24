"""Pure administration CLI contracts with no storage or runtime imports."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from uuid import UUID

from .errors import AipcsContractError, ErrorCode

MAX_SERVICE_REVISION = 2**63 - 1
MAX_CLI_PATH_LENGTH = 4_096
_POSITIVE_INTEGER = re.compile(r"^[1-9][0-9]*$")
_SAFE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,95}$")


class CliOutputFormat(StrEnum):
    JSON = "json"
    HUMAN = "human"


class CliExecutionMode(StrEnum):
    MACHINE = "machine"
    INTERACTIVE = "interactive"


class CliExit(IntEnum):
    SUCCESS = 0
    INVALID_INPUT = 2
    REFUSED = 3
    RETRYABLE = 4
    RECOVERY_REQUIRED = 5
    UNAVAILABLE = 6


class CliLifecycleAction(StrEnum):
    SUSPEND = "suspend"
    RESUME = "resume"
    ARCHIVE = "archive"
    RESTORE = "restore"


class CliConfirmation(StrEnum):
    NONE = "none"
    GENERATED_VALUES = "generated_values"
    ALWAYS = "always"
    EXACT_IDENTITY = "exact_identity"


@dataclass(frozen=True, slots=True)
class StatusInvocation:
    output_format: CliOutputFormat

    def __post_init__(self) -> None:
        _require_output_format(self.output_format)


@dataclass(frozen=True, slots=True)
class DoctorInvocation:
    output_format: CliOutputFormat
    service_id: UUID | None

    def __post_init__(self) -> None:
        _require_output_format(self.output_format)
        if self.service_id is not None:
            _require_nonzero_uuid(self.service_id, "service")


@dataclass(frozen=True, slots=True)
class StorageStatusInvocation:
    output_format: CliOutputFormat
    service_id: UUID | None

    def __post_init__(self) -> None:
        _require_output_format(self.output_format)
        if self.service_id is not None:
            _require_nonzero_uuid(self.service_id, "service")


@dataclass(frozen=True, slots=True)
class ServiceListInvocation:
    output_format: CliOutputFormat
    limit: int

    def __post_init__(self) -> None:
        _require_output_format(self.output_format)
        if type(self.limit) is not int or not 1 <= self.limit <= 100:
            raise _invalid("Service list limit must be between 1 and 100.")


@dataclass(frozen=True, slots=True)
class ServiceInspectInvocation:
    output_format: CliOutputFormat
    service_id: UUID

    def __post_init__(self) -> None:
        _require_output_format(self.output_format)
        _require_nonzero_uuid(self.service_id, "service")


@dataclass(frozen=True, slots=True)
class ServiceLifecycleInvocation:
    output_format: CliOutputFormat
    mode: CliExecutionMode
    action: CliLifecycleAction
    service_id: UUID
    expected_revision: int | None
    operation_id: UUID | None
    assume_yes: bool

    def __post_init__(self) -> None:
        _require_output_format(self.output_format)
        _require_execution_mode(self.mode)
        if type(self.action) is not CliLifecycleAction:
            raise _invalid("Lifecycle action is invalid.")
        _require_nonzero_uuid(self.service_id, "service")
        _validate_mutation_values(
            self.mode,
            self.expected_revision,
            self.operation_id,
            self.assume_yes,
        )
        if (
            self.action is CliLifecycleAction.ARCHIVE
            and self.mode is CliExecutionMode.MACHINE
            and not self.assume_yes
        ):
            raise _invalid("Archive requires --yes in machine mode.")


@dataclass(frozen=True, slots=True)
class ServicePurgeInvocation:
    output_format: CliOutputFormat
    mode: CliExecutionMode
    service_id: UUID
    expected_revision: int | None
    operation_id: UUID | None
    receipt_id: UUID | None
    explicit_override: bool
    confirmed_service_id: UUID | None
    assume_yes: bool

    def __post_init__(self) -> None:
        _require_output_format(self.output_format)
        _require_execution_mode(self.mode)
        if type(self.explicit_override) is not bool:
            raise _invalid("Purge authority is invalid.")
        _require_nonzero_uuid(self.service_id, "service")
        _validate_mutation_values(
            self.mode,
            self.expected_revision,
            self.operation_id,
            self.assume_yes,
        )
        if (self.receipt_id is None) == (not self.explicit_override):
            raise _invalid("Purge requires exactly one of --receipt or --override.")
        if self.receipt_id is not None:
            _require_nonzero_uuid(self.receipt_id, "receipt")
        if self.confirmed_service_id is not None:
            _require_nonzero_uuid(self.confirmed_service_id, "confirmed service")
        if self.mode is CliExecutionMode.MACHINE and (
            not self.assume_yes or self.confirmed_service_id != self.service_id
        ):
            raise _invalid(
                "Machine purge requires --yes and an exact --confirm-service-id."
            )


@dataclass(frozen=True, slots=True)
class ExportInvocation:
    output_format: CliOutputFormat
    mode: CliExecutionMode
    service_id: UUID
    expected_revision: int | None
    operation_id: UUID | None
    output_path: str
    assume_yes: bool

    def __post_init__(self) -> None:
        _require_output_format(self.output_format)
        _require_execution_mode(self.mode)
        _require_nonzero_uuid(self.service_id, "service")
        _validate_mutation_values(
            self.mode,
            self.expected_revision,
            self.operation_id,
            self.assume_yes,
        )
        _validate_path_text(self.output_path)


@dataclass(frozen=True, slots=True)
class ImportInvocation:
    output_format: CliOutputFormat
    mode: CliExecutionMode
    input_path: str
    dry_run: bool
    operation_id: UUID | None
    assume_yes: bool

    def __post_init__(self) -> None:
        _require_output_format(self.output_format)
        _require_execution_mode(self.mode)
        if type(self.dry_run) is not bool or type(self.assume_yes) is not bool:
            raise _invalid("Import mode is invalid.")
        _validate_path_text(self.input_path)
        if self.dry_run:
            if self.operation_id is not None or self.assume_yes:
                raise _invalid("Dry-run import does not accept mutation options.")
            return
        if self.mode is CliExecutionMode.MACHINE and (
            self.operation_id is None or not self.assume_yes
        ):
            raise _invalid(
                "Machine import requires an explicit --operation-id and --yes."
            )
        if self.operation_id is not None:
            _require_nonzero_uuid(self.operation_id, "operation")


@dataclass(frozen=True, slots=True)
class MaintenanceScanInvocation:
    output_format: CliOutputFormat
    service_id: UUID

    def __post_init__(self) -> None:
        _require_output_format(self.output_format)
        _require_nonzero_uuid(self.service_id, "service")


type AdminInvocation = (
    StatusInvocation
    | DoctorInvocation
    | StorageStatusInvocation
    | ServiceListInvocation
    | ServiceInspectInvocation
    | ServiceLifecycleInvocation
    | ServicePurgeInvocation
    | ExportInvocation
    | ImportInvocation
    | MaintenanceScanInvocation
)


def parse_output_format(value: object) -> CliOutputFormat:
    try:
        return CliOutputFormat(value)
    except (TypeError, ValueError):
        raise _invalid("Output format is invalid.") from None


def execution_mode(
    output_format: CliOutputFormat, *, input_is_tty: bool, output_is_tty: bool, assume_yes: bool
) -> CliExecutionMode:
    _require_output_format(output_format)
    if any(type(value) is not bool for value in (input_is_tty, output_is_tty, assume_yes)):
        raise _invalid("CLI execution mode is invalid.")
    if (
        output_format is CliOutputFormat.HUMAN
        and input_is_tty
        and output_is_tty
        and not assume_yes
    ):
        return CliExecutionMode.INTERACTIVE
    return CliExecutionMode.MACHINE


def parse_canonical_uuid(value: object, *, label: str) -> UUID:
    if type(value) is not str:
        raise _invalid(f"{label.capitalize()} identifier is invalid.")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise _invalid(f"{label.capitalize()} identifier is invalid.") from None
    if parsed.int == 0 or str(parsed) != value:
        raise _invalid(f"{label.capitalize()} identifier is invalid.")
    return parsed


def parse_positive_revision(value: object | None) -> int | None:
    if value is None:
        return None
    if type(value) is not str or _POSITIVE_INTEGER.fullmatch(value) is None:
        raise _invalid("Expected revision is invalid.")
    revision = int(value)
    if revision > MAX_SERVICE_REVISION:
        raise _invalid("Expected revision is invalid.")
    return revision


def parse_limit(value: object) -> int:
    if type(value) is not str or _POSITIVE_INTEGER.fullmatch(value) is None:
        raise _invalid("Service list limit is invalid.")
    limit = int(value)
    if not 1 <= limit <= 100:
        raise _invalid("Service list limit is invalid.")
    return limit


def confirmation_for(command: AdminInvocation) -> CliConfirmation:
    if isinstance(command, ServicePurgeInvocation):
        return CliConfirmation.EXACT_IDENTITY
    if isinstance(command, ImportInvocation):
        return CliConfirmation.NONE if command.dry_run else CliConfirmation.ALWAYS
    if isinstance(command, ServiceLifecycleInvocation):
        if command.action is CliLifecycleAction.ARCHIVE:
            return CliConfirmation.ALWAYS
        if command.expected_revision is None or command.operation_id is None:
            return CliConfirmation.GENERATED_VALUES
        return CliConfirmation.NONE
    if isinstance(command, ExportInvocation) and (
        command.expected_revision is None or command.operation_id is None
    ):
        return CliConfirmation.GENERATED_VALUES
    return CliConfirmation.NONE


_INVALID_INPUT_CODES = frozenset(
    {
        ErrorCode.INVALID_REQUEST,
        ErrorCode.INVALID_IDENTIFIER,
        ErrorCode.VALIDATION_FAILED,
        ErrorCode.RETIRED_FIELD,
        ErrorCode.MANIFEST_VERSION_UNSUPPORTED,
        ErrorCode.LEGACY_IMPORT_REQUIRED,
        ErrorCode.TRANSPORT_NOT_SUPPORTED,
    }
)
_REFUSED_CODES = frozenset(
    {
        ErrorCode.NOT_FOUND,
        ErrorCode.ALREADY_EXISTS,
        ErrorCode.INVALID_STATE,
        ErrorCode.UNSUPPORTED_OPERATION,
        ErrorCode.CONFLICT,
        ErrorCode.UNSUPPORTED_TRANSITION,
        ErrorCode.STALE_REVISION,
        ErrorCode.STALE_RECORD_VERSION,
        ErrorCode.STALE_BRANCH_REVISION,
        ErrorCode.CHANGED_FINGERPRINT,
        ErrorCode.CONSTRAINT_VIOLATION,
        ErrorCode.STORAGE_MIGRATION_REQUIRED,
        ErrorCode.FORBIDDEN,
    }
)
_RETRYABLE_CODES = frozenset(
    {
        ErrorCode.OPERATION_IN_PROGRESS,
        ErrorCode.STORAGE_BUSY,
        ErrorCode.OPERATION_UNCERTAIN,
    }
)
_UNAVAILABLE_CODES = frozenset(
    {
        ErrorCode.STORAGE_UNAVAILABLE,
        ErrorCode.INTERNAL_ERROR,
    }
)


def exit_for_error(error: AipcsContractError) -> CliExit:
    if error.code is ErrorCode.RECOVERY_REQUIRED:
        return CliExit.RECOVERY_REQUIRED
    if error.code in _RETRYABLE_CODES or error.retryable:
        return CliExit.RETRYABLE
    if error.code in _INVALID_INPUT_CODES:
        return CliExit.INVALID_INPUT
    if error.code in _REFUSED_CODES:
        return CliExit.REFUSED
    if error.code in _UNAVAILABLE_CODES:
        return CliExit.UNAVAILABLE
    raise RuntimeError("CLI error code has no exit mapping.")


def render_human_error(error: AipcsContractError) -> str:
    lines = [f"error [{error.code.value}]: {error.message}"]
    for issue in error.issues:
        lines.append(f"- {issue.path}: {issue.message}")
        if issue.remediation is not None:
            lines.append(f"  {issue.remediation}")
    return "\n".join(lines)


def render_human_result(result: Mapping[str, object]) -> str:
    lines: list[str] = []
    _render_mapping(result, lines, prefix="", depth=0)
    return "\n".join(lines)


def _render_mapping(
    value: Mapping[str, object], lines: list[str], *, prefix: str, depth: int
) -> None:
    if depth > 4 or len(value) > 64:
        raise ValueError("Human result is outside the supported projection bounds.")
    for key, item in value.items():
        if type(key) is not str or _SAFE_KEY.fullmatch(key) is None:
            raise ValueError("Human result contains an unsafe field.")
        label = f"{prefix}{key}"
        if isinstance(item, Mapping):
            _render_mapping(item, lines, prefix=f"{label}.", depth=depth + 1)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            if len(item) > 100:
                raise ValueError("Human result is outside the supported projection bounds.")
            sequence = list(item)
            if all(isinstance(entry, Mapping) for entry in sequence):
                if not sequence:
                    lines.append(f"{label}: []")
                for index, entry in enumerate(sequence):
                    _render_mapping(
                        entry,
                        lines,
                        prefix=f"{label}[{index}].",
                        depth=depth + 1,
                    )
                continue
            if any(
                value is not None and type(value) not in {str, int, bool, float}
                for value in sequence
            ):
                raise ValueError("Human result contains an unsupported value.")
            lines.append(
                f"{label}: "
                f"{json.dumps(sequence, ensure_ascii=True, separators=(',', ':'))}"
            )
        elif item is None or type(item) in {str, int, bool, float}:
            lines.append(f"{label}: {_render_scalar(item)}")
        else:
            raise ValueError("Human result contains an unsupported value.")


def _render_scalar(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _validate_mutation_values(
    mode: CliExecutionMode,
    expected_revision: int | None,
    operation_id: UUID | None,
    assume_yes: bool,
) -> None:
    if type(assume_yes) is not bool:
        raise _invalid("Confirmation mode is invalid.")
    if expected_revision is not None and (
        type(expected_revision) is not int
        or not 1 <= expected_revision <= MAX_SERVICE_REVISION
    ):
        raise _invalid("Expected revision is invalid.")
    if operation_id is not None:
        _require_nonzero_uuid(operation_id, "operation")
    if mode is CliExecutionMode.MACHINE and (
        expected_revision is None or operation_id is None
    ):
        raise _invalid(
            "Machine mutation requires explicit --expected-revision and --operation-id."
        )


def _validate_path_text(value: object) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_CLI_PATH_LENGTH
        or "\x00" in value
    ):
        raise _invalid("Artifact path is invalid.")


def _require_nonzero_uuid(value: object, label: str) -> None:
    if type(value) is not UUID or value.int == 0:
        raise _invalid(f"{label.capitalize()} identifier is invalid.")


def _require_output_format(value: object) -> None:
    if type(value) is not CliOutputFormat:
        raise _invalid("Output format is invalid.")


def _require_execution_mode(value: object) -> None:
    if type(value) is not CliExecutionMode:
        raise _invalid("CLI execution mode is invalid.")


def _invalid(message: str) -> AipcsContractError:
    return AipcsContractError(ErrorCode.VALIDATION_FAILED, message)
