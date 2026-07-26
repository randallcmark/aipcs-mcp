"""Command-line entry point for local and service AIPCS MCP transports."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import anyio

from .admin_cli import (
    AdminInvocation,
    CliConfirmation,
    CliExecutionMode,
    CliLifecycleAction,
    DoctorInvocation,
    ExportInvocation,
    ImportInvocation,
    MaintenanceScanInvocation,
    ServiceInspectInvocation,
    ServiceLifecycleInvocation,
    ServiceListInvocation,
    ServicePurgeInvocation,
    StatusInvocation,
    StorageStatusInvocation,
    confirmation_for,
    execution_mode,
    exit_for_error,
    parse_canonical_uuid,
    parse_limit,
    parse_output_format,
    parse_positive_revision,
    render_human_error,
    render_human_result,
)
from .admin_files import AdminFileError, ExclusiveExportFile, open_import_file
from .application.admin import project_doctor, project_migration, project_status
from .application.errors import ApplicationError
from .application.operational_lifecycle import (
    ArchiveCommand,
    RestoreCommand,
    ResumeCommand,
    SuspendCommand,
)
from .application.portable import (
    ImportBundleRequest,
    PortableExecutionResult,
    PortableResultCategory,
)
from .application.portable_models import PortableBundleSummary
from .application.registry_authority import (
    ExportCommand,
    PurgeAuthority,
    PurgeAuthorityKind,
    PurgeCommand,
    PurgeTombstone,
    StorageBackend,
    TransferReceipt,
)
from .configuration.errors import to_contract_error
from .configuration.models import ConfigOverrides, ResolvedConfiguration
from .configuration.reporting import (
    safe_config_report,
    safe_validation_report,
)
from .configuration.resolver import require_runnable, resolve_configuration
from .contracts import validate_transport_environment
from .data_results import project_maintenance
from .errors import AipcsContractError, ErrorCode, success
from .http_server import run_streamable_http
from .mcp_server import run_stdio
from .records import DataFailure
from .runtime import compose_admin_runtime, compose_server


def _add_configuration_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="Explicit TOML configuration file.")
    parser.add_argument("--profile", help="Execution profile override.")
    parser.add_argument("--transport", help="MCP transport: stdio or streamable-http.")
    parser.add_argument("--http-host", help="Streamable HTTP bind address override.")
    parser.add_argument("--http-port", help="Streamable HTTP bind port override.")
    parser.add_argument("--http-path", help="Streamable HTTP MCP endpoint path override.")
    parser.add_argument(
        "--http-allowed-hosts",
        help="Comma-separated Streamable HTTP Host header allowlist override.",
    )
    parser.add_argument(
        "--http-allowed-origins",
        help="Comma-separated Streamable HTTP Origin header allowlist override.",
    )
    parser.add_argument(
        "--http-session-idle-timeout-seconds",
        help="Streamable HTTP session idle timeout override.",
    )
    parser.add_argument(
        "--http-allow-non-loopback",
        help="Explicitly permit a non-loopback Streamable HTTP bind address (true or false).",
    )
    parser.add_argument("--principal-id", help="Principal identity override.")
    parser.add_argument(
        "--sqlite-data-root", help="SQLite data-root override (redacted in output)."
    )
    parser.add_argument(
        "--sqlite-busy-timeout-ms", help="SQLite busy-handler timeout in milliseconds."
    )
    parser.add_argument("--postgres-dsn-env", help="PostgreSQL DSN environment reference.")
    parser.add_argument(
        "--postgres-connect-timeout-seconds", help="PostgreSQL connect timeout in seconds."
    )
    parser.add_argument(
        "--postgres-lock-timeout-ms", help="PostgreSQL lock timeout in milliseconds."
    )
    parser.add_argument(
        "--postgres-statement-timeout-ms", help="PostgreSQL statement timeout in milliseconds."
    )
    parser.add_argument("--log-level", help="Stderr logging level override.")


def _add_presentation_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=("json", "human"),
        default="json",
        help="Result presentation format (default: json).",
    )


def _add_admin_options(parser: argparse.ArgumentParser) -> None:
    _add_configuration_options(parser)
    _add_presentation_option(parser)


def _add_mutation_options(
    parser: argparse.ArgumentParser, *, expected_revision: bool = True
) -> None:
    if expected_revision:
        parser.add_argument("--expected-revision")
    parser.add_argument("--operation-id")
    parser.add_argument("--yes", action="store_true", help="Do not prompt for confirmation.")


def build_parser() -> argparse.ArgumentParser:
    """Build the frozen V1-11 command tree."""

    parser = argparse.ArgumentParser(prog="aipcs")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Run the local AIPCS MCP server over stdio.")
    _add_configuration_options(serve)

    config = commands.add_parser("config", help="Inspect or validate AIPCS configuration.")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    show = config_commands.add_parser("show", help="Show a redacted configuration report.")
    _add_configuration_options(show)
    _add_presentation_option(show)
    validate = config_commands.add_parser(
        "validate",
        help="Validate a runnable configuration without starting the server.",
    )
    _add_configuration_options(validate)
    _add_presentation_option(validate)

    status = commands.add_parser("status", help="Show bounded configured-runtime status.")
    _add_admin_options(status)

    doctor = commands.add_parser("doctor", help="Run read-only configuration/storage checks.")
    doctor.add_argument("--service")
    _add_admin_options(doctor)

    storage = commands.add_parser("storage", help="Inspect storage readiness.")
    storage_commands = storage.add_subparsers(dest="storage_command", required=True)
    storage_status = storage_commands.add_parser(
        "status", help="Show safe registry or service-store migration state."
    )
    storage_status.add_argument("--service")
    _add_admin_options(storage_status)

    service = commands.add_parser("service", help="Inspect or operate one AIPCS service.")
    service_commands = service.add_subparsers(dest="service_command", required=True)
    service_list = service_commands.add_parser("list", help="List principal-scoped services.")
    service_list.add_argument("--limit", default="100")
    _add_admin_options(service_list)
    service_inspect = service_commands.add_parser("inspect", help="Inspect one service.")
    service_inspect.add_argument("service_id")
    _add_admin_options(service_inspect)
    for action in ("suspend", "resume", "archive", "restore"):
        lifecycle = service_commands.add_parser(action, help=f"{action.capitalize()} one service.")
        lifecycle.add_argument("service_id")
        _add_mutation_options(lifecycle)
        _add_admin_options(lifecycle)
    purge = service_commands.add_parser("purge", help="Irreversibly purge one archived service.")
    purge.add_argument("service_id")
    _add_mutation_options(purge)
    authority = purge.add_mutually_exclusive_group(required=True)
    authority.add_argument("--receipt")
    authority.add_argument("--override", action="store_true")
    purge.add_argument("--confirm-service-id")
    _add_admin_options(purge)

    export = commands.add_parser("export", help="Write a logical portable service artifact.")
    export.add_argument("service_id")
    export.add_argument("--output", required=True)
    _add_mutation_options(export)
    _add_admin_options(export)

    import_command = commands.add_parser(
        "import", help="Validate or import a logical portable service artifact."
    )
    import_command.add_argument("--input", required=True)
    import_command.add_argument("--dry-run", action="store_true")
    _add_mutation_options(import_command, expected_revision=False)
    _add_admin_options(import_command)

    maintenance = commands.add_parser("maintenance", help="Inspect maintenance candidates.")
    maintenance_commands = maintenance.add_subparsers(
        dest="maintenance_command", required=True
    )
    maintenance_scan = maintenance_commands.add_parser(
        "scan", help="Run a bounded read-only maintenance scan."
    )
    maintenance_scan.add_argument("--service", required=True)
    _add_admin_options(maintenance_scan)
    return parser


def _write_contract_error(error: AipcsContractError, output_format: str = "json") -> None:
    """Write one compact, stable failure envelope to stderr."""

    if parse_output_format(output_format).value == "human":
        print(render_human_error(error), file=sys.stderr)
    else:
        print(error.envelope().model_dump_json(), file=sys.stderr)


def _overrides_from_args(args: argparse.Namespace) -> ConfigOverrides:
    return ConfigOverrides(
        profile=args.profile,
        transport=args.transport,
        http_host=args.http_host,
        http_port=args.http_port,
        http_path=args.http_path,
        http_allowed_hosts=args.http_allowed_hosts,
        http_allowed_origins=args.http_allowed_origins,
        http_session_idle_timeout_seconds=args.http_session_idle_timeout_seconds,
        http_allow_non_loopback=args.http_allow_non_loopback,
        principal_id=args.principal_id,
        sqlite_data_root=args.sqlite_data_root,
        sqlite_busy_timeout_ms=args.sqlite_busy_timeout_ms,
        postgres_dsn_env=args.postgres_dsn_env,
        postgres_connect_timeout_seconds=args.postgres_connect_timeout_seconds,
        postgres_lock_timeout_ms=args.postgres_lock_timeout_ms,
        postgres_statement_timeout_ms=args.postgres_statement_timeout_ms,
        log_level=args.log_level,
    )


def _resolve(
    args: argparse.Namespace, *, runnable: bool, environ: Mapping[str, str]
) -> ResolvedConfiguration:
    try:
        resolved = resolve_configuration(
            overrides=_overrides_from_args(args),
            environ=environ,
            config_path=Path(args.config) if args.config is not None else None,
        )
        if runnable:
            require_runnable(resolved)
        return resolved
    except AipcsContractError:
        raise
    except Exception as error:
        raise to_contract_error(error) from None


def _write_success(result: dict[str, object], output_format: str = "json") -> None:
    if parse_output_format(output_format).value == "human":
        print(render_human_result(result))
    else:
        print(success(result).model_dump_json())


def _preflight(args: argparse.Namespace, environ: Mapping[str, str]) -> None:
    """Reject unsupported ambient listener settings before configuration work."""

    validate_transport_environment(args.transport, environ=environ)


def _run_config_show(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    _preflight(args, environ)
    resolved = _resolve(args, runnable=False, environ=environ)
    _write_success(safe_config_report(resolved), args.output_format)
    return 0


def _run_config_validate(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    _preflight(args, environ)
    resolved = _resolve(args, runnable=True, environ=environ)
    _write_success(safe_validation_report(resolved), args.output_format)
    return 0


def _configure_stderr_logging(config: ResolvedConfiguration) -> None:
    """Apply the resolved runtime threshold while keeping protocol stdout untouched."""

    level = getattr(logging, config.log_level.upper())
    root = logging.getLogger()
    root.handlers[:] = [logging.NullHandler()]
    root.setLevel(logging.CRITICAL + 1)
    logger = logging.getLogger("aipcs_mcp")
    logger.handlers[:] = [logging.StreamHandler(sys.stderr)]
    logger.handlers[0].setLevel(level)
    logger.setLevel(level)
    logger.propagate = False


def _run_serve(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    _preflight(args, environ)
    resolved = _resolve(args, runnable=True, environ=environ)
    _configure_stderr_logging(resolved)
    try:
        server = compose_server(resolved)
        if resolved.transport == "stdio":
            anyio.run(run_stdio, server)
        else:
            anyio.run(run_streamable_http, server, resolved)
    except Exception:
        _write_contract_error(
            AipcsContractError(ErrorCode.INTERNAL_ERROR, "Server could not be started safely.")
        )
        return 2
    return 0


def _parse_admin_invocation(args: argparse.Namespace) -> AdminInvocation:
    output_format = parse_output_format(args.output_format)
    assume_yes = bool(getattr(args, "yes", False))
    mode = execution_mode(
        output_format,
        input_is_tty=sys.stdin.isatty(),
        output_is_tty=sys.stdout.isatty(),
        assume_yes=assume_yes,
    )
    if args.command == "status":
        return StatusInvocation(output_format)
    if args.command == "doctor":
        return DoctorInvocation(
            output_format,
            _optional_uuid(args.service, label="service"),
        )
    if args.command == "storage" and args.storage_command == "status":
        return StorageStatusInvocation(
            output_format,
            _optional_uuid(args.service, label="service"),
        )
    if args.command == "service" and args.service_command == "list":
        return ServiceListInvocation(output_format, parse_limit(args.limit))
    if args.command == "service" and args.service_command == "inspect":
        return ServiceInspectInvocation(
            output_format,
            parse_canonical_uuid(args.service_id, label="service"),
        )
    if args.command == "service" and args.service_command in {
        "suspend",
        "resume",
        "archive",
        "restore",
    }:
        return ServiceLifecycleInvocation(
            output_format=output_format,
            mode=mode,
            action=CliLifecycleAction(args.service_command),
            service_id=parse_canonical_uuid(args.service_id, label="service"),
            expected_revision=parse_positive_revision(args.expected_revision),
            operation_id=_optional_uuid(args.operation_id, label="operation"),
            assume_yes=assume_yes,
        )
    if args.command == "service" and args.service_command == "purge":
        return ServicePurgeInvocation(
            output_format=output_format,
            mode=mode,
            service_id=parse_canonical_uuid(args.service_id, label="service"),
            expected_revision=parse_positive_revision(args.expected_revision),
            operation_id=_optional_uuid(args.operation_id, label="operation"),
            receipt_id=_optional_uuid(args.receipt, label="receipt"),
            explicit_override=args.override,
            confirmed_service_id=_optional_uuid(
                args.confirm_service_id, label="confirmed service"
            ),
            assume_yes=assume_yes,
        )
    if args.command == "export":
        return ExportInvocation(
            output_format=output_format,
            mode=mode,
            service_id=parse_canonical_uuid(args.service_id, label="service"),
            expected_revision=parse_positive_revision(args.expected_revision),
            operation_id=_optional_uuid(args.operation_id, label="operation"),
            output_path=args.output,
            assume_yes=assume_yes,
        )
    if args.command == "import":
        return ImportInvocation(
            output_format=output_format,
            mode=mode,
            input_path=args.input,
            dry_run=args.dry_run,
            operation_id=_optional_uuid(args.operation_id, label="operation"),
            assume_yes=assume_yes,
        )
    if args.command == "maintenance" and args.maintenance_command == "scan":
        return MaintenanceScanInvocation(
            output_format,
            parse_canonical_uuid(args.service, label="service"),
        )
    raise AipcsContractError(
        ErrorCode.INVALID_REQUEST, "Administration command is invalid."
    )


def _optional_uuid(value: object | None, *, label: str) -> UUID | None:
    return None if value is None else parse_canonical_uuid(value, label=label)


def _run_admin(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    """Resolve and execute the currently composed administration slice."""

    _preflight(args, environ)
    invocation = _parse_admin_invocation(args)
    if not isinstance(
        invocation,
        (
            StatusInvocation,
            DoctorInvocation,
            StorageStatusInvocation,
            ServiceListInvocation,
            ServiceInspectInvocation,
            ServiceLifecycleInvocation,
            ServicePurgeInvocation,
            ExportInvocation,
            ImportInvocation,
            MaintenanceScanInvocation,
        ),
    ):
        raise AipcsContractError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "Administration command is not available in this implementation slice.",
        )
    resolved = _resolve(args, runnable=True, environ=environ)
    try:
        runtime = compose_admin_runtime(resolved, environ=environ)
    except Exception:
        raise AipcsContractError(
            ErrorCode.STORAGE_UNAVAILABLE,
            "Administration storage is unavailable.",
        ) from None
    application = runtime.inspection
    context = runtime.context
    try:
        if type(invocation) is StatusInvocation:
            result = project_status(application.status(context))
        elif type(invocation) is DoctorInvocation:
            result = project_doctor(application.doctor(context, invocation.service_id))
        elif type(invocation) is StorageStatusInvocation:
            _require_persistent_admin(resolved.profile)
            result = project_migration(
                application.storage_status(context, invocation.service_id)
            )
        elif type(invocation) is ServiceListInvocation:
            _require_persistent_admin(resolved.profile)
            result = {
                "services": [
                    service.model_dump(mode="json", by_alias=True, warnings="error")
                    for service in application.service_list(context, invocation.limit)
                ]
            }
        elif type(invocation) is ServiceInspectInvocation:
            _require_persistent_admin(resolved.profile)
            result = application.service_inspect(
                context, invocation.service_id
            ).model_dump(mode="json", by_alias=True, warnings="error")
        elif type(invocation) is MaintenanceScanInvocation:
            _require_persistent_admin(resolved.profile)
            value = application.maintenance_scan(context, invocation.service_id)
            if type(value) is DataFailure:
                raise _data_failure(value)
            result = project_maintenance(value).model_dump(
                mode="json", warnings="error"
            )
        elif type(invocation) is ServiceLifecycleInvocation:
            _require_persistent_admin(resolved.profile)
            result = _run_lifecycle(runtime, invocation)
        elif type(invocation) is ExportInvocation:
            _require_persistent_admin(resolved.profile)
            result = _run_export(runtime, invocation)
        elif type(invocation) is ImportInvocation:
            _require_persistent_admin(resolved.profile)
            result = _run_import(runtime, invocation, resolved.profile)
        elif type(invocation) is ServicePurgeInvocation:
            _require_persistent_admin(resolved.profile)
            result = _run_purge(runtime, invocation)
        else:
            raise AipcsContractError(
                ErrorCode.UNSUPPORTED_OPERATION,
                "Administration command is not available in this implementation slice.",
            )
    except AipcsContractError:
        raise
    except ApplicationError as error:
        raise _application_failure(error) from None
    except Exception:
        raise AipcsContractError(
            ErrorCode.INTERNAL_ERROR,
            "The administration command could not be completed safely.",
        ) from None
    _write_success(result, invocation.output_format.value)
    return 0


def _run_purge(
    runtime: object, invocation: ServicePurgeInvocation
) -> dict[str, object]:
    inspection, context, portable = _portable_runtime(runtime)
    _require_ready_registry(inspection)
    expected_revision = invocation.expected_revision
    operation_id = invocation.operation_id
    current = None
    if expected_revision is None or operation_id is None:
        current = inspection.service_inspect(context, invocation.service_id)
        expected_revision = (
            current.service_revision
            if expected_revision is None
            else expected_revision
        )
        operation_id = uuid4() if operation_id is None else operation_id
    completed = replace(
        invocation,
        expected_revision=expected_revision,
        operation_id=operation_id,
    )
    if (
        completed.confirmed_service_id is not None
        and completed.confirmed_service_id != completed.service_id
    ):
        raise AipcsContractError(
            ErrorCode.VALIDATION_FAILED,
            "The confirmed service identity does not match.",
        )
    if invocation.mode is CliExecutionMode.INTERACTIVE:
        if current is None:
            current = inspection.service_inspect(context, invocation.service_id)
        if not _confirm_purge(completed, current.operational_status):
            raise AipcsContractError(
                ErrorCode.INVALID_STATE,
                "The purge operation was not confirmed.",
            )
    assert completed.expected_revision is not None
    assert completed.operation_id is not None
    authority = (
        PurgeAuthority(PurgeAuthorityKind.EXPLICIT_OVERRIDE)
        if completed.explicit_override
        else PurgeAuthority(
            PurgeAuthorityKind.VERIFIED_RECEIPT,
            completed.receipt_id,
        )
    )
    outcome = _compose_portable(portable).purge(
        PurgeCommand(
            context.principal_id,
            "cli",
            completed.service_id,
            completed.expected_revision,
            str(completed.operation_id),
            authority,
        )
    )
    if (
        type(outcome) is not PortableExecutionResult
        or outcome.category is not PortableResultCategory.COMPLETED
        or outcome.tombstone is None
    ):
        raise _portable_failure(outcome)
    return {
        "operation": "purge",
        "operation_id": str(completed.operation_id),
        "tombstone": _project_tombstone(outcome.tombstone),
    }


def _run_export(runtime: object, invocation: ExportInvocation) -> dict[str, object]:
    inspection, context, portable = _portable_runtime(runtime)
    _require_ready_registry(inspection)
    coordinator = _compose_portable(portable)
    expected_revision = invocation.expected_revision
    operation_id = invocation.operation_id
    current = None
    if expected_revision is None or operation_id is None:
        current = inspection.service_inspect(context, invocation.service_id)
        expected_revision = (
            current.service_revision
            if expected_revision is None
            else expected_revision
        )
        operation_id = uuid4() if operation_id is None else operation_id
    completed = replace(
        invocation,
        expected_revision=expected_revision,
        operation_id=operation_id,
    )
    if (
        invocation.mode is CliExecutionMode.INTERACTIVE
        and confirmation_for(invocation) is CliConfirmation.GENERATED_VALUES
    ):
        if current is None:
            current = inspection.service_inspect(context, invocation.service_id)
        if not _confirm_export(completed, current.operational_status):
            raise AipcsContractError(
                ErrorCode.INVALID_STATE,
                "The export operation was not confirmed.",
            )
    assert completed.expected_revision is not None
    assert completed.operation_id is not None
    command = ExportCommand(
        context.principal_id,
        "cli",
        completed.service_id,
        completed.expected_revision,
        str(completed.operation_id),
    )
    try:
        with ExclusiveExportFile(completed.output_path) as output:
            outcome = coordinator.export(command, output.stream)
            if (
                type(outcome) is not PortableExecutionResult
                or outcome.category is not PortableResultCategory.COMPLETED
                or not outcome.artifact_written
                or outcome.summary is None
                or outcome.receipt is None
            ):
                raise _portable_failure(outcome)
            output.publish()
    except AdminFileError as error:
        raise _file_failure(error) from None
    return {
        "operation": "export",
        "operation_id": str(completed.operation_id),
        "summary": _project_bundle_summary(outcome.summary),
        "receipt": _project_receipt(outcome.receipt),
    }


def _run_import(
    runtime: object,
    invocation: ImportInvocation,
    profile: str,
) -> dict[str, object]:
    inspection, context, portable = _portable_runtime(runtime)
    coordinator = _compose_portable(portable)
    backend = _storage_backend(profile)
    validation_request = ImportBundleRequest(
        context.principal_id,
        "cli",
        backend,
        "dry-run",
        True,
    )
    try:
        with open_import_file(invocation.input_path) as source:
            validated = coordinator.import_bundle(validation_request, source)
    except AdminFileError as error:
        raise _file_failure(error) from None
    if (
        type(validated) is not PortableExecutionResult
        or validated.category is not PortableResultCategory.VALIDATED
        or validated.summary is None
        or validated.service is None
    ):
        raise _portable_failure(validated)
    validation_result = {
        "operation": "import_dry_run",
        "summary": _project_bundle_summary(validated.summary),
        "service": _project_bundle_service(validated.service),
    }
    if invocation.dry_run:
        return validation_result

    operation_id = invocation.operation_id
    if operation_id is None:
        operation_id = uuid4()
    if invocation.mode is CliExecutionMode.INTERACTIVE and not _confirm_import(
        validated, backend, operation_id
    ):
        raise AipcsContractError(
            ErrorCode.INVALID_STATE,
            "The import operation was not confirmed.",
        )
    _require_ready_registry(inspection)
    request = ImportBundleRequest(
        context.principal_id,
        "cli",
        backend,
        str(operation_id),
        False,
    )
    try:
        with open_import_file(invocation.input_path) as source:
            outcome = coordinator.import_bundle(request, source)
    except AdminFileError as error:
        raise _file_failure(error) from None
    if (
        type(outcome) is not PortableExecutionResult
        or outcome.category is not PortableResultCategory.COMPLETED
        or outcome.service is None
    ):
        raise _portable_failure(outcome)
    service = inspection.service_inspect(context, outcome.service.service_id)
    return {
        "operation": "import",
        "operation_id": str(operation_id),
        "summary": _project_bundle_summary(
            validated.summary if outcome.summary is None else outcome.summary
        ),
        "service": service.model_dump(
            mode="json", by_alias=True, warnings="error"
        ),
    }


def _portable_runtime(runtime: object) -> tuple[object, object, object]:
    inspection = getattr(runtime, "inspection", None)
    context = getattr(runtime, "context", None)
    portable = getattr(runtime, "portable", None)
    if inspection is None or context is None or not callable(portable):
        raise AipcsContractError(
            ErrorCode.INTERNAL_ERROR,
            "The portable runtime could not be composed safely.",
        )
    return inspection, context, portable


def _compose_portable(portable: object) -> object:
    if not callable(portable):
        raise AipcsContractError(
            ErrorCode.INTERNAL_ERROR,
            "The portable runtime could not be composed safely.",
        )
    try:
        return portable()
    except Exception:
        raise AipcsContractError(
            ErrorCode.STORAGE_UNAVAILABLE,
            "The portable runtime is unavailable.",
        ) from None


def _require_ready_registry(inspection: object) -> None:
    registry = inspection.registry_status()
    if registry.status == "ready":
        return
    code = (
        ErrorCode.STORAGE_UNAVAILABLE
        if registry.status == "unavailable"
        else ErrorCode.STORAGE_MIGRATION_REQUIRED
    )
    raise AipcsContractError(code, "The registry is not ready for portable work.")


def _storage_backend(profile: str) -> StorageBackend:
    try:
        return StorageBackend(profile)
    except ValueError:
        raise AipcsContractError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "Portable operations require persistent storage.",
        ) from None


def _confirm_export(invocation: ExportInvocation, current_status: str) -> bool:
    return _terminal_confirmation(
        "AIPCS export confirmation\n"
        f"service_id: {invocation.service_id}\n"
        f"operational_status: {current_status}\n"
        f"expected_revision: {invocation.expected_revision}\n"
        f"operation_id: {invocation.operation_id}\n"
        "Confirm [y/N]: "
    )


def _confirm_purge(
    invocation: ServicePurgeInvocation, current_status: str
) -> bool:
    authority = (
        "explicit_override"
        if invocation.explicit_override
        else f"verified_receipt:{invocation.receipt_id}"
    )
    prompt = (
        "AIPCS PURGE confirmation\n"
        f"service_id: {invocation.service_id}\n"
        f"operational_status: {current_status}\n"
        f"expected_revision: {invocation.expected_revision}\n"
        f"operation_id: {invocation.operation_id}\n"
        f"authority: {authority}\n"
        f"Type the service id to purge ({invocation.service_id}): "
    )
    try:
        with Path("/dev/tty").open("r+", encoding="utf-8", buffering=1) as terminal:
            terminal.write(prompt)
            answer = terminal.readline(64)
    except OSError:
        raise AipcsContractError(
            ErrorCode.INVALID_REQUEST,
            "Interactive confirmation terminal is unavailable.",
        ) from None
    return answer.strip() == str(invocation.service_id)


def _confirm_import(
    validated: PortableExecutionResult,
    backend: StorageBackend,
    operation_id: UUID,
) -> bool:
    assert validated.service is not None and validated.summary is not None
    return _terminal_confirmation(
        "AIPCS import confirmation\n"
        f"service_id: {validated.service.service_id}\n"
        f"domain_name: {validated.service.domain_name}\n"
        f"source_backend: {validated.summary.header.source_backend}\n"
        f"destination_backend: {backend.value}\n"
        f"frame_count: {validated.summary.frame_count}\n"
        f"root_sha256: {validated.summary.root_sha256}\n"
        f"operation_id: {operation_id}\n"
        "Confirm [y/N]: "
    )


def _terminal_confirmation(prompt: str) -> bool:
    try:
        with Path("/dev/tty").open("r+", encoding="utf-8", buffering=1) as terminal:
            terminal.write(prompt)
            answer = terminal.readline(16)
    except OSError:
        raise AipcsContractError(
            ErrorCode.INVALID_REQUEST,
            "Interactive confirmation terminal is unavailable.",
        ) from None
    return answer.strip().casefold() in {"y", "yes"}


def _project_bundle_summary(value: PortableBundleSummary) -> dict[str, object]:
    return {
        "export_format_version": value.header.export_format_version,
        "source_backend": value.header.source_backend,
        "root_sha256": value.root_sha256,
        "frame_count": value.frame_count,
        "total_byte_count": value.total_byte_count,
        "sections": {
            section.kind: section.count for section in value.sections
        },
    }


def _project_receipt(value: TransferReceipt) -> dict[str, object]:
    return {
        "receipt_id": str(value.receipt_id),
        "kind": value.kind.value,
        "service_id": str(value.service_id),
        "bundle_root_sha256": value.bundle_root_sha256,
        "export_format_version": value.export_format_version,
        "backend": value.storage_backend.value,
        "service_revision": value.service_revision,
        "operational_status": value.operational_status,
        "verification": value.verification.value,
        "created_at": value.created_at.isoformat().replace("+00:00", "Z"),
    }


def _project_tombstone(value: PurgeTombstone) -> dict[str, object]:
    return {
        "service_id": str(value.service_id),
        "purged_at": value.purged_at.isoformat().replace("+00:00", "Z"),
        "authority": value.authority.kind.value,
        "receipt_id": None if value.receipt_id is None else str(value.receipt_id),
    }


def _project_bundle_service(value: object) -> dict[str, object]:
    return {
        "service_id": str(value.service_id),
        "domain_name": value.domain_name,
        "domain_class": value.domain_class,
        "intent_description": value.intent_description,
        "design_state": value.design_state,
        "operational_status": value.operational_status,
        "schema_version": value.schema_version,
        "service_revision": value.service_revision,
    }


def _file_failure(error: AdminFileError) -> AipcsContractError:
    if error.code == "already_exists":
        return AipcsContractError(
            ErrorCode.ALREADY_EXISTS,
            "The destination file already exists.",
        )
    if error.code == "invalid_path":
        return AipcsContractError(
            ErrorCode.VALIDATION_FAILED,
            "The file path is invalid.",
        )
    if error.code == "not_regular":
        return AipcsContractError(
            ErrorCode.VALIDATION_FAILED,
            "The import source must be a regular file.",
        )
    if error.code == "publication_uncertain":
        return AipcsContractError(
            ErrorCode.OPERATION_UNCERTAIN,
            "Export publication outcome is uncertain.",
            retryable=True,
        )
    return AipcsContractError(
        ErrorCode.STORAGE_UNAVAILABLE,
        "The requested file is unavailable.",
    )


def _run_lifecycle(runtime: object, invocation: ServiceLifecycleInvocation) -> dict[str, object]:
    inspection = getattr(runtime, "inspection", None)
    context = getattr(runtime, "context", None)
    portable = getattr(runtime, "portable", None)
    if inspection is None or context is None or not callable(portable):
        raise AipcsContractError(
            ErrorCode.INTERNAL_ERROR,
            "The lifecycle runtime could not be composed safely.",
        )
    registry = inspection.registry_status()
    if registry.status != "ready":
        code = (
            ErrorCode.STORAGE_UNAVAILABLE
            if registry.status == "unavailable"
            else ErrorCode.STORAGE_MIGRATION_REQUIRED
        )
        raise AipcsContractError(code, "The registry is not ready for lifecycle work.")

    completed = _complete_lifecycle_invocation(invocation, inspection, context)
    assert completed.expected_revision is not None
    assert completed.operation_id is not None
    command_type = {
        CliLifecycleAction.SUSPEND: SuspendCommand,
        CliLifecycleAction.RESUME: ResumeCommand,
        CliLifecycleAction.ARCHIVE: ArchiveCommand,
        CliLifecycleAction.RESTORE: RestoreCommand,
    }[completed.action]
    command = command_type(
        context.principal_id,
        "cli",
        completed.service_id,
        completed.expected_revision,
        str(completed.operation_id),
    )
    outcome = portable().transition(command)
    if (
        type(outcome) is not PortableExecutionResult
        or outcome.category is not PortableResultCategory.COMPLETED
    ):
        raise _portable_failure(outcome)
    service = inspection.service_inspect(context, completed.service_id)
    return {
        "operation": completed.action.value,
        "operation_id": str(completed.operation_id),
        "service": service.model_dump(
            mode="json", by_alias=True, warnings="error"
        ),
    }


def _complete_lifecycle_invocation(
    invocation: ServiceLifecycleInvocation, inspection: object, context: object
) -> ServiceLifecycleInvocation:
    expected_revision = invocation.expected_revision
    operation_id = invocation.operation_id
    generated = expected_revision is None or operation_id is None
    current = None
    if generated:
        current = inspection.service_inspect(context, invocation.service_id)
        if expected_revision is None:
            expected_revision = current.service_revision
        if operation_id is None:
            operation_id = uuid4()
    completed = replace(
        invocation,
        expected_revision=expected_revision,
        operation_id=operation_id,
    )
    confirmation = confirmation_for(invocation)
    if (
        invocation.mode is CliExecutionMode.INTERACTIVE
        and confirmation in {CliConfirmation.GENERATED_VALUES, CliConfirmation.ALWAYS}
    ):
        if current is None:
            current = inspection.service_inspect(context, invocation.service_id)
        if not _confirm_lifecycle(completed, current.operational_status):
            raise AipcsContractError(
                ErrorCode.INVALID_STATE,
                "The lifecycle operation was not confirmed.",
            )
    return completed


def _confirm_lifecycle(
    invocation: ServiceLifecycleInvocation, current_status: str
) -> bool:
    target = {
        CliLifecycleAction.SUSPEND: "suspended",
        CliLifecycleAction.RESUME: "active",
        CliLifecycleAction.ARCHIVE: "archived",
        CliLifecycleAction.RESTORE: "suspended",
    }[invocation.action]
    return _terminal_confirmation(
        "AIPCS lifecycle confirmation\n"
        f"service_id: {invocation.service_id}\n"
        f"transition: {current_status} -> {target}\n"
        f"expected_revision: {invocation.expected_revision}\n"
        f"operation_id: {invocation.operation_id}\n"
        "Confirm [y/N]: "
    )


_PORTABLE_FAILURES: dict[
    PortableResultCategory, tuple[ErrorCode, str, bool]
] = {
    PortableResultCategory.MALFORMED_INPUT: (
        ErrorCode.VALIDATION_FAILED,
        "The lifecycle command is invalid.",
        False,
    ),
    PortableResultCategory.CHANGED_FINGERPRINT: (
        ErrorCode.CHANGED_FINGERPRINT,
        "The operation id cannot be reused for a different request.",
        False,
    ),
    PortableResultCategory.STALE_REVISION: (
        ErrorCode.STALE_REVISION,
        "The service revision precondition is stale.",
        False,
    ),
    PortableResultCategory.UNSUPPORTED_TRANSITION: (
        ErrorCode.UNSUPPORTED_TRANSITION,
        "The lifecycle transition is not supported from the current state.",
        False,
    ),
    PortableResultCategory.OPERATION_IN_PROGRESS: (
        ErrorCode.OPERATION_IN_PROGRESS,
        "Another lifecycle operation is in progress.",
        True,
    ),
    PortableResultCategory.IDENTITY_COLLISION: (
        ErrorCode.ALREADY_EXISTS,
        "The operation conflicts with an existing identity.",
        False,
    ),
    PortableResultCategory.TOMBSTONED: (
        ErrorCode.CONFLICT,
        "The service identity is tombstoned.",
        False,
    ),
    PortableResultCategory.RECOVERY_REQUIRED: (
        ErrorCode.RECOVERY_REQUIRED,
        "The service requires recovery before more lifecycle work.",
        False,
    ),
    PortableResultCategory.OPERATION_UNCERTAIN: (
        ErrorCode.OPERATION_UNCERTAIN,
        "The operation outcome is uncertain; retry with the same operation id.",
        True,
    ),
    PortableResultCategory.STORAGE_BUSY: (
        ErrorCode.STORAGE_BUSY,
        "Storage is temporarily busy.",
        True,
    ),
    PortableResultCategory.STORAGE_UNAVAILABLE: (
        ErrorCode.STORAGE_UNAVAILABLE,
        "Storage is unavailable.",
        False,
    ),
    PortableResultCategory.INTERNAL_FAILURE: (
        ErrorCode.INTERNAL_ERROR,
        "The lifecycle operation could not be completed safely.",
        False,
    ),
    PortableResultCategory.VALIDATED: (
        ErrorCode.INTERNAL_ERROR,
        "The lifecycle operation returned an invalid result.",
        False,
    ),
    PortableResultCategory.COMPLETED: (
        ErrorCode.INTERNAL_ERROR,
        "The lifecycle operation returned an invalid result.",
        False,
    ),
}


def _portable_failure(value: object) -> AipcsContractError:
    category = (
        value.category
        if type(value) is PortableExecutionResult
        else PortableResultCategory.INTERNAL_FAILURE
    )
    code, message, retryable = _PORTABLE_FAILURES[category]
    return AipcsContractError(code, message, retryable=retryable)


def _require_persistent_admin(profile: str) -> None:
    if profile == "stateless":
        raise AipcsContractError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "Persistent administration is not available for this profile.",
        )


def _application_failure(error: ApplicationError) -> AipcsContractError:
    try:
        code = ErrorCode(error.code)
    except ValueError:
        code = ErrorCode.INTERNAL_ERROR
    return AipcsContractError(code, error.message)


_ADMIN_DATA_FAILURES: dict[str, tuple[ErrorCode, str, bool]] = {
    "validation_failed": (
        ErrorCode.VALIDATION_FAILED,
        "Administration input failed validation.",
        False,
    ),
    "not_found": (ErrorCode.NOT_FOUND, "The requested service was not found.", False),
    "storage_migration_required": (
        ErrorCode.STORAGE_MIGRATION_REQUIRED,
        "Service storage requires migration.",
        False,
    ),
    "storage_busy": (ErrorCode.STORAGE_BUSY, "Storage is temporarily busy.", True),
    "operation_uncertain": (
        ErrorCode.OPERATION_UNCERTAIN,
        "The operation outcome is uncertain.",
        True,
    ),
    "storage_unavailable": (
        ErrorCode.STORAGE_UNAVAILABLE,
        "Storage is unavailable.",
        False,
    ),
    "internal_error": (
        ErrorCode.INTERNAL_ERROR,
        "The administration command could not be completed safely.",
        False,
    ),
}


def _data_failure(value: DataFailure) -> AipcsContractError:
    mapped = _ADMIN_DATA_FAILURES.get(value.code)
    if mapped is None:
        return AipcsContractError(
            ErrorCode.INTERNAL_ERROR,
            "The administration command could not be completed safely.",
        )
    code, message, retryable = mapped
    return AipcsContractError(code, message, retryable=retryable)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the stdio server or return a public CLI status code."""

    args = build_parser().parse_args(argv)
    environment = dict(os.environ)
    try:
        if args.command == "serve":
            return _run_serve(args, environment)
        if args.command == "config" and args.config_command == "show":
            return _run_config_show(args, environment)
        if args.command == "config" and args.config_command == "validate":
            return _run_config_validate(args, environment)
        if args.command in {
            "status",
            "doctor",
            "storage",
            "service",
            "export",
            "import",
            "maintenance",
        }:
            return _run_admin(args, environment)
    except AipcsContractError as error:
        output_format = getattr(args, "output_format", "json")
        _write_contract_error(error, output_format)
        if args.command in {"serve", "config"}:
            return 2
        return int(exit_for_error(error))
    return 2
