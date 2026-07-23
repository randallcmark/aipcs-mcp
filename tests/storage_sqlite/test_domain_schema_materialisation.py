"""SYNTHETIC_FIXTURE. Black-box V1-07B SQLite domain-schema conformance."""

from __future__ import annotations

import hashlib
import sqlite3
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest
from fixtures import entity, valid_manifest

from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.relational import classify_transition, compile_manifest
from aipcs_mcp.storage import (
    DomainSchemaState,
    ServiceStoreLocator,
    StorageContractError,
    StorageMigrationError,
)
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteServiceStoreCatalog
from aipcs_mcp.storage.sqlite.domain_schema import SQLiteDomainSchemaStore
from aipcs_mcp.storage.sqlite.service_store_migrations import (
    META,
    MIGRATION,
    R2_POLICY,
    R3_BRANCH_TABLE,
    R3_HISTORY_TABLE,
    R3_MUTATION_TABLE,
    R3_RECORD_BRANCH_TABLE,
)


def _locator(value: int = 1) -> ServiceStoreLocator:
    return ServiceStoreLocator.for_service("sqlite", UUID(int=value))


def _database(root: Path, locator: ServiceStoreLocator) -> Path:
    return root / "service-stores" / f"{locator.namespace}.sqlite"


def _foundation(root: Path, locator: ServiceStoreLocator) -> None:
    catalog = SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root))
    assert catalog.migrate(locator).status == "ready"


def _store(root: Path) -> SQLiteDomainSchemaStore:
    return SQLiteDomainSchemaStore(SQLiteLocationPolicy(root))


def _manifest(payload: dict[str, object]) -> ManifestV2:
    return ManifestV2.model_validate(payload)


def _evolved(payload: dict[str, object]) -> dict[str, object]:
    result = deepcopy(payload)
    result["schema_version"] = 2
    result["migration_history"] = [
        {"from_schema_version": 1, "to_schema_version": 2, "operations": ["metadata only"]}
    ]
    return result


def _maximal_specification():
    payload = valid_manifest()
    payload["entities"] = [
        entity(
            "team",
            [
                {"name": "project_id", "type": "uuid"},
                {"name": "score", "type": "number"},
            ],
        ),
        entity(
            "project",
            [
                {"name": "title", "type": "string", "required": True},
                {"name": "quantity", "type": "integer", "required": True},
                {"name": "ratio", "type": "number"},
                {"name": "enabled", "type": "boolean"},
                {"name": "recorded_at", "type": "datetime"},
                {"name": "parent_id", "type": "uuid"},
                {"name": "team_id", "type": "uuid"},
                {"name": "labels", "type": "string_list"},
            ],
        ),
    ]
    payload["relationships"] = [
        {
            "name": "team_project_fk",
            "from": {"entity": "team", "field": "project_id"},
            "to": {"entity": "project", "field": "id"},
            "on_delete": "restrict",
        },
        {
            "name": "project_team_fk",
            "from": {"entity": "project", "field": "team_id"},
            "to": {"entity": "team", "field": "id"},
            "on_delete": "restrict",
        },
        {
            "name": "project_parent_fk",
            "from": {"entity": "project", "field": "parent_id"},
            "to": {"entity": "project", "field": "id"},
            "on_delete": "restrict",
        },
    ]
    payload["indices"] = [
        {
            "name": "project_title_quantity_idx",
            "entity": "project",
            "fields": ["title", "quantity"],
        },
        {
            "name": "project_team_owner_unique_idx",
            "entity": "project",
            "fields": ["team_id", "owner_id"],
            "unique": True,
        },
        {"name": "team_project_owner_idx", "entity": "team", "fields": ["project_id", "owner_id"]},
    ]
    return compile_manifest(_manifest(payload))


def _expected_columns() -> dict[str, list[tuple[str, str, int, int]]]:
    return {
        "project": [
            ("id", "TEXT", 1, 1),
            ("owner_id", "TEXT", 1, 0),
            ("created_at", "TEXT", 1, 0),
            ("updated_at", "TEXT", 1, 0),
            ("created_via", "TEXT", 1, 0),
            ("record_version", "INTEGER", 1, 0),
            ("title", "TEXT", 1, 0),
            ("quantity", "INTEGER", 1, 0),
            ("ratio", "REAL", 0, 0),
            ("enabled", "INTEGER", 0, 0),
            ("recorded_at", "TEXT", 0, 0),
            ("parent_id", "TEXT", 0, 0),
            ("team_id", "TEXT", 0, 0),
            ("labels", "TEXT", 0, 0),
        ],
        "team": [
            ("id", "TEXT", 1, 1),
            ("owner_id", "TEXT", 1, 0),
            ("created_at", "TEXT", 1, 0),
            ("updated_at", "TEXT", 1, 0),
            ("created_via", "TEXT", 1, 0),
            ("record_version", "INTEGER", 1, 0),
            ("project_id", "TEXT", 0, 0),
            ("score", "REAL", 0, 0),
        ],
    }


def test_materialise_exactly_maps_all_types_relationships_and_indices(tmp_path: Path) -> None:
    root = tmp_path / "root"
    locator = _locator()
    specification = _maximal_specification()
    _foundation(root, locator)

    assert _store(root).inspect(locator, specification) == DomainSchemaState("unmaterialised")
    assert _store(root).materialise(locator, specification) == DomainSchemaState("ready")

    with sqlite3.connect(_database(root, locator)) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND substr(name, 1, 7) <> 'sqlite_'"
            )
        }
        assert names == {
            META,
            MIGRATION,
            R2_POLICY.table_name,
            R3_MUTATION_TABLE,
            R3_HISTORY_TABLE,
            R3_BRANCH_TABLE,
            R3_RECORD_BRANCH_TABLE,
            "project",
            "team",
        }
        for table, expected in _expected_columns().items():
            actual = [
                (row[1], row[2], row[3], row[5])
                for row in connection.execute(f'PRAGMA table_xinfo("{table}")')
            ]
            assert actual == expected
        project_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='project'"
        ).fetchone()[0]
        assert 'CONSTRAINT "project_parent_fk"' in project_sql
        assert 'CONSTRAINT "project_team_fk"' in project_sql
        assert "ON DELETE RESTRICT ON UPDATE RESTRICT" in project_sql
        assert "DEFERRABLE" not in project_sql
        assert project_sql.rstrip().endswith("STRICT")
        assert "DEFAULT" not in project_sql and "CHECK" not in project_sql
        project_indices = {
            row[1]: (row[2], row[3], row[4])
            for row in connection.execute('PRAGMA index_list("project")')
        }
        assert project_indices["project_title_quantity_idx"] == (0, "c", 0)
        assert project_indices["project_team_owner_unique_idx"] == (1, "c", 0)
        for index, columns in {
            "project_title_quantity_idx": ("title", "quantity"),
            "project_team_owner_unique_idx": ("team_id", "owner_id"),
            "team_project_owner_idx": ("project_id", "owner_id"),
        }.items():
            actual = tuple(
                row[2] for row in connection.execute(f'PRAGMA index_xinfo("{index}")') if row[5]
            )
            assert actual == columns


def test_materialised_foreign_keys_are_immediate_and_restrictive(tmp_path: Path) -> None:
    root = tmp_path / "root"
    locator = _locator()
    specification = _maximal_specification()
    _foundation(root, locator)
    assert _store(root).materialise(locator, specification).status == "ready"

    with sqlite3.connect(_database(root, locator), isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                'INSERT INTO "project" ("id","owner_id","created_at","updated_at","created_via",'
                '"record_version","title","quantity","team_id") VALUES (?,?,?,?,?,?,?,?,?)',
                ("project-1", "owner", "t", "t", "test", 1, "Project", 1, "missing-team"),
            )
        connection.execute(
            'INSERT INTO "team" ("id","owner_id","created_at","updated_at","created_via",'
            '"record_version") VALUES (?,?,?,?,?,?)',
            ("team-1", "owner", "t", "t", "test", 1),
        )
        connection.execute(
            'INSERT INTO "project" ("id","owner_id","created_at","updated_at","created_via",'
            '"record_version","title","quantity","team_id") VALUES (?,?,?,?,?,?,?,?,?)',
            ("project-1", "owner", "t", "t", "test", 1, "Project", 1, "team-1"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute('DELETE FROM "team" WHERE "id"=?', ("team-1",))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute('UPDATE "team" SET "id"=? WHERE "id"=?', ("team-2", "team-1"))


def test_inspect_repeat_and_restart_are_exact_noops(tmp_path: Path) -> None:
    root = tmp_path / "root"
    locator = _locator()
    specification = _maximal_specification()
    _foundation(root, locator)
    store = _store(root)
    assert store.materialise(locator, specification) == DomainSchemaState("ready")
    database = _database(root, locator)
    before = hashlib.sha256(database.read_bytes()).digest()
    with sqlite3.connect(database) as connection:
        foundation_before = connection.execute(
            f'SELECT applied_revision,dirty FROM "{META}"'
        ).fetchone()

    assert store.inspect(locator, specification) == DomainSchemaState("ready")
    assert store.materialise(locator, specification) == DomainSchemaState("ready")
    assert hashlib.sha256(database.read_bytes()).digest() == before
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(f'SELECT applied_revision,dirty FROM "{META}"').fetchone()
            == foundation_before
        )
    assert _store(root).inspect(locator, specification) == DomainSchemaState("ready")


def test_inspect_ignores_schema_version_but_materialise_rejects_it_before_location_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    locator = _locator()
    payload = valid_manifest()
    initial = compile_manifest(_manifest(payload))
    later = compile_manifest(_manifest(_evolved(payload)))
    _foundation(root, locator)
    store = _store(root)
    assert store.materialise(locator, initial).status == "ready"
    assert store.inspect(locator, later) == DomainSchemaState("ready")
    monkeypatch.setattr(
        store._location,
        "acquire_service_store",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("location touched")),
    )
    with pytest.raises(StorageContractError):
        store.materialise(locator, later)


def test_evolve_is_validated_then_unavailable_without_location_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    locator = _locator()
    current_payload = valid_manifest()
    target_payload = _evolved(current_payload)
    target_payload["indices"].append(
        {"name": "project_title_idx", "entity": "project", "fields": ["title"]}
    )
    transition = classify_transition(_manifest(current_payload), _manifest(target_payload))
    store = _store(root)
    monkeypatch.setattr(
        store._location,
        "acquire_service_store",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("location touched")),
    )
    with pytest.raises(StorageContractError):
        store.evolve(object(), transition)  # type: ignore[arg-type]
    with pytest.raises(StorageContractError):
        store.evolve(locator, object())  # type: ignore[arg-type]
    with pytest.raises(StorageMigrationError):
        store.evolve(locator, transition)
