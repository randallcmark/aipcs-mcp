"""Stdio-only command-line entry point for AIPCS MCP."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from .configuration.errors import to_contract_error
from .configuration.models import ConfigOverrides, ResolvedConfiguration
from .configuration.reporting import (
    safe_config_report,
    safe_validation_report,
)
from .configuration.resolver import require_runnable, resolve_configuration
from .contracts import validate_stdio_only
from .errors import AipcsContractError, success
from .mcp_server import create_server


def _add_configuration_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="Explicit TOML configuration file.")
    parser.add_argument("--profile", help="Execution profile override.")
    parser.add_argument("--transport", help="Public v1 accepts stdio only.")
    parser.add_argument("--principal-id", help="Principal identity override.")
    parser.add_argument("--sqlite-data-root", help="Future SQLite data-root descriptor.")
    parser.add_argument("--postgres-dsn-env", help="Future PostgreSQL DSN environment reference.")
    parser.add_argument("--log-level", help="Stderr logging level override.")


def build_parser() -> argparse.ArgumentParser:
    """Build the intentionally small public CLI surface."""

    parser = argparse.ArgumentParser(prog="aipcs")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Run the local AIPCS MCP server over stdio.")
    _add_configuration_options(serve)

    config = commands.add_parser("config", help="Inspect or validate AIPCS configuration.")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    show = config_commands.add_parser("show", help="Show a redacted configuration report.")
    _add_configuration_options(show)
    validate = config_commands.add_parser(
        "validate",
        help="Validate a runnable configuration without starting the server.",
    )
    _add_configuration_options(validate)
    return parser


def _write_contract_error(error: AipcsContractError) -> None:
    """Write one compact, stable failure envelope to stderr."""

    print(error.envelope().model_dump_json(), file=sys.stderr)


def _overrides_from_args(args: argparse.Namespace) -> ConfigOverrides:
    return ConfigOverrides(
        profile=args.profile,
        transport=args.transport,
        principal_id=args.principal_id,
        sqlite_data_root=args.sqlite_data_root,
        postgres_dsn_env=args.postgres_dsn_env,
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


def _write_success(result: dict[str, object]) -> None:
    print(success(result).model_dump_json())


def _preflight(args: argparse.Namespace, environ: Mapping[str, str]) -> None:
    """Reject listeners before configuration work or server construction."""

    validate_stdio_only(args.transport, environ=environ)


def _run_config_show(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    _preflight(args, environ)
    resolved = _resolve(args, runnable=False, environ=environ)
    _write_success(safe_config_report(resolved))
    return 0


def _run_config_validate(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    _preflight(args, environ)
    resolved = _resolve(args, runnable=True, environ=environ)
    _write_success(safe_validation_report(resolved))
    return 0


def _configure_stderr_logging(config: ResolvedConfiguration) -> None:
    """Apply the resolved runtime threshold while keeping protocol stdout untouched."""

    level = getattr(logging, config.log_level.upper())
    logging.basicConfig(level=level, stream=sys.stderr, force=True)
    logging.getLogger().setLevel(level)


def _run_serve(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    _preflight(args, environ)
    resolved = _resolve(args, runnable=True, environ=environ)
    _configure_stderr_logging(resolved)
    server = create_server()
    server.run(transport="stdio")
    return 0


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
    except AipcsContractError as error:
        _write_contract_error(error)
        return 2
    return 2
