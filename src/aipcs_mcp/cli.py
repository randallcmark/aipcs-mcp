"""Stdio-only command-line entry point for AIPCS MCP."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .contracts import validate_stdio_only
from .errors import AipcsContractError
from .mcp_server import create_server


def build_parser() -> argparse.ArgumentParser:
    """Build the intentionally small public CLI surface."""

    parser = argparse.ArgumentParser(prog="aipcs")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run the local AIPCS MCP server over stdio.")
    serve.add_argument(
        "--transport",
        default="stdio",
        help="Transport to use; public v1 accepts stdio only.",
    )
    return parser


def _write_contract_error(error: AipcsContractError) -> None:
    """Write one compact, stable failure envelope to stderr."""

    print(error.envelope().model_dump_json(), file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the stdio server or return a public CLI status code."""

    args = build_parser().parse_args(argv)
    if args.command != "serve":
        return 2

    try:
        validate_stdio_only(args.transport)
    except AipcsContractError as error:
        _write_contract_error(error)
        return 2

    server = create_server()
    server.run(transport="stdio")
    return 0
