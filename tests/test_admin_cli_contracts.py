from __future__ import annotations

import json
from uuid import UUID

import pytest

from aipcs_mcp import cli
from aipcs_mcp.admin_cli import (
    CliConfirmation,
    CliExecutionMode,
    CliExit,
    CliLifecycleAction,
    CliOutputFormat,
    ExportInvocation,
    ImportInvocation,
    ServiceLifecycleInvocation,
    ServicePurgeInvocation,
    confirmation_for,
    execution_mode,
    exit_for_error,
    parse_canonical_uuid,
    parse_limit,
    parse_positive_revision,
    render_human_error,
    render_human_result,
)
from aipcs_mcp.errors import AipcsContractError, ErrorCode, ErrorIssue

SERVICE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OPERATION_ID = "22222222-2222-4222-8222-222222222222"
RECEIPT_ID = "33333333-3333-4333-8333-333333333333"
OTHER_SERVICE_ID = "44444444-4444-4444-8444-444444444444"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "00000000-0000-0000-0000-000000000000",
        SERVICE_ID.upper(),
        "{" + SERVICE_ID + "}",
        "not-a-uuid",
        1,
        None,
    ],
)
def test_canonical_uuid_rejects_noncanonical_or_zero_values(value: object) -> None:
    with pytest.raises(AipcsContractError) as captured:
        parse_canonical_uuid(value, label="service")
    assert captured.value.code is ErrorCode.VALIDATION_FAILED


def test_canonical_uuid_returns_exact_nonzero_uuid() -> None:
    assert parse_canonical_uuid(SERVICE_ID, label="service") == UUID(SERVICE_ID)


@pytest.mark.parametrize("value", [None, "1", str(2**63 - 1)])
def test_expected_revision_accepts_omission_or_bounded_canonical_integer(
    value: str | None,
) -> None:
    expected = None if value is None else int(value)
    assert parse_positive_revision(value) == expected


@pytest.mark.parametrize("value", ["", "0", "+1", "01", "-1", str(2**63), 1, True])
def test_expected_revision_rejects_invalid_lexical_or_numeric_values(value: object) -> None:
    with pytest.raises(AipcsContractError):
        parse_positive_revision(value)


@pytest.mark.parametrize(("value", "expected"), [("1", 1), ("100", 100)])
def test_limit_accepts_bounds(value: str, expected: int) -> None:
    assert parse_limit(value) == expected


@pytest.mark.parametrize("value", ["0", "101", "01", "x", 1, True])
def test_limit_rejects_noncanonical_or_out_of_range_values(value: object) -> None:
    with pytest.raises(AipcsContractError):
        parse_limit(value)


@pytest.mark.parametrize(
    ("output_format", "input_tty", "output_tty", "assume_yes", "expected"),
    [
        (CliOutputFormat.HUMAN, True, True, False, CliExecutionMode.INTERACTIVE),
        (CliOutputFormat.JSON, True, True, False, CliExecutionMode.MACHINE),
        (CliOutputFormat.HUMAN, False, True, False, CliExecutionMode.MACHINE),
        (CliOutputFormat.HUMAN, True, False, False, CliExecutionMode.MACHINE),
        (CliOutputFormat.HUMAN, True, True, True, CliExecutionMode.MACHINE),
    ],
)
def test_execution_mode_requires_human_ttys_without_yes(
    output_format: CliOutputFormat,
    input_tty: bool,
    output_tty: bool,
    assume_yes: bool,
    expected: CliExecutionMode,
) -> None:
    assert (
        execution_mode(
            output_format,
            input_is_tty=input_tty,
            output_is_tty=output_tty,
            assume_yes=assume_yes,
        )
        is expected
    )


@pytest.mark.parametrize("action", list(CliLifecycleAction))
def test_machine_lifecycle_requires_revision_and_operation_id(
    action: CliLifecycleAction,
) -> None:
    with pytest.raises(AipcsContractError):
        ServiceLifecycleInvocation(
            CliOutputFormat.JSON,
            CliExecutionMode.MACHINE,
            action,
            UUID(SERVICE_ID),
            None,
            UUID(OPERATION_ID),
            action is CliLifecycleAction.ARCHIVE,
        )
    with pytest.raises(AipcsContractError):
        ServiceLifecycleInvocation(
            CliOutputFormat.JSON,
            CliExecutionMode.MACHINE,
            action,
            UUID(SERVICE_ID),
            1,
            None,
            action is CliLifecycleAction.ARCHIVE,
        )


def test_machine_archive_requires_yes_but_other_lifecycle_does_not() -> None:
    common = (
        CliOutputFormat.JSON,
        CliExecutionMode.MACHINE,
        UUID(SERVICE_ID),
        1,
        UUID(OPERATION_ID),
    )
    with pytest.raises(AipcsContractError):
        ServiceLifecycleInvocation(
            common[0],
            common[1],
            CliLifecycleAction.ARCHIVE,
            common[2],
            common[3],
            common[4],
            False,
        )
    assert (
        ServiceLifecycleInvocation(
            common[0],
            common[1],
            CliLifecycleAction.SUSPEND,
            common[2],
            common[3],
            common[4],
            False,
        ).action
        is CliLifecycleAction.SUSPEND
    )


def test_interactive_missing_values_require_generated_value_confirmation() -> None:
    command = ServiceLifecycleInvocation(
        CliOutputFormat.HUMAN,
        CliExecutionMode.INTERACTIVE,
        CliLifecycleAction.SUSPEND,
        UUID(SERVICE_ID),
        None,
        None,
        False,
    )
    assert confirmation_for(command) is CliConfirmation.GENERATED_VALUES


def test_confirmation_policy_distinguishes_archive_import_purge_and_export() -> None:
    archive = ServiceLifecycleInvocation(
        CliOutputFormat.HUMAN,
        CliExecutionMode.INTERACTIVE,
        CliLifecycleAction.ARCHIVE,
        UUID(SERVICE_ID),
        1,
        UUID(OPERATION_ID),
        False,
    )
    export = ExportInvocation(
        CliOutputFormat.HUMAN,
        CliExecutionMode.INTERACTIVE,
        UUID(SERVICE_ID),
        None,
        None,
        "bundle.jsonl",
        False,
    )
    dry_run = ImportInvocation(
        CliOutputFormat.JSON,
        CliExecutionMode.MACHINE,
        "bundle.jsonl",
        True,
        None,
        False,
    )
    actual_import = ImportInvocation(
        CliOutputFormat.HUMAN,
        CliExecutionMode.INTERACTIVE,
        "bundle.jsonl",
        False,
        None,
        False,
    )
    purge = ServicePurgeInvocation(
        CliOutputFormat.HUMAN,
        CliExecutionMode.INTERACTIVE,
        UUID(SERVICE_ID),
        1,
        UUID(OPERATION_ID),
        UUID(RECEIPT_ID),
        False,
        None,
        False,
    )
    assert confirmation_for(archive) is CliConfirmation.ALWAYS
    assert confirmation_for(export) is CliConfirmation.GENERATED_VALUES
    assert confirmation_for(dry_run) is CliConfirmation.NONE
    assert confirmation_for(actual_import) is CliConfirmation.ALWAYS
    assert confirmation_for(purge) is CliConfirmation.EXACT_IDENTITY


def test_machine_import_requires_operation_id_and_yes() -> None:
    with pytest.raises(AipcsContractError):
        ImportInvocation(
            CliOutputFormat.JSON,
            CliExecutionMode.MACHINE,
            "bundle.jsonl",
            False,
            UUID(OPERATION_ID),
            False,
        )
    with pytest.raises(AipcsContractError):
        ImportInvocation(
            CliOutputFormat.JSON,
            CliExecutionMode.MACHINE,
            "bundle.jsonl",
            False,
            None,
            True,
        )


def test_dry_run_import_rejects_mutation_options() -> None:
    with pytest.raises(AipcsContractError):
        ImportInvocation(
            CliOutputFormat.JSON,
            CliExecutionMode.MACHINE,
            "bundle.jsonl",
            True,
            UUID(OPERATION_ID),
            False,
        )
    with pytest.raises(AipcsContractError):
        ImportInvocation(
            CliOutputFormat.JSON,
            CliExecutionMode.MACHINE,
            "bundle.jsonl",
            True,
            None,
            True,
        )


def test_machine_purge_requires_authority_yes_and_exact_identity() -> None:
    common = {
        "output_format": CliOutputFormat.JSON,
        "mode": CliExecutionMode.MACHINE,
        "service_id": UUID(SERVICE_ID),
        "expected_revision": 1,
        "operation_id": UUID(OPERATION_ID),
        "receipt_id": UUID(RECEIPT_ID),
        "explicit_override": False,
    }
    with pytest.raises(AipcsContractError):
        ServicePurgeInvocation(
            **common,
            confirmed_service_id=UUID(SERVICE_ID),
            assume_yes=False,
        )
    with pytest.raises(AipcsContractError):
        ServicePurgeInvocation(
            **common,
            confirmed_service_id=UUID(OTHER_SERVICE_ID),
            assume_yes=True,
        )
    command = ServicePurgeInvocation(
        **common,
        confirmed_service_id=UUID(SERVICE_ID),
        assume_yes=True,
    )
    assert command.receipt_id == UUID(RECEIPT_ID)


def test_purge_requires_exactly_one_authority() -> None:
    common = (
        CliOutputFormat.HUMAN,
        CliExecutionMode.INTERACTIVE,
        UUID(SERVICE_ID),
        1,
        UUID(OPERATION_ID),
    )
    with pytest.raises(AipcsContractError):
        ServicePurgeInvocation(*common, None, False, None, False)
    with pytest.raises(AipcsContractError):
        ServicePurgeInvocation(*common, UUID(RECEIPT_ID), True, None, False)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (ErrorCode.INVALID_REQUEST, CliExit.INVALID_INPUT),
        (ErrorCode.INVALID_IDENTIFIER, CliExit.INVALID_INPUT),
        (ErrorCode.VALIDATION_FAILED, CliExit.INVALID_INPUT),
        (ErrorCode.RETIRED_FIELD, CliExit.INVALID_INPUT),
        (ErrorCode.MANIFEST_VERSION_UNSUPPORTED, CliExit.INVALID_INPUT),
        (ErrorCode.LEGACY_IMPORT_REQUIRED, CliExit.INVALID_INPUT),
        (ErrorCode.TRANSPORT_NOT_SUPPORTED, CliExit.INVALID_INPUT),
        (ErrorCode.NOT_FOUND, CliExit.REFUSED),
        (ErrorCode.ALREADY_EXISTS, CliExit.REFUSED),
        (ErrorCode.INVALID_STATE, CliExit.REFUSED),
        (ErrorCode.UNSUPPORTED_OPERATION, CliExit.REFUSED),
        (ErrorCode.CONFLICT, CliExit.REFUSED),
        (ErrorCode.UNSUPPORTED_TRANSITION, CliExit.REFUSED),
        (ErrorCode.STALE_REVISION, CliExit.REFUSED),
        (ErrorCode.STALE_RECORD_VERSION, CliExit.REFUSED),
        (ErrorCode.STALE_BRANCH_REVISION, CliExit.REFUSED),
        (ErrorCode.CHANGED_FINGERPRINT, CliExit.REFUSED),
        (ErrorCode.CONSTRAINT_VIOLATION, CliExit.REFUSED),
        (ErrorCode.STORAGE_MIGRATION_REQUIRED, CliExit.REFUSED),
        (ErrorCode.FORBIDDEN, CliExit.REFUSED),
        (ErrorCode.OPERATION_IN_PROGRESS, CliExit.RETRYABLE),
        (ErrorCode.STORAGE_BUSY, CliExit.RETRYABLE),
        (ErrorCode.OPERATION_UNCERTAIN, CliExit.RETRYABLE),
        (ErrorCode.RECOVERY_REQUIRED, CliExit.RECOVERY_REQUIRED),
        (ErrorCode.STORAGE_UNAVAILABLE, CliExit.UNAVAILABLE),
        (ErrorCode.INTERNAL_ERROR, CliExit.UNAVAILABLE),
    ],
)
def test_every_public_error_code_has_stable_cli_exit(
    code: ErrorCode, expected: CliExit
) -> None:
    assert exit_for_error(AipcsContractError(code, "Safe.")) is expected


def test_exit_mapping_test_covers_complete_error_enum() -> None:
    listed = {
        ErrorCode.INVALID_REQUEST,
        ErrorCode.INVALID_IDENTIFIER,
        ErrorCode.VALIDATION_FAILED,
        ErrorCode.RETIRED_FIELD,
        ErrorCode.MANIFEST_VERSION_UNSUPPORTED,
        ErrorCode.LEGACY_IMPORT_REQUIRED,
        ErrorCode.TRANSPORT_NOT_SUPPORTED,
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
        ErrorCode.OPERATION_IN_PROGRESS,
        ErrorCode.STORAGE_BUSY,
        ErrorCode.OPERATION_UNCERTAIN,
        ErrorCode.RECOVERY_REQUIRED,
        ErrorCode.STORAGE_UNAVAILABLE,
        ErrorCode.INTERNAL_ERROR,
    }
    assert listed == set(ErrorCode)


def test_retryable_flag_uses_retryable_exit_without_parsing_message() -> None:
    error = AipcsContractError(
        ErrorCode.CONFLICT,
        "Message is irrelevant.",
        retryable=True,
    )
    assert exit_for_error(error) is CliExit.RETRYABLE


def test_human_error_is_bounded_to_safe_error_fields() -> None:
    error = AipcsContractError(
        ErrorCode.VALIDATION_FAILED,
        "Request is invalid.",
        issues=[
            ErrorIssue(
                code="invalid_value",
                path="request.service_id",
                message="The field value is invalid.",
                remediation="Use a canonical UUID.",
            )
        ],
    )
    assert render_human_error(error).splitlines() == [
        "error [validation_failed]: Request is invalid.",
        "- request.service_id: The field value is invalid.",
        "  Use a canonical UUID.",
    ]


def test_human_result_projects_safe_mapping_deterministically() -> None:
    assert render_human_result(
        {
            "profile": "sqlite",
            "ready": True,
            "counts": {"active": 2, "archived": 1},
            "issues": ["one", "two"],
        }
    ).splitlines() == [
        "profile: sqlite",
        "ready: true",
        "counts.active: 2",
        "counts.archived: 1",
        'issues: [\"one\",\"two\"]',
    ]


def test_human_result_projects_bounded_mapping_sequences_with_indices() -> None:
    assert render_human_result(
        {
            "checks": [
                {"check_id": "registry", "status": "pass"},
                {"check_id": "service_store", "status": "warning"},
            ]
        }
    ).splitlines() == [
        "checks[0].check_id: registry",
        "checks[0].status: pass",
        "checks[1].check_id: service_store",
        "checks[1].status: warning",
    ]


@pytest.mark.parametrize(
    "result",
    [
        {"Bad-Key": "x"},
        {"safe": object()},
        {"safe": [{"unsafe": object()}]},
        {"safe": {"nested": {"again": {"deeper": {"too": {"far": 1}}}}}},
    ],
)
def test_human_result_rejects_unsafe_or_unbounded_values(
    result: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        render_human_result(result)


@pytest.mark.parametrize(
    "argv",
    [
        [
            "service",
            "suspend",
            SERVICE_ID,
            "--expected-revision",
            "1",
            "--operation-id",
            OPERATION_ID,
        ],
        [
            "service",
            "resume",
            SERVICE_ID,
            "--expected-revision",
            "1",
            "--operation-id",
            OPERATION_ID,
        ],
        [
            "service",
            "archive",
            SERVICE_ID,
            "--expected-revision",
            "1",
            "--operation-id",
            OPERATION_ID,
            "--yes",
        ],
        [
            "service",
            "restore",
            SERVICE_ID,
            "--expected-revision",
            "1",
            "--operation-id",
            OPERATION_ID,
        ],
        [
            "service",
            "purge",
            SERVICE_ID,
            "--expected-revision",
            "1",
            "--operation-id",
            OPERATION_ID,
            "--receipt",
            RECEIPT_ID,
            "--confirm-service-id",
            SERVICE_ID,
            "--yes",
        ],
        [
            "service",
            "purge",
            SERVICE_ID,
            "--expected-revision",
            "1",
            "--operation-id",
            OPERATION_ID,
            "--override",
            "--confirm-service-id",
            SERVICE_ID,
            "--yes",
        ],
        [
            "export",
            SERVICE_ID,
            "--output",
            "bundle.jsonl",
            "--expected-revision",
            "1",
            "--operation-id",
            OPERATION_ID,
        ],
        ["import", "--input", "bundle.jsonl", "--dry-run"],
        [
            "import",
            "--input",
            "bundle.jsonl",
            "--operation-id",
            OPERATION_ID,
            "--yes",
        ],
    ],
)
def test_persistent_admin_mutations_fail_closed_for_stateless_profile(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(argv) == CliExit.REFUSED
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "unsupported_operation"


def test_machine_mutation_validation_precedes_unavailable_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["service", "suspend", SERVICE_ID]) == CliExit.INVALID_INPUT
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "validation_failed"


def test_complete_machine_archive_reaches_unavailable_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        cli.main(
            [
                "service",
                "archive",
                SERVICE_ID,
                "--expected-revision",
                "7",
                "--operation-id",
                OPERATION_ID,
                "--yes",
            ]
        )
        == CliExit.REFUSED
    )
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "unsupported_operation"


def test_human_admin_failure_uses_human_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["storage", "status", "--format", "human"]) == CliExit.REFUSED
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("error [unsupported_operation]:")


def test_stateless_status_and_doctor_are_read_only_successes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["status"]) == CliExit.SUCCESS
    status = json.loads(capsys.readouterr().out)["result"]
    assert status["profile"] == "stateless"
    assert status["overall"] == "unavailable"

    assert cli.main(["doctor"]) == CliExit.SUCCESS
    doctor = json.loads(capsys.readouterr().out)["result"]
    assert doctor["profile"] == "stateless"
    assert doctor["overall"] == "fail"
