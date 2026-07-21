"""Static guards for configuration ownership and imports."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "src" / "aipcs_mcp"
CONFIGURATION_ROOT = PACKAGE_ROOT / "configuration"

FORBIDDEN_IMPORT_PARTS = frozenset(
    {
        "application",
        "mcp",
        "fastmcp",
        "sqlite3",
        "psycopg",
        "psycopg2",
        "asyncpg",
        "sqlalchemy",
        "tests",
        "test",
        "fakes",
        "fake",
        "adapter",
        "adapters",
    }
)


def _modules(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _imports(tree: ast.AST) -> Iterable[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                yield node.module
            else:
                yield from (alias.name for alias in node.names)


def test_configuration_package_has_no_runtime_or_test_dependencies() -> None:
    assert CONFIGURATION_ROOT.is_dir()
    modules = _modules(CONFIGURATION_ROOT)
    assert modules
    for module in modules:
        imported_parts = {
            part.casefold() for imported in _imports(ast.parse(module.read_text())) for part in imported.split(".")
        }
        assert not imported_parts & FORBIDDEN_IMPORT_PARTS, module


def test_application_package_does_not_import_configuration() -> None:
    for module in _modules(PACKAGE_ROOT / "application"):
        imported_parts = {
            part.casefold() for imported in _imports(ast.parse(module.read_text())) for part in imported.split(".")
        }
        assert "configuration" not in imported_parts, module
