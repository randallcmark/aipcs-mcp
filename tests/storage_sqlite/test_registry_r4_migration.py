"""SYNTHETIC_FIXTURE. Exact R3-to-R4 registry migration and evidence invariants."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fixtures import valid_manifest

from aipcs_mcp.application.models import Service
from aipcs_mcp.application.operational_lifecycle import OperationalLifecyclePhase
from aipcs_mcp.application.registry_authority import (
    PurgeAuthority,
    PurgeAuthorityKind,
    PurgeCommand,
    PurgeTombstone,
    prepare_registry_intent,
)
from aipcs_mcp.lifecycle import MaterialiseCommand, lifecycle_fingerprint
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.storage import MigrationState
from aipcs_mcp.storage.errors import StorageMigrationError
from aipcs_mcp.storage.registry_authority_codecs import (
    encode_registry_authority_intent,
    encode_registry_authority_result,
)
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter
from aipcs_mcp.storage.sqlite import adapter as adapter_module
from aipcs_mcp.storage.sqlite.codecs import encode_manifest, encode_result, encode_service_values
from aipcs_mcp.storage.sqlite.migrations import R1, R2, R3, R4

_AT = "2026-07-23T00:00:00.000000Z"
_SERVICE_ID = "00000000-0000-0000-0000-000000000001"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _adapter(root: Path) -> SQLiteRegistryAdapter:
    return SQLiteRegistryAdapter(SQLiteLocationPolicy(root))


def _create_exact_r3(root: Path, *, audit_count: int = 1) -> Path:
    root.mkdir(mode=0o700)
    database = root / "registry.sqlite"
    descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    at = datetime(2026, 7, 23, tzinfo=UTC)
    service_id = UUID(_SERVICE_ID)
    manifest = ManifestV2.model_validate(valid_manifest())
    service = Service(
        service_id=service_id,
        principal_id="p",
        domain_name="notes",
        domain_class="project",
        intent_description="Preserve exact R3 evidence.",
        created_at=at,
        updated_at=at,
        last_activity_at=at,
        manifest=manifest,
        schema_version=1,
    )
    materialise = MaterialiseCommand("p", "test", service_id, 1, 1, "materialise-key")
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        for statement in R1.ddl[:2]:
            connection.execute(statement)
        for statement in R2.ddl:
            connection.execute(statement)
        connection.execute(R3.ddl[0])
        connection.execute(
            'INSERT INTO "aipcs_registry_meta" VALUES (1,?,?,?,?)',
            ("aipcs.sqlite.registry", "registry", 3, 0),
        )
        for descriptor_value in (R1, R2, R3):
            connection.execute(
                'INSERT INTO "aipcs_registry_migration" VALUES (?,?,?,?,?)',
                (
                    "registry",
                    descriptor_value.revision,
                    descriptor_value.migration_id,
                    descriptor_value.checksum,
                    _AT,
                ),
            )
        connection.execute(
            'INSERT INTO "aipcs_registry_storage_policy" VALUES (?,?,?,?)',
            (1, "aipcs.sqlite.wal.v1", R3.checksum, "ready"),
        )
        connection.execute(
            'INSERT INTO "aipcs_registry_service" VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            encode_service_values(service),
        )
        connection.execute(
            'INSERT INTO "aipcs_registry_mutation" VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                "p",
                "seed-key",
                "a" * 64,
                _SERVICE_ID,
                "seed",
                "completed",
                None,
                None,
                None,
                None,
                encode_result(service),
                None,
            ),
        )
        connection.execute(
            'INSERT INTO "aipcs_registry_mutation" VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                "p",
                "materialise-key",
                lifecycle_fingerprint(materialise),
                _SERVICE_ID,
                "materialise",
                "prepared",
                "test",
                1,
                1,
                encode_manifest(manifest),
                None,
                None,
            ),
        )
        for audit_id in range(1, audit_count + 1):
            connection.execute(
                'INSERT INTO "aipcs_registry_audit" VALUES (?,?,?,?,?,?,?)',
                (audit_id, "seed", "created", _SERVICE_ID, "p", "test", _AT),
            )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return database


def test_exact_r3_upgrades_atomically_to_r4_and_preserves_wal_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "registry"
    database = _create_exact_r3(root, audit_count=1000)
    adapter = _adapter(root)
    assert adapter.inspect_migration() == MigrationState("registry", 3, 4, "outdated")
    assert adapter.migrate() == MigrationState("registry", 4, 4, "ready")

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert tuple(
            connection.execute(
                'SELECT policy_id,policy_checksum,phase '
                'FROM "aipcs_registry_storage_policy"'
            ).fetchone()
        ) == ("aipcs.sqlite.wal.v1", R3.checksum, "ready")
        assert tuple(
            connection.execute(
                'SELECT revision,migration_id,checksum FROM "aipcs_registry_migration" '
                "ORDER BY revision"
            ).fetchall()[-1]
        ) == (4, R4.migration_id, R4.checksum)
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='aipcs_registry_mutation'"
        ).fetchone() is None
        claims = connection.execute(
            'SELECT operation_kind,phase,intent_json,result_json '
            'FROM "aipcs_registry_claim" ORDER BY idempotency_key'
        ).fetchall()
        assert [(row[0], row[1]) for row in claims] == [
            ("materialise", "prepared"),
            ("seed", "completed"),
        ]
        materialise_intent = json.loads(claims[0][2])
        assert materialise_intent["target_manifest"] == json.loads(
            encode_manifest(ManifestV2.model_validate(valid_manifest()))
        )
        assert materialise_intent["expected_service_revision"] == 1
        assert claims[1][2] is None and type(claims[1][3]) is str
        assert tuple(
            connection.execute(
                'SELECT principal_id,identity_state,domain_name,storage_backend,'
                'storage_namespace,claim_idempotency_key,lifecycle_at '
                'FROM "aipcs_registry_identity"'
            ).fetchone()
        ) == (
            "p",
            "live",
            "notes",
            None,
            f"svc_{UUID(_SERVICE_ID).hex}",
            None,
            None,
        )
        assert tuple(
            connection.execute(
                'SELECT count(*),min(audit_id),max(audit_id) FROM "aipcs_registry_audit"'
            ).fetchone()
        ) == (1000, 1, 1000)
        assert tuple(
            connection.execute('SELECT count(*) FROM "aipcs_registry_receipt"').fetchone()
        ) == (0,)


class _FaultConnection:
    def __init__(self, wrapped: sqlite3.Connection, marker: str) -> None:
        self._wrapped = wrapped
        self._marker = marker

    def __getattr__(self, name: str):
        return getattr(self._wrapped, name)

    def execute(self, statement: str, *args, **kwargs):
        prefixes = {
            "dirty": 'UPDATE "aipcs_registry_meta" SET "dirty"=1',
            "rename": 'ALTER TABLE "aipcs_registry_mutation" RENAME',
            "claim": 'CREATE TABLE "aipcs_registry_claim"',
            "copy": 'INSERT INTO "aipcs_registry_claim"(',
            "history": 'INSERT INTO "aipcs_registry_migration"',
        }
        if self._marker in prefixes and statement.startswith(prefixes[self._marker]):
            raise sqlite3.OperationalError("synthetic R4 migration fault")
        return self._wrapped.execute(statement, *args, **kwargs)

    def commit(self) -> None:
        if self._marker == "commit":
            raise sqlite3.OperationalError("synthetic R4 commit fault")
        self._wrapped.commit()


@pytest.mark.parametrize("marker", ("dirty", "rename", "claim", "copy", "history", "commit"))
def test_r4_precommit_fault_rolls_back_to_exact_r3_and_retry_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    root = tmp_path / marker
    _create_exact_r3(root)
    adapter = _adapter(root)
    real_connect = adapter_module.connect
    monkeypatch.setattr(
        adapter_module,
        "connect",
        lambda *args, **kwargs: _FaultConnection(real_connect(*args, **kwargs), marker),
    )
    with pytest.raises(StorageMigrationError):
        adapter.migrate()
    monkeypatch.setattr(adapter_module, "connect", real_connect)
    assert adapter.inspect_migration() == MigrationState("registry", 3, 4, "outdated")
    assert adapter.migrate() == MigrationState("registry", 4, 4, "ready")


def _prepared_intent(kind: str, service_id: str = _SERVICE_ID) -> str:
    return _canonical(
        {
            "created_via": "test",
            "expected_service_revision": 1,
            "kind": kind,
            "principal": "p",
            "service_id": service_id,
        }
    )


def test_r4_global_key_and_export_blocker_are_single_database_invariants(
    tmp_path: Path,
) -> None:
    root = tmp_path / "invariants"
    database = _create_exact_r3(root)
    assert _adapter(root).migrate().status == "ready"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            'UPDATE "aipcs_registry_claim" SET phase=\'completed\',result_json=\'{}\' '
            "WHERE idempotency_key='materialise-key'"
        )
        connection.execute(
            'INSERT INTO "aipcs_registry_claim" VALUES (?,?,?,?,?,?,?,?,?,?)',
            (
                "p",
                "shared",
                "b" * 64,
                _SERVICE_ID,
                "export",
                "prepared",
                "test",
                _prepared_intent("export"),
                None,
                None,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                'INSERT INTO "aipcs_registry_claim" VALUES (?,?,?,?,?,?,?,?,?,?)',
                (
                    "p",
                    "shared",
                    "c" * 64,
                    _SERVICE_ID,
                    "design",
                    "completed",
                    None,
                    None,
                    "{}",
                    None,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                'INSERT INTO "aipcs_registry_claim" VALUES (?,?,?,?,?,?,?,?,?,?)',
                (
                    "p",
                    "suspend",
                    "d" * 64,
                    _SERVICE_ID,
                    "suspend",
                    "prepared",
                    "test",
                    _prepared_intent("suspend"),
                    None,
                    None,
                ),
            )


def test_r4_tombstone_and_terminal_evidence_survive_live_service_deletion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "purge"
    database = _create_exact_r3(root)
    adapter = _adapter(root)
    assert adapter.migrate().status == "ready"
    purge_command = PurgeCommand(
        "p", "test", UUID(_SERVICE_ID), 1, "purge-key",
        PurgeAuthority(PurgeAuthorityKind.EXPLICIT_OVERRIDE),
    )
    purge_intent = prepare_registry_intent(purge_command)
    purge_tombstone = PurgeTombstone(
        "p", UUID(_SERVICE_ID), f"svc_{UUID(_SERVICE_ID).hex}", "test",
        datetime(2026, 7, 23, tzinfo=UTC), purge_command.authority,
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            'UPDATE "aipcs_registry_claim" SET phase=\'tombstoned\',created_via=NULL,'
            "intent_json=NULL,result_json=NULL,recovery_category=NULL"
        )
        connection.execute(
            'INSERT INTO "aipcs_registry_claim" VALUES (?,?,?,?,?,?,?,?,?,?)',
            (
                "p",
                "purge-key",
                    purge_intent.fingerprint,
                _SERVICE_ID,
                "purge",
                "completed",
                "test",
                    encode_registry_authority_intent(purge_intent.with_phase(OperationalLifecyclePhase.COMPLETED)),
                    encode_registry_authority_result(purge_tombstone),
                None,
            ),
        )
        connection.execute(
            'UPDATE "aipcs_registry_identity" SET identity_state=\'tombstoned\','
            'domain_name=NULL,storage_backend=NULL,claim_idempotency_key=\'purge-key\','
            "lifecycle_at=? WHERE service_id=?",
            (_AT, _SERVICE_ID),
        )
        connection.execute(
            'DELETE FROM "aipcs_registry_service" WHERE service_id=?', (_SERVICE_ID,)
        )
    assert adapter.inspect_migration() == MigrationState("registry", 4, 4, "ready")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            'SELECT identity_state,domain_name,storage_backend,storage_namespace,'
            'claim_idempotency_key,lifecycle_at FROM "aipcs_registry_identity"'
        ).fetchone() == (
            "tombstoned",
            None,
            None,
            f"svc_{UUID(_SERVICE_ID).hex}",
            "purge-key",
            _AT,
        )
        assert connection.execute(
            'SELECT operation_kind,phase,result_json FROM "aipcs_registry_claim" '
            "WHERE idempotency_key='purge-key'"
        ).fetchone()[0:2] == ("purge", "completed")
        assert connection.execute('SELECT count(*) FROM "aipcs_registry_audit"').fetchone() == (
            1,
        )


def test_r4_inspection_rejects_kind_invalid_legacy_completion_evidence(tmp_path: Path) -> None:
    root = tmp_path / "legacy-completion"
    database = _create_exact_r3(root)
    adapter = _adapter(root)
    assert adapter.migrate().status == "ready"
    with sqlite3.connect(database) as connection:
        result = json.loads(
            connection.execute(
                'SELECT "result_json" FROM "aipcs_registry_claim" WHERE "idempotency_key"=\'seed-key\''
            ).fetchone()[0]
        )
        result["service_revision"] = 2
        connection.execute(
            'INSERT INTO "aipcs_registry_claim" VALUES (?,?,?,?,?,?,?,?,?,?)',
            (
                "p",
                "design-key",
                "b" * 64,
                _SERVICE_ID,
                "design",
                "completed",
                None,
                None,
                _canonical(result),
                None,
            ),
        )
    assert adapter.inspect_migration().status == "ready"

    with sqlite3.connect(database) as connection:
        result = json.loads(
            connection.execute(
                'SELECT "result_json" FROM "aipcs_registry_claim" WHERE "idempotency_key"=\'design-key\''
            ).fetchone()[0]
        )
        result["service_revision"] = 3
        connection.execute(
            'UPDATE "aipcs_registry_claim" SET "result_json"=? WHERE "idempotency_key"=\'design-key\'',
            (_canonical(result),),
        )
    assert adapter.inspect_migration().status == "incompatible"
