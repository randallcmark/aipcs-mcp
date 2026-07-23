from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from application.fakes import FixedClock, SequentialIds
from fixtures import valid_manifest
from storage_contracts.conformance import assert_registry_application_conformance

from aipcs_mcp.application.errors import InternalFailure
from aipcs_mcp.application.models import (
    ApplicationContext,
    DesignCommand,
    MaterialisationStorage,
    MaterialiseCompletion,
    PreparedLifecycleClaim,
    SeedCommand,
    Service,
)
from aipcs_mcp.application.services import ServiceApplication
from aipcs_mcp.lifecycle import LifecyclePhase, MaterialiseCommand
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.storage import MigrationState
from aipcs_mcp.storage.errors import StorageMigrationError, StorageUnavailable
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter
from aipcs_mcp.storage.sqlite.codecs import encode_result
from aipcs_mcp.storage.sqlite.migrations import R1, R2, R3


class TraceUow:
    def __init__(self, wrapped, harness: Harness) -> None:
        self.wrapped, self.harness = wrapped, harness
        self.calls: list[str] = []
        self.close_count = 0
        self.services = self
        self.mutations = self
        self.audits = self

    def _call(self, name, *args):
        self.calls.append(name)
        if self.harness.failure == name:
            raise RuntimeError("sqlite-secret")
        return getattr(self.wrapped, name)(*args)

    def find_domain(self, *args):
        return self._call("find_domain", *args)

    def get(self, *args):
        return self._call("get", *args)

    def list(self, *args):
        return self._call("list", *args)

    def add(self, *args):
        return self._call("add", *args)

    def save(self, *args):
        return self._call("save", *args)

    def resolve_non_lifecycle(self, *args):
        return self._call("resolve_non_lifecycle", *args)

    def complete_non_lifecycle(self, *args):
        return self._call("complete_non_lifecycle", *args)

    def recovery_state(self, *args):
        return self._call("recovery_state", *args)

    def append(self, *args):
        return self._call("append", *args)

    def commit(self):
        return self._call("commit")

    def rollback(self):
        return self._call("rollback")

    def close(self):
        self.close_count += 1
        self.calls.append("close")
        self.wrapped.close()
        if self.harness.failure == "close":
            raise RuntimeError("sqlite-secret")


class Harness:
    def __init__(self, root: Path) -> None:
        self.adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root / "registry-root"))
        assert self.adapter.migrate().status == "ready"
        self.failure: str | None = None
        self.items: list[TraceUow] = []

    def application(self, *ids: UUID) -> ServiceApplication:
        def factory():
            uow = TraceUow(self.adapter.open_uow(), self)
            self.items.append(uow)
            return uow

        return ServiceApplication(
            factory, FixedClock(datetime(2026, 1, 1, tzinfo=UTC)), SequentialIds(*ids)
        )

    restart = application

    def traces(self):
        return self.items

    def fail(self, boundary):
        self.failure = boundary


def _directory_snapshot(directory: Path) -> tuple[tuple[str, int, int, int], ...]:
    return tuple(
        sorted(
            (
                entry.name,
                entry.stat().st_mode,
                entry.stat().st_size,
                entry.stat().st_mtime_ns,
            )
            for entry in directory.iterdir()
        )
    )


def _leave_wal_header_without_sidecars(database: Path, *, malformed: bool) -> None:
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    for suffix in ("-wal", "-shm"):
        Path(str(database) + suffix).unlink(missing_ok=True)
    if malformed:
        descriptor = os.open(database, os.O_WRONLY)
        try:
            assert os.pwrite(descriptor, b"\x00", 21) == 1
        finally:
            os.close(descriptor)
    database.chmod(0o600)


def _create_exact_r1(
    root: Path, *, audit_count: int = 0, operational_status: str = "active"
) -> tuple[SQLiteRegistryAdapter, Path, str]:
    root.mkdir(mode=0o700)
    database = root / "registry.sqlite"
    descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    service_id = "00000000-0000-0000-0000-000000000001"
    at = "2026-01-01T00:00:00.000000Z"
    result = json.dumps(
        {
            "service_id": service_id,
            "principal_id": "p",
            "domain_name": "notes",
            "domain_class": "project",
            "intent_description": "Notes",
            "created_at": at,
            "updated_at": at,
            "last_activity_at": at,
            "manifest": None,
            "schema_version": None,
            "design_state": "seeded",
            "operational_status": operational_status,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
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
            ("registry", 1, R1.migration_id, R1.checksum, at),
        )
        connection.execute(
            'INSERT INTO "aipcs_registry_service" VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                service_id,
                "p",
                "notes",
                "project",
                "Notes",
                at,
                at,
                at,
                None,
                None,
                "seeded",
                operational_status,
            ),
        )
        connection.execute(
            'INSERT INTO "aipcs_registry_mutation" VALUES (?,?,?,?,?)',
            ("p", "legacy-key", "a" * 64, service_id, result),
        )
        for audit_id in range(1, audit_count + 1):
            connection.execute(
                'INSERT INTO "aipcs_registry_audit"('
                '"audit_id","action","outcome","service_id","principal_id","created_via","at") '
                "VALUES (?,?,?,?,?,?,?)",
                (audit_id, "seed", "created", service_id, "p", "test", at),
            )
    return SQLiteRegistryAdapter(SQLiteLocationPolicy(root)), database, result


def _create_ready_lifecycle_r2(
    root: Path, phase: LifecyclePhase
) -> tuple[SQLiteRegistryAdapter, Path]:
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    assert adapter.migrate().status == "ready"
    at = datetime(2026, 1, 1, tzinfo=UTC)
    service_id = UUID(int=1)
    manifest = ManifestV2.model_validate(valid_manifest())
    service = Service(
        service_id=service_id,
        principal_id="p",
        domain_name="notes",
        domain_class="project",
        intent_description="Notes",
        created_at=at,
        updated_at=at,
        last_activity_at=at,
        manifest=manifest,
        schema_version=1,
    )
    uow = adapter.open_uow()
    uow.services.add(service)
    uow.commit()
    uow.close()
    command = MaterialiseCommand("p", "test", service_id, 1, 1, "lifecycle-key")
    uow = adapter.open_uow()
    prepared = uow.mutations.resolve_or_admit(command)
    assert isinstance(prepared, PreparedLifecycleClaim)
    uow.commit()
    uow.close()
    if phase is LifecyclePhase.RECOVERY_REQUIRED:
        uow = adapter.open_uow()
        uow.mutations.finalize_recovery_required(prepared.intent, at)
        uow.commit()
        uow.close()
    elif phase is LifecyclePhase.COMPLETED:
        uow = adapter.open_uow()
        uow.mutations.finalize_completed(
            MaterialiseCompletion(
                prepared.intent,
                at,
                MaterialisationStorage("sqlite", f"svc_{service_id.hex}"),
            )
        )
        uow.commit()
        uow.close()
    return adapter, root / "registry.sqlite"


def _assert_r2_incompatible_without_mutation(
    adapter: SQLiteRegistryAdapter, database: Path
) -> None:
    before = database.read_bytes()
    assert adapter.inspect_migration() == MigrationState("registry", 3, 3, "incompatible")
    assert database.read_bytes() == before


def test_exact_public_r1_upgrades_atomically_with_legacy_bytes_and_audit_trim(tmp_path: Path) -> None:
    adapter, database, legacy_result = _create_exact_r1(tmp_path / "r1", audit_count=1001)
    assert adapter.inspect_migration() == MigrationState("registry", 1, 3, "incompatible")
    assert adapter.migrate() == MigrationState("registry", 3, 3, "ready")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            'SELECT revision,migration_id,checksum FROM "aipcs_registry_migration" ORDER BY revision'
        ).fetchall() == [
            (1, R1.migration_id, R1.checksum),
            (2, R2.migration_id, R2.checksum),
            (3, R3.migration_id, R3.checksum),
        ]
        assert connection.execute(
            'SELECT service_revision,materialised_at,storage_backend,storage_namespace '
            'FROM "aipcs_registry_service"'
        ).fetchone() == (1, None, None, None)
        assert connection.execute(
            'SELECT operation_kind,phase,result_json FROM "aipcs_registry_mutation"'
        ).fetchone() == ("legacy", "completed", legacy_result)
        assert connection.execute('SELECT count(*),min(audit_id),max(audit_id) FROM "aipcs_registry_audit"').fetchone() == (
            1000,
            2,
            1001,
        )


def test_private_or_unreachable_r1_is_not_upgradeable(tmp_path: Path) -> None:
    adapter, database, _ = _create_exact_r1(
        tmp_path / "r1", operational_status="suspended"
    )
    assert adapter.inspect_migration() == MigrationState("registry", 1, 3, "incompatible")
    assert adapter.migrate() == MigrationState("registry", 1, 3, "incompatible")
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT applied_revision,dirty FROM "aipcs_registry_meta"').fetchone() == (
            1,
            0,
        )
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name='aipcs_registry_service_r1'"
        ).fetchone() is None


@pytest.mark.parametrize("row_kind", ["service", "legacy_result"])
@pytest.mark.parametrize(
    ("created_at", "updated_at", "last_activity_at"),
    [
        (
            "2026-01-01T00:00:00.000000Z",
            "2025-12-31T23:59:59.000000Z",
            "2025-12-31T23:59:59.000000Z",
        ),
        (
            "2026-01-01T00:00:00.000000Z",
            "2026-01-01T00:00:00.000000Z",
            "2026-01-01T00:00:01.000000Z",
        ),
        (
            "2026-01-01T00:00:00.000000Z",
            "2026-01-01T00:00:01.000000Z",
            "2026-01-01T00:00:01.000000Z",
        ),
    ],
)
def test_publicly_impossible_r1_timestamp_state_is_not_upgradeable_or_mutated(
    tmp_path: Path,
    row_kind: str,
    created_at: str,
    updated_at: str,
    last_activity_at: str,
) -> None:
    adapter, database, _ = _create_exact_r1(tmp_path / "r1")
    with sqlite3.connect(database) as connection:
        if row_kind == "service":
            connection.execute(
                'UPDATE "aipcs_registry_service" SET '
                '"created_at"=?,"updated_at"=?,"last_activity_at"=?',
                (created_at, updated_at, last_activity_at),
            )
        else:
            result = json.loads(
                connection.execute(
                    'SELECT "result_json" FROM "aipcs_registry_mutation"'
                ).fetchone()[0]
            )
            result.update(
                created_at=created_at,
                updated_at=updated_at,
                last_activity_at=last_activity_at,
            )
            connection.execute(
                'UPDATE "aipcs_registry_mutation" SET "result_json"=?',
                (json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True),),
            )

    before = database.read_bytes()
    assert adapter.inspect_migration() == MigrationState("registry", 1, 3, "incompatible")
    assert adapter.migrate() == MigrationState("registry", 1, 3, "incompatible")
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "corruption",
    ["service", "non_lifecycle_result", "audit_timestamp", "audit_provenance", "audit_id"],
)
def test_ready_r2_semantic_row_corruption_is_incompatible_without_mutation(
    tmp_path: Path, corruption: str
) -> None:
    harness = Harness(tmp_path)
    harness.application(UUID(int=1)).seed(
        ApplicationContext("p", "test"), SeedCommand("notes", "project", "Notes", "seed-key")
    )
    database = tmp_path / "registry-root" / "registry.sqlite"
    with sqlite3.connect(database) as connection:
        if corruption == "service":
            connection.execute(
                'UPDATE "aipcs_registry_service" SET "domain_name"=?', ("UPPER",)
            )
        elif corruption == "non_lifecycle_result":
            connection.execute(
                'UPDATE "aipcs_registry_mutation" SET "result_json"=?',
                ('{"unexpected":true}',),
            )
        elif corruption == "audit_timestamp":
            connection.execute(
                'UPDATE "aipcs_registry_audit" SET "at"=?',
                ("2026-99-99T99:99:99.999999Z",),
            )
        elif corruption == "audit_provenance":
            connection.execute(
                'UPDATE "aipcs_registry_audit" SET "created_via"=?', (" test",)
            )
        else:
            connection.execute('UPDATE "aipcs_registry_audit" SET "audit_id"=0')
    _assert_r2_incompatible_without_mutation(harness.adapter, database)


def test_ready_r2_rejects_audit_rows_above_the_per_principal_retention_cap(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    created = harness.application(UUID(int=1)).seed(
        ApplicationContext("p", "test"), SeedCommand("notes", "project", "Notes", "seed-key")
    )
    database = tmp_path / "registry-root" / "registry.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executemany(
            'INSERT INTO "aipcs_registry_audit"('
            '"action","outcome","service_id","principal_id","created_via","at") '
            "VALUES (?,?,?,?,?,?)",
            [
                (
                    "seed",
                    "created",
                    str(created.service_id),
                    "p",
                    "test",
                    "2026-01-01T00:00:00.000000Z",
                )
                for _ in range(1_000)
            ],
        )
    _assert_r2_incompatible_without_mutation(harness.adapter, database)


def test_ready_r2_design_result_is_kind_exact_while_seed_duplicate_may_replay_design(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    context = ApplicationContext("p", "test")
    application = harness.application(UUID(int=1))
    created = application.seed(context, SeedCommand("notes", "project", "Notes", "seed-key"))
    manifest = ManifestV2.model_validate(valid_manifest())
    designed = application.design(
        context,
        DesignCommand(created.service_id, manifest, "design-key"),
    )
    duplicate = application.seed(
        context,
        SeedCommand("notes", "project", "Notes", "duplicate-seed-key"),
    )
    assert duplicate == designed
    assert harness.adapter.inspect_migration().status == "ready"

    database = tmp_path / "registry-root" / "registry.sqlite"
    uow = harness.adapter.open_uow()
    current = uow.services.get("p", created.service_id)
    assert current is not None
    uow.close()
    forged = replace(current, service_revision=1)
    with sqlite3.connect(database) as connection:
        connection.execute(
            'UPDATE "aipcs_registry_mutation" SET "result_json"=? '
            'WHERE "operation_kind"=\'design\'',
            (encode_result(forged),),
        )
    _assert_r2_incompatible_without_mutation(harness.adapter, database)


def test_ready_r2_design_completion_cannot_be_ahead_of_current_service(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    created = harness.application(UUID(int=1)).seed(
        ApplicationContext("p", "test"), SeedCommand("notes", "project", "Notes", "seed-key")
    )
    manifest = ManifestV2.model_validate(valid_manifest())
    uow = harness.adapter.open_uow()
    current = uow.services.get("p", created.service_id)
    assert current is not None
    uow.close()
    forged = replace(
        current,
        manifest=manifest,
        schema_version=1,
        service_revision=2,
    )
    database = tmp_path / "registry-root" / "registry.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            'INSERT INTO "aipcs_registry_mutation"('
            '"principal_id","idempotency_key","fingerprint","service_id","operation_kind",'
            '"phase","created_via","expected_service_revision","expected_schema_version",'
            '"target_manifest_json","result_json","recovery_category") '
            "VALUES (?,?,?,?,?,'completed',NULL,NULL,NULL,NULL,?,NULL)",
            (
                "p",
                "forged-design",
                "f" * 64,
                str(created.service_id),
                "design",
                encode_result(forged),
            ),
        )
    _assert_r2_incompatible_without_mutation(harness.adapter, database)


@pytest.mark.parametrize(
    ("phase", "column", "value"),
    [
        (LifecyclePhase.PREPARED, "result_json", "{}"),
        (LifecyclePhase.RECOVERY_REQUIRED, "recovery_category", None),
        (LifecyclePhase.COMPLETED, "recovery_category", "recovery_required"),
    ],
)
def test_ready_r2_lifecycle_phase_evidence_corruption_is_incompatible_without_mutation(
    tmp_path: Path, phase: LifecyclePhase, column: str, value: object
) -> None:
    adapter, database = _create_ready_lifecycle_r2(tmp_path / "registry", phase)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(f'UPDATE "aipcs_registry_mutation" SET "{column}"=?', (value,))
    _assert_r2_incompatible_without_mutation(adapter, database)


def test_ready_r2_completed_result_may_be_canonical_but_must_match_its_intent(
    tmp_path: Path,
) -> None:
    adapter, database = _create_ready_lifecycle_r2(
        tmp_path / "registry", LifecyclePhase.COMPLETED
    )
    uow = adapter.open_uow()
    current = uow.services.get("p", UUID(int=1))
    assert current is not None
    uow.close()
    wrong_terminal = replace(current, service_revision=current.service_revision + 1)
    with sqlite3.connect(database) as connection:
        connection.execute(
            'UPDATE "aipcs_registry_mutation" SET "result_json"=?',
            (encode_result(wrong_terminal),),
        )
    _assert_r2_incompatible_without_mutation(adapter, database)


@pytest.mark.parametrize("corruption", ["materialised_before_completion", "activity_after_completion"])
def test_ready_r2_completed_materialise_result_requires_exact_terminal_timestamps(
    tmp_path: Path, corruption: str
) -> None:
    adapter, database = _create_ready_lifecycle_r2(
        tmp_path / "registry", LifecyclePhase.COMPLETED
    )
    uow = adapter.open_uow()
    current = uow.services.get("p", UUID(int=1))
    assert current is not None
    uow.close()
    later = datetime(2026, 1, 2, tzinfo=UTC)
    if corruption == "materialised_before_completion":
        wrong_terminal = replace(current, updated_at=later, last_activity_at=later)
    else:
        wrong_terminal = replace(current, last_activity_at=later)
    with sqlite3.connect(database) as connection:
        connection.execute(
            'UPDATE "aipcs_registry_mutation" SET "result_json"=?',
            (encode_result(wrong_terminal),),
        )
    _assert_r2_incompatible_without_mutation(adapter, database)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("domain_name", "forged_domain"),
        ("domain_class", "forged"),
        ("intent_description", "forged"),
        ("created_at", datetime(2025, 1, 1, tzinfo=UTC)),
    ],
)
def test_ready_r2_completion_must_match_current_immutable_service_identity(
    tmp_path: Path, field: str, value: object
) -> None:
    adapter, database = _create_ready_lifecycle_r2(
        tmp_path / "registry", LifecyclePhase.COMPLETED
    )
    uow = adapter.open_uow()
    current = uow.services.get("p", UUID(int=1))
    assert current is not None
    uow.close()
    forged = replace(current, **{field: value})
    with sqlite3.connect(database) as connection:
        connection.execute(
            'UPDATE "aipcs_registry_mutation" SET "result_json"=?',
            (encode_result(forged),),
        )
    _assert_r2_incompatible_without_mutation(adapter, database)


@pytest.mark.parametrize(
    "corruption", ["mutation_idempotency", "audit_timestamp", "audit_provenance", "audit_id"]
)
def test_r1_mutation_and_audit_validation_rejects_non_public_rows_without_mutation(
    tmp_path: Path, corruption: str
) -> None:
    adapter, database, _ = _create_exact_r1(tmp_path / "r1", audit_count=1)
    with sqlite3.connect(database) as connection:
        if corruption == "mutation_idempotency":
            connection.execute(
                'UPDATE "aipcs_registry_mutation" SET "idempotency_key"=?', (" key",)
            )
        elif corruption == "audit_timestamp":
            connection.execute(
                'UPDATE "aipcs_registry_audit" SET "at"=?',
                ("2026-99-99T99:99:99.999999Z",),
            )
        elif corruption == "audit_provenance":
            connection.execute(
                'UPDATE "aipcs_registry_audit" SET "created_via"=?', (" test",)
            )
        else:
            connection.execute('UPDATE "aipcs_registry_audit" SET "audit_id"=0')
    before = database.read_bytes()
    assert adapter.migrate() == MigrationState("registry", 1, 3, "incompatible")
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    ("action", "outcome"),
    [("materialise", "completed"), ("evolve", "recovery_required")],
)
def test_r1_lifecycle_audit_vocabulary_is_bounded_and_not_migrated(
    tmp_path: Path, action: str, outcome: str
) -> None:
    adapter, database, _ = _create_exact_r1(tmp_path / "r1", audit_count=1)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            'UPDATE "aipcs_registry_audit" SET "action"=?,"outcome"=?', (action, outcome)
        )
    before = database.read_bytes()
    assert adapter.inspect_migration() == MigrationState("registry", 1, 3, "incompatible")
    with pytest.raises(StorageMigrationError):
        adapter.migrate()
    assert database.read_bytes() == before


def test_sqlite_adapter_satisfies_unchanged_registry_conformance(tmp_path: Path) -> None:
    counter = 0

    def factory() -> Harness:
        nonlocal counter
        counter += 1
        base = tmp_path / str(counter)
        base.mkdir()
        return Harness(base)

    assert_registry_application_conformance(factory)


def test_inspection_is_read_only_and_migration_is_explicit(tmp_path: Path) -> None:
    root = tmp_path / "root"
    policy = SQLiteLocationPolicy(root)
    adapter = SQLiteRegistryAdapter(policy)
    assert adapter.inspect_migration() == MigrationState("registry", 0, 3, "uninitialised")
    assert not root.exists()
    assert adapter.migrate().status == "ready"
    database = root / "registry.sqlite"
    before = database.stat().st_mtime_ns
    assert adapter.inspect_migration().status == "ready"
    assert database.stat().st_mtime_ns == before
    # A valid read-only WAL open may create SQLite-owned operational sidecars.


def test_location_rejects_symlinked_database(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / "registry.sqlite").symlink_to(tmp_path / "outside")
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    with pytest.raises(Exception) as captured:
        adapter.inspect_migration()
    assert "outside" not in str(captured.value)


def test_zero_byte_store_is_uninitialised_and_migratable(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    database = root / "registry.sqlite"
    descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    assert adapter.inspect_migration().status == "uninitialised"
    assert adapter.migrate().status == "ready"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_hostile_sidecars_fail_closed(tmp_path: Path, suffix: str) -> None:
    root = tmp_path / "root"
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    assert adapter.migrate().status == "ready"
    sidecar = root / f"registry.sqlite{suffix}"
    sidecar.unlink(missing_ok=True)
    descriptor = os.open(sidecar, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(descriptor, b"synthetic-sidecar")
    os.close(descriptor)
    if suffix == "-journal":
        with pytest.raises(StorageUnavailable):
            adapter.inspect_migration()
    else:
        # SQLite may safely recover or replace malformed WAL/SHM operational
        # files; the adapter must never repair them itself.
        assert adapter.inspect_migration().status == "ready"
    if suffix == "-journal":
        with pytest.raises(StorageUnavailable):
            adapter.migrate()
        assert sidecar.exists()


@pytest.mark.parametrize("malformed", [False, True], ids=["valid", "malformed"])
def test_wal_header_without_sidecars_never_changes_registry_layout(
    tmp_path: Path, malformed: bool
) -> None:
    root = tmp_path / "root"
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    assert adapter.migrate().status == "ready"
    _leave_wal_header_without_sidecars(root / "registry.sqlite", malformed=malformed)
    if malformed:
        with pytest.raises(StorageUnavailable):
            adapter.inspect_migration()
    else:
        assert adapter.inspect_migration().status == "ready"
    # WAL/SHM are operational SQLite state and may be recreated by inspection.
    assert (root / "registry.sqlite").exists()
    if malformed:
        with pytest.raises(StorageUnavailable):
            adapter.migrate()
        with pytest.raises(StorageUnavailable):
            adapter.open_uow()
    else:
        assert adapter.migrate().status == "ready"
        uow = adapter.open_uow()
        uow.close()


def test_readiness_rejects_dangling_foreign_key(tmp_path: Path) -> None:
    root = tmp_path / "root"
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    adapter.migrate()
    with sqlite3.connect(root / "registry.sqlite") as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            'INSERT INTO "aipcs_registry_audit"('
            "action,outcome,service_id,principal_id,created_via,at) VALUES (?,?,?,?,?,?)",
            ("seed", "created", str(UUID(int=99)), "p", "test", "2026-01-01T00:00:00.000000Z"),
        )
    assert adapter.inspect_migration().status == "incompatible"


def test_dirty_known_partial_layout_is_classified_dirty(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    database = root / "registry.sqlite"
    descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with sqlite3.connect(database) as connection:
        connection.execute(R1.ddl[0])
        connection.execute(
            'INSERT INTO "aipcs_registry_meta" VALUES (1,?,?,?,?)',
            ("aipcs.sqlite.registry", "registry", 0, 1),
        )
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    assert adapter.inspect_migration() == MigrationState("registry", 0, 3, "dirty")


def test_corrupt_service_and_replay_rows_fail_with_bounded_storage_error(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    context = ApplicationContext("p", "test")
    command = SeedCommand("notes", "project", "Notes", "key")
    created = harness.application(UUID(int=1)).seed(context, command)
    root = tmp_path / "registry-root"
    with sqlite3.connect(root / "registry.sqlite") as connection:
        connection.execute(
            'UPDATE "aipcs_registry_service" SET domain_name=? WHERE service_id=?',
            ("UPPER", str(created.service_id)),
        )
    uow = harness.adapter.open_uow()
    with pytest.raises(StorageMigrationError) as captured:
        uow.services.list("p", 100)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    uow.close()


def test_location_rejects_traversal_and_hides_paths(tmp_path: Path) -> None:
    with pytest.raises(StorageUnavailable):
        SQLiteLocationPolicy(Path("/tmp/safe/../other"))
    policy = SQLiteLocationPolicy(tmp_path / "root")
    assert not hasattr(policy, "root") and not hasattr(policy, "database_path")


def test_fresh_uow_is_query_only_until_write_gate(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    uow = harness.adapter.open_uow()
    assert uow._connection.execute("PRAGMA query_only").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        uow._connection.execute('DELETE FROM "aipcs_registry_service"')
    uow.rollback()
    uow.close()


def test_read_then_write_releases_snapshot_and_acquires_immediate_writer(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    uow = harness.adapter.open_uow()
    service = Service(
        service_id=UUID(int=1),
        principal_id="p",
        domain_name="notes",
        domain_class="project",
        intent_description="Read then write",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_activity_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert uow.services.get("p", service.service_id) is None
    assert uow._connection.in_transaction
    assert uow._connection.execute("PRAGMA query_only").fetchone()[0] == 1
    uow.services.add(service)
    assert uow._connection.in_transaction
    assert uow._connection.execute("PRAGMA query_only").fetchone()[0] == 0
    uow.commit()
    uow.close()

    observed = harness.adapter.open_uow()
    assert observed.services.get("p", service.service_id) == service
    observed.close()


def test_commit_after_durable_exception_replays_from_real_ledger(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    context = ApplicationContext("p", "test")
    command = SeedCommand("notes", "project", "Notes", "key")
    uow = harness.adapter.open_uow()

    class CommitAfterDurable:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def commit(self):
            self.wrapped.commit()
            raise sqlite3.OperationalError("synthetic commit uncertainty")

    uow._connection = CommitAfterDurable(uow._connection)
    uncertain = ServiceApplication(
        lambda: uow,
        FixedClock(datetime(2026, 1, 1, tzinfo=UTC)),
        SequentialIds(UUID(int=1)),
    )
    with pytest.raises(InternalFailure) as captured:
        uncertain.seed(context, command)
    assert captured.value.__cause__ is None and captured.value.__context__ is None
    replay = harness.restart(UUID(int=2)).seed(context, command)
    assert replay.service_id == UUID(int=1)


def test_lock_timeout_is_bounded_and_does_not_leak_path(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    database = tmp_path / "registry-root" / "registry.sqlite"
    blocker = sqlite3.connect(database, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        assert harness.adapter.inspect_migration().status == "ready"
    finally:
        blocker.rollback()
        blocker.close()


def test_nonzero_invalid_database_and_unsafe_root_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    database = root / "registry.sqlite"
    descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(descriptor, b"not-sqlite")
    os.close(descriptor)
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    with pytest.raises((StorageUnavailable, StorageMigrationError)):
        adapter.inspect_migration()
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    with pytest.raises(StorageUnavailable):
        SQLiteRegistryAdapter(SQLiteLocationPolicy(unsafe)).inspect_migration()


def test_malformed_replay_is_bounded(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    context = ApplicationContext("p", "test")
    harness.application(UUID(int=1)).seed(context, SeedCommand("notes", "project", "Notes", "key"))
    database = tmp_path / "registry-root" / "registry.sqlite"
    with sqlite3.connect(database) as connection:
        fingerprint = connection.execute(
            'SELECT fingerprint FROM "aipcs_registry_mutation" WHERE idempotency_key=?', ("key",)
        ).fetchone()[0]
        connection.execute(
            'UPDATE "aipcs_registry_mutation" SET result_json=? WHERE idempotency_key=?',
            ('{"unexpected":true}', "key"),
        )
    uow = harness.adapter.open_uow()
    with pytest.raises(StorageMigrationError) as captured:
        uow.mutations.resolve_non_lifecycle("seed", "p", "key", fingerprint)
    assert captured.value.__cause__ is None and captured.value.__context__ is None
    uow.rollback()
    uow.close()


def test_explicit_migrate_recovers_real_hot_rollback_journal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    adapter.migrate()
    database = root / "registry.sqlite"
    script = (
        "import os,sqlite3,sys;"
        "c=sqlite3.connect(sys.argv[1],isolation_level=None);"
        "c.execute('PRAGMA journal_mode=DELETE');"
        "c.execute('PRAGMA synchronous=FULL');"
        "c.execute('PRAGMA cache_size=1');"
        "c.execute('BEGIN EXCLUSIVE');"
        "c.execute('UPDATE aipcs_registry_meta SET dirty=1');"
        "c.execute('CREATE TABLE crash_probe(value BLOB)');"
        "c.execute('INSERT INTO crash_probe VALUES (zeroblob(1000000))');"
        "os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", script, str(database)], check=True)
    journal = root / "registry.sqlite-journal"
    assert journal.exists()
    os.chmod(journal, 0o600)
    with pytest.raises(StorageUnavailable):
        adapter.inspect_migration()
    assert adapter.migrate().status == "incompatible"
    assert not journal.exists()
