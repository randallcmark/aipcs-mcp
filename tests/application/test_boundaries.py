"""Static guards for the internal application boundary."""

from __future__ import annotations

import argparse
import ast
from collections.abc import Iterable
from dataclasses import fields
from pathlib import Path
from uuid import UUID

import pytest

from aipcs_mcp.application.models import AuditEvent
from aipcs_mcp.cli import build_parser
from aipcs_mcp.contracts import ServiceMetadata

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT / "src" / "aipcs_mcp"
APPLICATION_ROOT = PACKAGE_ROOT / "application"
STORAGE_ROOT = PACKAGE_ROOT / "storage"
RELATIONAL_MODULE = PACKAGE_ROOT / "relational.py"

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


class _TextSubclass(str):
    pass


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


def test_storage_contract_package_has_no_adapter_transport_or_configuration_dependencies() -> None:
    assert STORAGE_ROOT.is_dir(), "The V1-06A storage package is required."
    forbidden = FORBIDDEN_IMPORT_PARTS | {"storage", "importlib"}
    modules = [STORAGE_ROOT / name for name in ("__init__.py", "contracts.py", "errors.py")]
    assert all(module.is_file() for module in modules)
    for module in modules:
        tree = ast.parse(module.read_text(), filename=str(module))
        imported_parts = {
            part.casefold() for imported in _imported_modules(tree) for part in imported.split(".")
        }
        assert not imported_parts & forbidden, module
        assert not _ast_names(tree) & (FORBIDDEN_NAMES | {"connect", "execute", "cursor"}), module


def test_relational_contract_has_only_manifest_and_standard_library_dependencies() -> None:
    tree = ast.parse(RELATIONAL_MODULE.read_text(), filename=str(RELATIONAL_MODULE))
    assert set(_imported_modules(tree)) == {
        "__future__",
        "dataclasses",
        "manifest_v2",
        "re",
        "typing",
    }
    assert not _ast_names(tree) & (
        FORBIDDEN_NAMES
        | {
            "connect",
            "cursor",
            "dsn",
            "execute",
            "postgresql",
            "registry",
            "sqlite",
        }
    )


def test_application_does_not_depend_on_storage_contracts() -> None:
    for module in _production_modules(APPLICATION_ROOT):
        tree = ast.parse(module.read_text(), filename=str(module))
        assert "storage" not in {
            part.casefold() for imported in _imported_modules(tree) for part in imported.split(".")
        }, module


def test_storage_values_are_allowlisted_and_reject_hostile_locator_input() -> None:
    from aipcs_mcp.storage import (  # noqa: PLC0415
        MigrationState,
        ServiceStoreLocator,
        StorageAdapterInfo,
        StorageContractError,
    )

    assert [field.name for field in fields(ServiceStoreLocator)] == ["backend", "namespace"]
    assert [field.name for field in fields(StorageAdapterInfo)] == ["backend", "supported_components"]
    assert [field.name for field in fields(MigrationState)] == [
        "component",
        "applied_revision",
        "target_revision",
        "status",
    ]
    valid = ServiceStoreLocator.for_service("sqlite", UUID(int=1))
    assert valid.namespace == "svc_00000000000000000000000000000001"
    for hostile in (
        "../../private",
        "file:" + "//host/share/store",
        "postgresql://secret@host/private",
        "svc_%2f",
        "svc_0000000000000000000000000000000A",
        "svc_0000000000000000000000000000000é",
        "svc_0000000000000000000000000000000 ",
        "svc_00000000000000000000000000000000",
    ):
        with pytest.raises(StorageContractError) as captured:
            ServiceStoreLocator("sqlite", hostile)
        assert hostile not in str(captured.value)
        assert hostile not in repr(captured.value)
        assert captured.value.args == (StorageContractError.message,)
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None

    for backend in ("mysql", "sqlite://host/database", "SQLite", "", 1, None):
        with pytest.raises(StorageContractError):
            ServiceStoreLocator(backend, valid.namespace)
    with pytest.raises(StorageContractError):
        ServiceStoreLocator(_TextSubclass("sqlite"), valid.namespace)
    with pytest.raises(StorageContractError):
        ServiceStoreLocator("sqlite", _TextSubclass(valid.namespace))
    for service_id in (UUID(int=0), "00000000-0000-0000-0000-000000000001", None):
        with pytest.raises(StorageContractError):
            ServiceStoreLocator.for_service("sqlite", service_id)


def test_migration_state_keeps_readiness_separate_from_adapter_information() -> None:
    from aipcs_mcp.storage import (  # noqa: PLC0415
        MigrationState,
        StorageAdapterInfo,
        StorageContractError,
    )

    assert StorageAdapterInfo("sqlite", frozenset({"registry"})).supported_components == {
        "registry"
    }
    assert MigrationState("registry", 0, 1, "uninitialised").applied_revision == 0
    assert MigrationState("service_store", 1, 1, "ready").status == "ready"
    for invalid in (
        ("registry", 1, 1, "uninitialised"),
        ("registry", 0, 1, "ready"),
        ("registry", -1, 1, "dirty"),
        ("registry", 1, 0, "dirty"),
        ("domain", 1, 1, "ready"),
        ("registry", True, 1, "dirty"),
        ("registry", 1, True, "dirty"),
    ):
        with pytest.raises(StorageContractError):
            MigrationState(*invalid)
    with pytest.raises(StorageContractError):
        StorageAdapterInfo("sqlite", frozenset({"registry", "records"}))
    for components in ({"registry"}, frozenset(), frozenset({_TextSubclass("registry")})):
        with pytest.raises(StorageContractError):
            StorageAdapterInfo("sqlite", components)
    for invalid in (
        (_TextSubclass("registry"), 1, 1, "ready"),
        ("registry", 1, 1, _TextSubclass("ready")),
    ):
        with pytest.raises(StorageContractError):
            MigrationState(*invalid)
    assert MigrationState("registry", 0, 1, "incompatible").status == "incompatible"
    assert MigrationState("registry", 1, 1, "dirty").status == "dirty"
    assert MigrationState("registry", 2, 1, "incompatible").status == "incompatible"


def test_application_and_audit_projections_remain_allowlisted() -> None:
    assert [field.name for field in fields(AuditEvent)] == [
        "action",
        "outcome",
        "service_id",
        "principal_id",
        "created_via",
        "at",
    ]
    assert tuple(ServiceMetadata.model_fields) == (
        "service_id",
        "domain_name",
        "domain_class",
        "intent_description",
        "design_state",
        "operational_status",
        "schema_",
        "schema_version",
        "service_revision",
        "recovery_state",
        "created_at",
        "updated_at",
        "last_activity_at",
        "materialised_at",
        "storage",
    )


def test_cli_command_surface_is_serve_and_config_show_validate() -> None:
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    assert tuple(subparsers.choices) == ("serve", "config")

    config_parser = subparsers.choices["config"]
    config_subparsers = next(
        action
        for action in config_parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert tuple(config_subparsers.choices) == ("show", "validate")

    # The existing real stdio smoke owns the MCP tool-list assertion.
