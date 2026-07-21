"""Static guards for the internal application boundary."""

from __future__ import annotations

import argparse
import ast
from collections.abc import Iterable
from pathlib import Path

from aipcs_mcp.cli import build_parser

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT / "src" / "aipcs_mcp"
APPLICATION_ROOT = PACKAGE_ROOT / "application"

FORBIDDEN_IMPORT_PARTS = frozenset(
    {
        "mcp",
        "fastmcp",
        "sqlite3",
        "psycopg",
        "psycopg2",
        "asyncpg",
        "sqlalchemy",
        "pathlib",
        "os",
        "dotenv",
        "configparser",
        "tomllib",
        "yaml",
        "tests",
        "test",
        "fakes",
        "fake",
        "cli",
        "mcp_server",
        "transport",
    }
)
FORBIDDEN_NAMES = frozenset(
    {
        "open",
        "path",
        "purepath",
        "environ",
        "getenv",
        "getcwd",
        "chdir",
        "config",
        "settings",
        "__import__",
        "eval",
        "exec",
    }
)
FORBIDDEN_GENERATORS = ("datetime.now", "datetime.utcnow", "time.time", "uuid4", "random.")


def _production_modules(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _imported_modules(tree: ast.AST) -> Iterable[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                yield node.module
            else:
                yield from (alias.name for alias in node.names)


def _ast_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id.casefold())
        elif isinstance(node, ast.Attribute):
            names.add(node.attr.casefold())
    return names


def test_application_package_has_no_transport_infrastructure_or_test_dependencies() -> None:
    assert APPLICATION_ROOT.is_dir(), "The V1-04 application package is required."
    modules = _production_modules(APPLICATION_ROOT)
    assert modules, "The V1-04 application package must contain Python modules."

    for module in modules:
        source = module.read_text()
        tree = ast.parse(source, filename=str(module))
        imported_parts = {
            part.casefold() for imported in _imported_modules(tree) for part in imported.split(".")
        }
        assert not imported_parts & FORBIDDEN_IMPORT_PARTS, module
        assert not _ast_names(tree) & FORBIDDEN_NAMES, module
        assert not any(fragment in source for fragment in FORBIDDEN_GENERATORS), module


def test_package_code_never_imports_test_fakes() -> None:
    for module in _production_modules(PACKAGE_ROOT):
        tree = ast.parse(module.read_text(), filename=str(module))
        imported_parts = {
            part.casefold() for imported in _imported_modules(tree) for part in imported.split(".")
        }
        assert not imported_parts & {"tests", "test", "fakes", "fake"}, module


def test_cli_command_surface_remains_serve_only() -> None:
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    assert tuple(subparsers.choices) == ("serve",)

    # The existing real stdio smoke owns the MCP tool-list assertion.
