"""SYNTHETIC_FIXTURE. V1-07D proof that relational SQLite remains private."""

from __future__ import annotations

import ast
import importlib.util
import os
import sqlite3
from collections.abc import Iterable
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import anyio
from fixtures import entity, valid_manifest
from stdio_helpers import async_session, call_envelope, sqlite_parameters, successful

from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.relational import classify_transition, compile_manifest
from aipcs_mcp.storage import DomainSchemaState, DomainSchemaStore, ServiceStoreLocator
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteServiceStoreCatalog
from aipcs_mcp.storage.sqlite.domain_schema import SQLiteDomainSchemaStore
from aipcs_mcp.storage.sqlite.service_store_migrations import META, MIGRATION

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "src" / "aipcs_mcp"
_TOOL_NAMES = [
    "aipcs_server_info",
    "aipcs_service_seed",
    "aipcs_service_list",
    "aipcs_service_inspect",
    "aipcs_service_design",
]
_PRIVATE_RELATIONAL_MODULES = frozenset(
    {
        "aipcs_mcp.storage.sqlite.domain_schema",
        "aipcs_mcp.storage.sqlite.domain_schema_layout",
        "aipcs_mcp.storage.sqlite.sql_tokens",
    }
)


def _secure_parent(root: Path) -> None:
    os.chmod(root.parent, 0o700)


def _seed() -> dict[str, str]:
    return {
        "domain_name": "projects",
        "domain_class": "project_memory",
        "intent_description": "Keep compact synthetic project context.",
        "idempotency_key": "private-boundary-seed",
    }


def _v2(payload: dict[str, object]) -> dict[str, object]:
    result = deepcopy(payload)
    result["schema_version"] = 2
    result["migration_history"] = [
        {
            "from_schema_version": 1,
            "to_schema_version": 2,
            "operations": ["add task and project tag"],
        }
    ]
    result["entities"][0]["attributes"].append(  # type: ignore[index]
        {"name": "tag", "type": "string"}
    )
    result["entities"].append(  # type: ignore[index]
        entity(
            "task",
            [
                {"name": "project_id", "type": "uuid"},
                {"name": "title", "type": "string", "required": True},
            ],
        )
    )
    result["relationships"].append(  # type: ignore[index]
        {
            "name": "task_project_fk",
            "from": {"entity": "task", "field": "project_id"},
            "to": {"entity": "project", "field": "id"},
            "on_delete": "restrict",
        }
    )
    result["indices"].extend(  # type: ignore[index]
        [
            {"name": "project_tag_idx", "entity": "project", "fields": ["tag"]},
            {
                "name": "task_project_title_unique_idx",
                "entity": "task",
                "fields": ["project_id", "title"],
                "unique": True,
            },
        ]
    )
    return result


def _database(root: Path, locator: ServiceStoreLocator) -> Path:
    return root / "service-stores" / f"{locator.namespace}.sqlite"


def _assert_only_foundation_and_target_objects(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        rows = tuple(connection.execute("SELECT type,name FROM sqlite_schema"))

    assert set(rows) == {
        ("table", META),
        ("table", MIGRATION),
        ("table", "project"),
        ("table", "task"),
        ("index", "sqlite_autoindex___aipcs_service_store_migration_1"),
        ("index", "sqlite_autoindex___aipcs_service_store_migration_2"),
        ("index", "sqlite_autoindex_project_1"),
        ("index", "sqlite_autoindex_task_1"),
        ("index", "project_owner_idx"),
        ("index", "project_tag_idx"),
        ("index", "task_project_title_unique_idx"),
    }


def test_private_relational_schema_isolated_from_registry_and_public_restart(tmp_path: Path) -> None:
    root = tmp_path / "sqlite-root"
    _secure_parent(root)
    source_payload = valid_manifest()

    async def design_public_service() -> tuple[str, dict[str, object]]:
        async with async_session(sqlite_parameters(root, "private-boundary-principal")) as session:
            assert [tool.name for tool in (await session.list_tools()).tools] == _TOOL_NAMES
            seeded_envelope, failed = await call_envelope(session, "aipcs_service_seed", _seed())
            assert failed is False
            service_id = successful(seeded_envelope)["service_id"]
            designed_envelope, failed = await call_envelope(
                session,
                "aipcs_service_design",
                {
                    "service_id": service_id,
                    "schema": source_payload,
                    "idempotency_key": "private-boundary-design",
                },
            )
            assert failed is False
            return service_id, successful(designed_envelope)

    service_id, designed = anyio.run(design_public_service)
    registry = root / "registry.sqlite"
    registry_bytes = registry.read_bytes()

    source_manifest = ManifestV2.model_validate(source_payload)
    target_manifest = ManifestV2.model_validate(_v2(source_payload))
    locator = ServiceStoreLocator.for_service("sqlite", UUID(service_id))
    location = SQLiteLocationPolicy(root)
    assert SQLiteServiceStoreCatalog(location).migrate(locator).status == "ready"
    store = SQLiteDomainSchemaStore(location)
    assert store.materialise(locator, compile_manifest(source_manifest)) == DomainSchemaState("ready")
    assert store.evolve(locator, classify_transition(source_manifest, target_manifest)) == DomainSchemaState(
        "ready"
    )
    assert registry.read_bytes() == registry_bytes
    _assert_only_foundation_and_target_objects(_database(root, locator))

    async def inspect_after_restart() -> tuple[list[str], dict[str, object]]:
        async with async_session(sqlite_parameters(root, "private-boundary-principal")) as session:
            names = [tool.name for tool in (await session.list_tools()).tools]
            inspected_envelope, failed = await call_envelope(
                session, "aipcs_service_inspect", {"service_id": service_id}
            )
            assert failed is False
            return names, successful(inspected_envelope)

    names, observed = anyio.run(inspect_after_restart)
    assert names == _TOOL_NAMES
    assert observed == designed
    assert observed["design_state"] == "seeded"
    assert observed["schema_version"] == 1
    assert observed["materialised_at"] is None
    assert observed["storage"] is None


def _production_modules(root: Path) -> Iterable[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _private_relational_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    relative_module = path.relative_to(ROOT / "src").with_suffix("")
    package = ".".join(relative_module.parts[:-1])
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = importlib.util.resolve_name(f"{'.' * node.level}{module}", package)
            imports.add(module)
            if module.endswith("storage.sqlite"):
                imports.update(f"{module}.{alias.name}" for alias in node.names)
    return {
        imported
        for imported in imports
        if imported in _PRIVATE_RELATIONAL_MODULES
        or f"aipcs_mcp.{imported}" in _PRIVATE_RELATIONAL_MODULES
    }


def test_public_composition_roots_do_not_import_or_export_private_relational_sqlite_modules() -> None:
    import aipcs_mcp.storage as storage_package
    import aipcs_mcp.storage.sqlite as sqlite_package

    assert "SQLiteDomainSchemaStore" not in sqlite_package.__all__
    assert not hasattr(sqlite_package, "SQLiteDomainSchemaStore")
    assert storage_package.DomainSchemaStore is DomainSchemaStore
    assert "DomainSchemaStore" in storage_package.__all__

    public_modules = [
        PACKAGE_ROOT / "runtime.py",
        PACKAGE_ROOT / "mcp_server.py",
        PACKAGE_ROOT / "cli.py",
        PACKAGE_ROOT / "storage" / "sqlite" / "__init__.py",
        PACKAGE_ROOT / "storage" / "sqlite" / "adapter.py",
        PACKAGE_ROOT / "storage" / "sqlite" / "migrations.py",
        PACKAGE_ROOT / "storage" / "sqlite" / "uow.py",
        *_production_modules(PACKAGE_ROOT / "configuration"),
        *_production_modules(PACKAGE_ROOT / "application"),
    ]
    for module in public_modules:
        assert _private_relational_imports(module) == set(), module
