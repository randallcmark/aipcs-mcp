"""Stdio-only command-line entry point for AIPCS MCP."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID

import anyio

from .admin_cli import (
    AdminInvocation,
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
    execution_mode,
    exit_for_error,
    parse_canonical_uuid,
    parse_limit,
    parse_output_format,
    parse_positive_revision,
    render_human_error,
    render_human_result,
)
from .configuration.errors import to_contract_error
from .configuration.models import ConfigOverrides, ResolvedConfiguration
from .configuration.reporting import (
    safe_config_report,
    safe_validation_report,
)
from .configuration.resolver import require_runnable, resolve_configuration
from .contracts import validate_stdio_only
from .errors import AipcsContractError, ErrorCode, success
from .mcp_server import run_stdio
from .runtime import compose_server


def _add_configuration_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="Explicit TOML configuration file.")
    parser.add_argument("--profile", help="Execution profile override.")
    parser.add_argument("--transport", help="Public v1 accepts stdio only.")
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
    """Build the frozen V1-11 command tree without composing admin storage."""

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
    """Reject listeners before configuration work or server construction."""

    validate_stdio_only(args.transport, environ=environ)


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
        anyio.run(run_stdio, server)
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


def _run_unavailable_admin(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    """Validate C1 syntax without composing a registry, store, or admin runtime."""

    _preflight(args, environ)
    _parse_admin_invocation(args)
    raise AipcsContractError(
        ErrorCode.UNSUPPORTED_OPERATION,
        "Administration command is not available in this implementation slice.",
    )


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
            return _run_unavailable_admin(args, environment)
    except AipcsContractError as error:
        output_format = getattr(args, "output_format", "json")
        _write_contract_error(error, output_format)
        if args.command in {"serve", "config"}:
            return 2
        return int(exit_for_error(error))
    return 2
