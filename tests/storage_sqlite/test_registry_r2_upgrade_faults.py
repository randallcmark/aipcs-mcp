"""SYNTHETIC_FIXTURE. Exact-R1 to R2 registry upgrade fault and retention proof."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from aipcs_mcp.storage import MigrationState
from aipcs_mcp.storage.errors import StorageMigrationError
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter
from aipcs_mcp.storage.sqlite import adapter as adapter_module
from aipcs_mcp.storage.sqlite.migrations import R1

_AT = "2026-01-01T00:00:00.000000Z"
_SERVICE_ID = "00000000-0000-0000-0000-000000000001"
_SECOND_SERVICE_ID = "00000000-0000-0000-0000-000000000002"


def _legacy_result(service_id: str, principal_id: str) -> str:
    return json.dumps(
        {
            "service_id": service_id,
            "principal_id": principal_id,
            "domain_name": "notes" if principal_id == "p" else "tasks",
            "domain_class": "project",
            "intent_description": "Notes",
            "created_at": _AT,
            "updated_at": _AT,
            "last_activity_at": _AT,
            "manifest": None,
            "schema_version": None,
            "design_state": "seeded",
            "operational_status": "active",
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _create_exact_r1(root: Path, audit_counts: dict[str, int] | None = None) -> Path:
    root.mkdir(mode=0o700)
    database = root / "registry.sqlite"
    descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    audit_counts = audit_counts or {"p": 0}
    services = (("p", _SERVICE_ID, "notes"), ("q", _SECOND_SERVICE_ID, "tasks"))
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for statement in R1.ddl:
            connection.execute(statement)
        connection.execute(
            'INSERT INTO "aipcs_registry_meta" VALUES (1,?,?,?,?)',
            ("aipcs.sqlite.registry", "registry", 1, 0),
        )
        connection.execute(
            'INSERT INTO "aipcs_registry_migration" VALUES (?,?,?,?,?)',
            ("registry", 1, R1.migration_id, R1.checksum, _AT),
        )
        for principal_id, service_id, domain_name in services:
            if principal_id not in audit_counts:
                continue
            connection.execute(
                'INSERT INTO "aipcs_registry_service" VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (
                    service_id,
                    principal_id,
                    domain_name,
                    "project",
                    "Notes",
                    _AT,
                    _AT,
                    _AT,
                    None,
                    None,
                    "seeded",
                    "active",
                ),
            )
        connection.execute(
            'INSERT INTO "aipcs_registry_mutation" VALUES (?,?,?,?,?)',
            ("p", "legacy", "a" * 64, _SERVICE_ID, _legacy_result(_SERVICE_ID, "p")),
        )
        audit_id = 1
        for principal_id, count in audit_counts.items():
            service_id = _SERVICE_ID if principal_id == "p" else _SECOND_SERVICE_ID
            for _ in range(count):
                connection.execute(
                    'INSERT INTO "aipcs_registry_audit"('
                    '"audit_id","action","outcome","service_id","principal_id","created_via","at") '
                    "VALUES (?,?,?,?,?,?,?)",
                    (audit_id, "seed", "created", service_id, principal_id, "test", _AT),
                )
                audit_id += 1
    return database


def _adapter(root: Path) -> SQLiteRegistryAdapter:
    return SQLiteRegistryAdapter(SQLiteLocationPolicy(root))


def _assert_exact_r1(adapter: SQLiteRegistryAdapter, database: Path) -> None:
    assert adapter.inspect_migration() == MigrationState("registry", 1, 2, "incompatible")
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT applied_revision,dirty FROM "aipcs_registry_meta"').fetchone() == (
            1,
            0,
        )
        assert connection.execute(
            'SELECT revision,migration_id,checksum FROM "aipcs_registry_migration"'
        ).fetchall() == [(1, R1.migration_id, R1.checksum)]
        assert connection.execute(
            "SELECT count(*) FROM sqlite_schema WHERE name LIKE '%_r1'"
        ).fetchone() == (0,)


def _statement(marker: str) -> Callable[[str], bool]:
    statements = {
        "dirty_set": 'UPDATE "aipcs_registry_meta" SET "dirty"=1',
        "rename_service": 'ALTER TABLE "aipcs_registry_service" RENAME',
        "rename_mutation": 'ALTER TABLE "aipcs_registry_mutation" RENAME',
        "rename_audit": 'ALTER TABLE "aipcs_registry_audit" RENAME',
        "drop_old_index": 'DROP INDEX "aipcs_registry_service_list"',
        "create_service": 'CREATE TABLE "aipcs_registry_service"',
        "create_mutation": 'CREATE TABLE "aipcs_registry_mutation"',
        "create_audit": 'CREATE TABLE "aipcs_registry_audit"',
        "copy_service": 'INSERT INTO "aipcs_registry_service"(',
        "copy_mutation": 'INSERT INTO "aipcs_registry_mutation"(',
        "copy_audit": 'INSERT INTO "aipcs_registry_audit"(',
        "drop_old_mutation": 'DROP TABLE "aipcs_registry_mutation_r1"',
        "drop_old_audit": 'DROP TABLE "aipcs_registry_audit_r1"',
        "drop_old_service": 'DROP TABLE "aipcs_registry_service_r1"',
        "create_service_index": 'CREATE INDEX "aipcs_registry_service_list"',
        "create_blocker_index": 'CREATE UNIQUE INDEX "aipcs_registry_mutation_active_lifecycle"',
        "create_audit_index": 'CREATE INDEX "aipcs_registry_audit_principal"',
        "audit_trim": 'DELETE FROM "aipcs_registry_audit" AS "old"',
        "history_insert": 'INSERT INTO "aipcs_registry_migration"',
        "meta_revision": 'UPDATE "aipcs_registry_meta" SET "applied_revision"=2',
        "validation": 'PRAGMA table_xinfo',
        "dirty_clear": 'UPDATE "aipcs_registry_meta" SET "dirty"=0',
    }
    expected = statements[marker]
    return lambda statement: statement.startswith(expected)


class _UpgradeFaultConnection:
    def __init__(self, wrapped: sqlite3.Connection, marker: str) -> None:
        self._wrapped = wrapped
        self._marker = marker
        self._upgrade_started = False
        self._meta_revision_written = False

    def __getattr__(self, name: str):
        return getattr(self._wrapped, name)

    def execute(self, statement: str, *args, **kwargs):
        if statement == "BEGIN EXCLUSIVE":
            self._upgrade_started = True
        elif (
            self._upgrade_started
            and self._marker not in {"commit_before", "commit_after"}
            and (self._marker != "validation" or self._meta_revision_written)
            and _statement(self._marker)(statement)
        ):
            raise sqlite3.OperationalError("synthetic upgrade fault")
        result = self._wrapped.execute(statement, *args, **kwargs)
        if statement.startswith('UPDATE "aipcs_registry_meta" SET "applied_revision"=2'):
            self._meta_revision_written = True
        return result

    def commit(self) -> None:
        if self._upgrade_started and self._marker == "commit_before":
            raise sqlite3.OperationalError("synthetic commit fault")
        self._wrapped.commit()
        if self._upgrade_started and self._marker == "commit_after":
            raise sqlite3.OperationalError("synthetic post-commit fault")


_PRECOMMIT_MARKERS = (
    "dirty_set",
    "rename_service",
    "rename_mutation",
    "rename_audit",
    "drop_old_index",
    "create_service",
    "create_mutation",
    "create_audit",
    "copy_service",
    "copy_mutation",
    "copy_audit",
    "drop_old_mutation",
    "drop_old_audit",
    "drop_old_service",
    "create_service_index",
    "create_blocker_index",
    "create_audit_index",
    "audit_trim",
    "history_insert",
    "meta_revision",
    "validation",
    "dirty_clear",
    "commit_before",
)


@pytest.mark.parametrize("marker", _PRECOMMIT_MARKERS)
def test_r1_upgrade_faults_roll_back_to_exact_r1_and_explicit_retry_upgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    root = tmp_path / marker
    database = _create_exact_r1(root, {"p": 1001})
    adapter = _adapter(root)
    real_connect = adapter_module.connect

    def faulting_connect(*args, **kwargs):
        return _UpgradeFaultConnection(real_connect(*args, **kwargs), marker)

    monkeypatch.setattr(adapter_module, "connect", faulting_connect)
    with pytest.raises(StorageMigrationError) as captured:
        adapter.migrate()
    assert captured.value.__cause__ is None and captured.value.__context__ is None
    monkeypatch.setattr(adapter_module, "connect", real_connect)
    _assert_exact_r1(adapter, database)
    assert adapter.migrate() == MigrationState("registry", 2, 2, "ready")


def test_post_commit_upgrade_fault_reopens_clean_r2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "post-commit"
    _create_exact_r1(root, {"p": 1001})
    adapter = _adapter(root)
    real_connect = adapter_module.connect

    def faulting_connect(*args, **kwargs):
        return _UpgradeFaultConnection(real_connect(*args, **kwargs), "commit_after")

    monkeypatch.setattr(adapter_module, "connect", faulting_connect)
    with pytest.raises(StorageMigrationError) as captured:
        adapter.migrate()
    assert captured.value.__cause__ is None and captured.value.__context__ is None
    monkeypatch.setattr(adapter_module, "connect", real_connect)
    assert adapter.inspect_migration() == MigrationState("registry", 2, 2, "ready")
    assert adapter.migrate() == MigrationState("registry", 2, 2, "ready")


@pytest.mark.parametrize("primary_count", (999, 1000, 1001))
def test_r1_upgrade_retains_newest_1000_audits_per_principal(
    tmp_path: Path, primary_count: int
) -> None:
    root = tmp_path / str(primary_count)
    database = _create_exact_r1(root, {"p": primary_count, "q": 1})
    assert _adapter(root).migrate() == MigrationState("registry", 2, 2, "ready")
    with sqlite3.connect(database) as connection:
        counts = connection.execute(
            'SELECT principal_id,count(*) FROM "aipcs_registry_audit" '
            'GROUP BY principal_id ORDER BY principal_id'
        ).fetchall()
        assert counts == [("p", min(primary_count, 1000)), ("q", 1)]
        if primary_count == 1001:
            assert connection.execute(
                'SELECT min(audit_id),max(audit_id) FROM "aipcs_registry_audit" WHERE principal_id="p"'
            ).fetchone() == (2, 1001)
