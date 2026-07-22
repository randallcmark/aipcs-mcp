"""SYNTHETIC_FIXTURE. Black-box V1-07B domain-schema drift and failure states."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from uuid import UUID

import pytest
from fixtures import entity, valid_manifest

from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.relational import compile_manifest
from aipcs_mcp.storage import DomainSchemaState, ServiceStoreLocator
from aipcs_mcp.storage.errors import StorageMigrationError
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteServiceStoreCatalog
from aipcs_mcp.storage.sqlite.domain_schema import SQLiteDomainSchemaStore


def _locator(value: int = 1) -> ServiceStoreLocator:
    return ServiceStoreLocator.for_service("sqlite", UUID(int=value))


def _database(root: Path, locator: ServiceStoreLocator) -> Path:
    return root / "service-stores" / f"{locator.namespace}.sqlite"


def _manifest(payload: dict[str, object]) -> ManifestV2:
    return ManifestV2.model_validate(payload)


def _specification(with_parent: bool = False):
    payload = valid_manifest()
    payload["entities"].append(entity("note", [{"name": "body", "type": "string"}]))
    if with_parent:
        payload["entities"][0]["attributes"].append({"name": "parent_id", "type": "uuid"})
        payload["relationships"] = [
            {
                "name": "project_parent_fk",
                "from": {"entity": "project", "field": "parent_id"},
                "to": {"entity": "project", "field": "id"},
                "on_delete": "restrict",
            }
        ]
    return compile_manifest(_manifest(payload))


def _ready_store(tmp_path: Path, *, parent: bool = False):
    root = tmp_path / "root"
    locator = _locator()
    catalog = SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root))
    assert catalog.migrate(locator).status == "ready"
    store = SQLiteDomainSchemaStore(SQLiteLocationPolicy(root))
    specification = _specification(parent)
    assert store.materialise(locator, specification) == DomainSchemaState("ready")
    return store, locator, root, _database(root, locator), specification


def test_absent_or_uninitialised_foundation_is_a_migration_precondition_failure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    locator = _locator()
    store = SQLiteDomainSchemaStore(SQLiteLocationPolicy(root))
    with pytest.raises(StorageMigrationError):
        store.inspect(locator, _specification())
    assert not root.exists()


@pytest.mark.parametrize(
    "mutation", ["extra_table", "extra_index", "missing_table", "column_shape"]
)
def test_ordinary_domain_drift_is_incompatible_and_materialise_does_not_repair(
    tmp_path: Path, mutation: str
) -> None:
    store, locator, _, database, specification = _ready_store(tmp_path)
    with sqlite3.connect(database) as connection:
        if mutation == "extra_table":
            connection.execute('CREATE TABLE "unrelated" ("id" TEXT PRIMARY KEY NOT NULL) STRICT')
        elif mutation == "extra_index":
            connection.execute('CREATE INDEX "project_owner_extra_idx" ON "project" ("owner_id")')
        elif mutation == "missing_table":
            connection.execute('DROP TABLE "project"')
        else:
            connection.execute('ALTER TABLE "project" ADD COLUMN "unexpected" TEXT')
    before = hashlib.sha256(database.read_bytes()).digest()

    assert store.inspect(locator, specification) == DomainSchemaState("incompatible")
    assert store.materialise(locator, specification) == DomainSchemaState("incompatible")
    assert hashlib.sha256(database.read_bytes()).digest() == before


@pytest.mark.parametrize(
    "statement",
    [
        'CREATE VIEW "project_view" AS SELECT "id" FROM "project"',
        'CREATE TRIGGER "project_trigger" AFTER INSERT ON "project" BEGIN SELECT 1; END',
    ],
)
def test_foundation_forbidden_objects_are_precondition_failures(
    tmp_path: Path, statement: str
) -> None:
    store, locator, _, database, specification = _ready_store(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(statement)

    with pytest.raises(StorageMigrationError):
        store.inspect(locator, specification)


def test_orphan_rows_are_incompatible_without_repair(tmp_path: Path) -> None:
    store, locator, _, database, specification = _ready_store(tmp_path, parent=True)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            'INSERT INTO "project" ("id","owner_id","created_at","updated_at","created_via",'
            '"record_version","title","parent_id") VALUES (?,?,?,?,?,?,?,?)',
            ("orphan", "owner", "t", "t", "test", 1, "Orphan", "missing"),
        )
    before = hashlib.sha256(database.read_bytes()).digest()

    assert store.inspect(locator, specification) == DomainSchemaState("incompatible")
    assert store.materialise(locator, specification) == DomainSchemaState("incompatible")
    assert hashlib.sha256(database.read_bytes()).digest() == before


def test_unexpected_sqlite_internal_statistics_are_incompatible(tmp_path: Path) -> None:
    store, locator, _, database, specification = _ready_store(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("ANALYZE")
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='sqlite_stat1'"
        ).fetchone()

    assert store.inspect(locator, specification) == DomainSchemaState("incompatible")
    assert store.materialise(locator, specification) == DomainSchemaState("incompatible")


def test_incompatible_partial_schema_is_preserved_without_initial_ddl(tmp_path: Path) -> None:
    root = tmp_path / "root"
    locator = _locator()
    catalog = SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root))
    assert catalog.migrate(locator).status == "ready"
    database = _database(root, locator)
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE "project" ("id" TEXT PRIMARY KEY NOT NULL) STRICT')
    before = hashlib.sha256(database.read_bytes()).digest()
    store = SQLiteDomainSchemaStore(SQLiteLocationPolicy(root))

    assert store.inspect(locator, _specification()) == DomainSchemaState("incompatible")
    assert store.materialise(locator, _specification()) == DomainSchemaState("incompatible")
    assert hashlib.sha256(database.read_bytes()).digest() == before


def test_complete_domain_absence_is_unmaterialised_without_a_schema_ledger(
    tmp_path: Path,
) -> None:
    store, locator, _, database, specification = _ready_store(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute('DROP TABLE "note"')
        connection.execute('DROP TABLE "project"')

    assert store.inspect(locator, specification) == DomainSchemaState("unmaterialised")
    assert store.materialise(locator, specification) == DomainSchemaState("ready")
