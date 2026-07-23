#!/usr/bin/env python3
"""Fail closed when package dependencies cross the documented application boundary."""

from __future__ import annotations

import argparse
import ast
from collections.abc import Iterable
from pathlib import Path

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
        "configuration",
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
RELATIONAL_IMPORTS = frozenset({"__future__", "dataclasses", "manifest_v2", "re", "typing"})


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


def _import_parts(tree: ast.AST) -> set[str]:
    return {
        part.casefold() for imported in _imported_modules(tree) for part in imported.split(".")
    }


def _ast_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id.casefold())
        elif isinstance(node, ast.Attribute):
            names.add(node.attr.casefold())
    return names


def _tree(module: Path, root: Path, violations: list[str]) -> ast.AST | None:
    try:
        return ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    except (OSError, SyntaxError, UnicodeError):
        violations.append(f"{module.relative_to(root)}: unreadable or invalid Python")
        return None


def check(root: Path) -> tuple[str, ...]:
    """Return stable, path-bounded violations for one source checkout."""

    root = root.resolve()
    package_root = root / "src" / "aipcs_mcp"
    application_root = package_root / "application"
    storage_root = package_root / "storage"
    relational_module = package_root / "relational.py"
    violations: list[str] = []

    application_modules = (
        _production_modules(application_root) if application_root.is_dir() else []
    )
    if not application_modules:
        violations.append("src/aipcs_mcp/application: package is missing or empty")

    for module in application_modules:
        tree = _tree(module, root, violations)
        if tree is None:
            continue
        imported_parts = _import_parts(tree)
        forbidden_imports = sorted(imported_parts & FORBIDDEN_IMPORT_PARTS)
        forbidden_names = sorted(_ast_names(tree) & FORBIDDEN_NAMES)
        generators = sorted(
            fragment for fragment in FORBIDDEN_GENERATORS if fragment in module.read_text()
        )
        if forbidden_imports:
            violations.append(
                f"{module.relative_to(root)}: forbidden imports: {', '.join(forbidden_imports)}"
            )
        if "storage" in imported_parts:
            violations.append(f"{module.relative_to(root)}: application imports storage")
        if forbidden_names:
            violations.append(
                f"{module.relative_to(root)}: forbidden names: {', '.join(forbidden_names)}"
            )
        if generators:
            violations.append(
                f"{module.relative_to(root)}: nondeterministic generators: {', '.join(generators)}"
            )

    for module in _production_modules(package_root) if package_root.is_dir() else []:
        tree = _tree(module, root, violations)
        if tree is not None:
            forbidden = sorted(_import_parts(tree) & {"tests", "test", "fakes", "fake"})
            if forbidden:
                violations.append(
                    f"{module.relative_to(root)}: production imports tests: {', '.join(forbidden)}"
                )

    storage_forbidden = FORBIDDEN_IMPORT_PARTS | {"storage", "importlib"}
    for name in ("__init__.py", "contracts.py", "errors.py"):
        module = storage_root / name
        if not module.is_file():
            violations.append(f"{module.relative_to(root)}: required storage contract is missing")
            continue
        tree = _tree(module, root, violations)
        if tree is None:
            continue
        forbidden_imports = sorted(_import_parts(tree) & storage_forbidden)
        forbidden_names = sorted(
            _ast_names(tree) & (FORBIDDEN_NAMES | {"connect", "execute", "cursor"})
        )
        if forbidden_imports:
            violations.append(
                f"{module.relative_to(root)}: forbidden imports: {', '.join(forbidden_imports)}"
            )
        if forbidden_names:
            violations.append(
                f"{module.relative_to(root)}: forbidden names: {', '.join(forbidden_names)}"
            )

    if not relational_module.is_file():
        violations.append("src/aipcs_mcp/relational.py: required relational contract is missing")
    else:
        tree = _tree(relational_module, root, violations)
        if tree is not None:
            imports = set(_imported_modules(tree))
            if imports != RELATIONAL_IMPORTS:
                violations.append(
                    "src/aipcs_mcp/relational.py: imports differ from the boundary allowlist"
                )
            relational_names = FORBIDDEN_NAMES | {
                "connect",
                "cursor",
                "dsn",
                "execute",
                "postgresql",
                "registry",
                "sqlite",
            }
            forbidden_names = sorted(_ast_names(tree) & relational_names)
            if forbidden_names:
                violations.append(
                    "src/aipcs_mcp/relational.py: forbidden names: "
                    + ", ".join(forbidden_names)
                )

    return tuple(sorted(set(violations)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="source checkout to validate",
    )
    args = parser.parse_args()
    violations = check(args.root)
    if violations:
        for violation in violations:
            print(violation)
        return 1
    print("application boundary checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
