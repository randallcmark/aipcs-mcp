"""Focused SQLite conformance for the durable lifecycle registry contract."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fixtures import valid_manifest

from aipcs_mcp.application.models import (
    AuditEvent,
    CompletedLifecycleClaim,
    ConflictClaim,
    EvolveCompletion,
    MaterialisationStorage,
    MaterialiseCompletion,
    OperationInProgress,
    PreparedLifecycleClaim,
    RecoveryRequiredLifecycleClaim,
    Service,
    StaleRevision,
    UnsupportedTransition,
)
from aipcs_mcp.lifecycle import (
    MAX_SERVICE_REVISION,
    EvolveCommand,
    MaterialiseCommand,
    lifecycle_fingerprint,
    prepare_intent,
)
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.storage.errors import StorageMigrationError
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter
from aipcs_mcp.storage.sqlite.codecs import decode_lifecycle_intent, encode_manifest, encode_result

_START = datetime(2026, 7, 23, tzinfo=UTC)
_PRINCIPAL = "principal-a"
_SECOND_PRINCIPAL = "principal-b"
_SERVICE_ID = UUID("5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23")
_SECOND_SERVICE_ID = UUID("6b5d0b6a-976c-4e32-b4d2-cc0a64e3ee24")


def _manifest(version: int = 1) -> ManifestV2:
    payload = valid_manifest()
    payload["schema_version"] = version
    payload["migration_history"] = [
        {
            "from_schema_version": prior,
            "to_schema_version": prior + 1,
            "operations": [f"advance schema to version {prior + 1}"],
        }
        for prior in range(1, version)
    ]
    return ManifestV2.model_validate(payload)


def _adapter(tmp_path: Path) -> SQLiteRegistryAdapter:
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(tmp_path / "registry"))
    assert adapter.migrate().status == "ready"
    return adapter


def _database(tmp_path: Path) -> Path:
    return tmp_path / "registry" / "registry.sqlite"


def _write[T](adapter: SQLiteRegistryAdapter, operation: Callable[[object], T]) -> T:
    uow = adapter.open_uow()
    try:
        result = operation(uow)
        uow.commit()
        return result
    except Exception:
        uow.rollback()
        raise
    finally:
        uow.close()


def _service(
    principal_id: str = _PRINCIPAL,
    service_id: UUID = _SERVICE_ID,
    *,
    operational_status: str = "active",
) -> Service:
    manifest = _manifest()
    return Service(
        service_id=service_id,
        principal_id=principal_id,
        domain_name=f"notes_{service_id.hex[:8]}",
        domain_class="project",
        intent_description="Durable notes",
        created_at=_START,
        updated_at=_START,
        last_activity_at=_START,
        manifest=manifest,
        schema_version=1,
        design_state="seeded",
        operational_status=operational_status,  # type: ignore[arg-type]
    )


def _add(adapter: SQLiteRegistryAdapter, service: Service) -> None:
    _write(adapter, lambda uow: uow.services.add(service))


def _materialise(
    *,
    service_id: UUID = _SERVICE_ID,
    key: str = "materialise-1",
    expected_service_revision: int = 1,
) -> MaterialiseCommand:
    return MaterialiseCommand(
        principal_id=_PRINCIPAL,
        created_via="registry-test",
        service_id=service_id,
        expected_service_revision=expected_service_revision,
        expected_schema_version=1,
        idempotency_key=key,
    )


def _evolve(*, key: str = "evolve-1") -> EvolveCommand:
    return EvolveCommand(
        principal_id=_PRINCIPAL,
        created_via="registry-test",
        service_id=_SERVICE_ID,
        expected_service_revision=2,
        expected_schema_version=1,
        idempotency_key=key,
        target_manifest=_manifest(2),
    )


def _admit(
    adapter: SQLiteRegistryAdapter, command: MaterialiseCommand | EvolveCommand
) -> object:
    return _write(adapter, lambda uow: uow.mutations.resolve_or_admit(command))


def _service_state(adapter: SQLiteRegistryAdapter, service_id: UUID = _SERVICE_ID) -> Service:
    service = _write(adapter, lambda uow: uow.services.get(_PRINCIPAL, service_id))
    assert service is not None
    return service


def _registry_snapshot(database: Path) -> tuple[list[tuple[object, ...]], ...]:
    with sqlite3.connect(database) as connection:
        return tuple(
            connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            for table in (
                "aipcs_registry_service",
                "aipcs_registry_mutation",
                "aipcs_registry_audit",
            )
        )


class _ExistingKeyReadGuard:
    """Connection proxy that makes post-claim service/blocker reads observable."""

    def __init__(self, wrapped: sqlite3.Connection) -> None:
        self._wrapped = wrapped

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if 'FROM "aipcs_registry_service"' in sql or (
            'FROM "aipcs_registry_mutation"' in sql and 'AND "service_id"=?' in sql
        ):
            raise AssertionError("existing-key resolution touched service/blocker state")
        return self._wrapped.execute(sql, parameters)

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)


def test_existing_global_key_conflicts_before_unknown_service_is_examined(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    _add(adapter, _service())
    command = _materialise(key="global-key")
    assert isinstance(_admit(adapter, command), PreparedLifecycleClaim)

    unknown = _materialise(service_id=UUID(int=999), key="global-key")
    assert isinstance(_admit(adapter, unknown), ConflictClaim)


def test_materialise_prepared_replays_and_prepared_then_recovery_blocks_service(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    _add(adapter, _service())
    command = _materialise()
    prepared = _admit(adapter, command)
    assert isinstance(prepared, PreparedLifecycleClaim)
    replay = _admit(adapter, command)
    assert isinstance(replay, PreparedLifecycleClaim)
    assert replay.intent == prepared.intent

    assert isinstance(_admit(adapter, _materialise(key="blocked")), OperationInProgress)
    recovery = _write(
        adapter,
        lambda uow: uow.mutations.finalize_recovery_required(
            prepared.intent, _START + timedelta(seconds=1)
        ),
    )
    assert isinstance(recovery, RecoveryRequiredLifecycleClaim)
    blocked = _admit(adapter, _materialise(key="blocked-after-recovery"))
    assert isinstance(blocked, RecoveryRequiredLifecycleClaim)
    assert blocked.intent == recovery.intent


def test_stale_and_unsupported_new_keys_never_insert_prepared_intent(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    _add(adapter, _service())
    stale = _admit(adapter, _materialise(key="stale", expected_service_revision=2))
    missing = _admit(adapter, _materialise(key="missing", service_id=UUID(int=998)))
    assert isinstance(stale, StaleRevision)
    assert isinstance(missing, StaleRevision)

    _add(adapter, _service(service_id=_SECOND_SERVICE_ID, operational_status="suspended"))
    unsupported = _admit(adapter, _materialise(key="unsupported", service_id=_SECOND_SERVICE_ID))
    assert isinstance(unsupported, UnsupportedTransition)

    with sqlite3.connect(tmp_path / "registry" / "registry.sqlite") as connection:
        assert connection.execute('SELECT count(*) FROM "aipcs_registry_mutation"').fetchone() == (0,)


def test_service_save_is_exact_cas_success_then_stale_without_blind_overwrite(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    _add(adapter, _service())
    original = _service_state(adapter)
    saved_at = _START + timedelta(seconds=1)
    saved = replace(
        original,
        service_revision=2,
        updated_at=saved_at,
        last_activity_at=saved_at,
    )
    assert _write(adapter, lambda uow: uow.services.save(saved, 1)) == "saved"

    stale_at = _START + timedelta(seconds=2)
    stale = replace(saved, updated_at=stale_at, last_activity_at=stale_at)
    assert _write(adapter, lambda uow: uow.services.save(stale, 1)) == "stale_revision"
    assert _service_state(adapter).service_revision == 2


def test_materialise_then_evolve_constructs_exact_terminal_registry_services(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    _add(adapter, _service())
    materialise = _admit(adapter, _materialise())
    assert isinstance(materialise, PreparedLifecycleClaim)
    storage = MaterialisationStorage("sqlite", f"svc_{_SERVICE_ID.hex}")
    materialised = _write(
        adapter,
        lambda uow: uow.mutations.finalize_completed(
            MaterialiseCompletion(materialise.intent, _START + timedelta(seconds=1), storage)
        ),
    )
    assert isinstance(materialised, CompletedLifecycleClaim)
    assert materialised.service.service_revision == 2
    assert materialised.service.design_state == "materialised"
    assert materialised.service.manifest == materialise.intent.target_manifest
    assert materialised.service.storage == storage

    evolve_command = _evolve()
    evolve = _admit(adapter, evolve_command)
    assert isinstance(evolve, PreparedLifecycleClaim)
    evolved = _write(
        adapter,
        lambda uow: uow.mutations.finalize_completed(
            EvolveCompletion(evolve.intent, _START + timedelta(seconds=2))
        ),
    )
    assert isinstance(evolved, CompletedLifecycleClaim)
    assert evolved.service.service_revision == 3
    assert evolved.service.manifest == evolve_command.target_manifest
    assert evolved.service.schema_version == 2
    assert evolved.service.materialised_at == materialised.service.materialised_at
    assert evolved.service.storage == storage

    with sqlite3.connect(tmp_path / "registry" / "registry.sqlite") as connection:
        assert connection.execute(
            'SELECT "action","outcome" FROM "aipcs_registry_audit" ORDER BY "audit_id"'
        ).fetchall() == [("materialise", "completed"), ("evolve", "completed")]


@pytest.mark.parametrize("corruption", ["materialised_at", "storage"])
def test_ready_r2_evolve_replay_preserves_materialisation_identity(
    tmp_path: Path, corruption: str
) -> None:
    adapter = _adapter(tmp_path)
    _add(adapter, _service())
    materialise = _admit(adapter, _materialise())
    assert isinstance(materialise, PreparedLifecycleClaim)
    materialised = _write(
        adapter,
        lambda uow: uow.mutations.finalize_completed(
            MaterialiseCompletion(
                materialise.intent,
                _START + timedelta(seconds=1),
                MaterialisationStorage("sqlite", f"svc_{_SERVICE_ID.hex}"),
            )
        ),
    )
    assert isinstance(materialised, CompletedLifecycleClaim)
    evolve = _admit(adapter, _evolve())
    assert isinstance(evolve, PreparedLifecycleClaim)
    evolved = _write(
        adapter,
        lambda uow: uow.mutations.finalize_completed(
            EvolveCompletion(evolve.intent, _START + timedelta(seconds=2))
        ),
    )
    assert isinstance(evolved, CompletedLifecycleClaim)
    if corruption == "materialised_at":
        forged = replace(evolved.service, materialised_at=_START)
    else:
        forged = replace(
            evolved.service,
            storage=MaterialisationStorage("postgresql", f"svc_{_SERVICE_ID.hex}"),
        )
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            'UPDATE "aipcs_registry_mutation" SET "result_json"=? '
            'WHERE "principal_id"=? AND "idempotency_key"=?',
            (encode_result(forged), _PRINCIPAL, "evolve-1"),
        )
    before = database.read_bytes()
    assert adapter.inspect_migration().status == "incompatible"
    assert database.read_bytes() == before


def test_ready_r2_completed_lifecycle_revision_cannot_be_ahead_of_current_service(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path)
    _add(adapter, _service())
    prepared = _admit(adapter, _materialise())
    assert isinstance(prepared, PreparedLifecycleClaim)
    completed = _write(
        adapter,
        lambda uow: uow.mutations.finalize_completed(
            MaterialiseCompletion(
                prepared.intent,
                _START + timedelta(seconds=1),
                MaterialisationStorage("sqlite", f"svc_{_SERVICE_ID.hex}"),
            )
        ),
    )
    assert isinstance(completed, CompletedLifecycleClaim)
    forged_command = _materialise(key="materialise-1", expected_service_revision=99)
    forged_result = replace(completed.service, service_revision=100)
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            'UPDATE "aipcs_registry_mutation" SET "fingerprint"=?,'
            '"expected_service_revision"=?,"result_json"=? '
            'WHERE "principal_id"=? AND "idempotency_key"=?',
            (
                lifecycle_fingerprint(forged_command),
                99,
                encode_result(forged_result),
                _PRINCIPAL,
                "materialise-1",
            ),
        )
    before = database.read_bytes()
    assert adapter.inspect_migration().status == "incompatible"
    assert database.read_bytes() == before


def test_ready_r2_current_revision_completion_must_equal_current_snapshot(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path)
    _add(adapter, _service())
    prepared = _admit(adapter, _materialise())
    assert isinstance(prepared, PreparedLifecycleClaim)
    completed = _write(
        adapter,
        lambda uow: uow.mutations.finalize_completed(
            MaterialiseCompletion(
                prepared.intent,
                _START + timedelta(seconds=1),
                MaterialisationStorage("sqlite", f"svc_{_SERVICE_ID.hex}"),
            )
        ),
    )
    assert isinstance(completed, CompletedLifecycleClaim)
    forged_manifest = _manifest()
    forged_manifest.retrieval_guidance = "Forged but structurally valid guidance."
    forged_result = replace(completed.service, manifest=forged_manifest)
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            'UPDATE "aipcs_registry_mutation" SET "target_manifest_json"=?,'
            '"result_json"=? WHERE "principal_id"=? AND "idempotency_key"=?',
            (
                encode_manifest(forged_manifest),
                encode_result(forged_result),
                _PRINCIPAL,
                "materialise-1",
            ),
        )
    before = database.read_bytes()
    assert adapter.inspect_migration().status == "incompatible"
    assert database.read_bytes() == before


def test_ready_r2_prepared_lifecycle_schema_cannot_be_ahead_of_current_service(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path)
    _add(adapter, _service())
    materialise = _admit(adapter, _materialise())
    assert isinstance(materialise, PreparedLifecycleClaim)
    _write(
        adapter,
        lambda uow: uow.mutations.finalize_completed(
            MaterialiseCompletion(
                materialise.intent,
                _START + timedelta(seconds=1),
                MaterialisationStorage("sqlite", f"svc_{_SERVICE_ID.hex}"),
            )
        ),
    )
    prepared = _admit(adapter, _evolve())
    assert isinstance(prepared, PreparedLifecycleClaim)
    target = _manifest(65)
    forged_command = EvolveCommand(
        principal_id=_PRINCIPAL,
        created_via="registry-test",
        service_id=_SERVICE_ID,
        expected_service_revision=2,
        expected_schema_version=64,
        idempotency_key="evolve-1",
        target_manifest=target,
    )
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            'UPDATE "aipcs_registry_mutation" SET "fingerprint"=?,'
            '"expected_schema_version"=?,"target_manifest_json"=? '
            'WHERE "principal_id"=? AND "idempotency_key"=?',
            (
                lifecycle_fingerprint(forged_command),
                64,
                encode_manifest(target),
                _PRINCIPAL,
                "evolve-1",
            ),
        )
    before = database.read_bytes()
    assert adapter.inspect_migration().status == "incompatible"
    assert database.read_bytes() == before


def test_stale_finalization_is_recovery_required_with_audit_and_no_service_overwrite(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    _add(adapter, _service())
    prepared = _admit(adapter, _materialise())
    assert isinstance(prepared, PreparedLifecycleClaim)

    current = _service_state(adapter)
    concurrent_at = _START + timedelta(seconds=1)
    concurrent = replace(
        current,
        service_revision=2,
        updated_at=concurrent_at,
        last_activity_at=concurrent_at,
    )
    assert _write(adapter, lambda uow: uow.services.save(concurrent, 1)) == "saved"
    result = _write(
        adapter,
        lambda uow: uow.mutations.finalize_completed(
            MaterialiseCompletion(
                prepared.intent,
                _START + timedelta(seconds=2),
                MaterialisationStorage("sqlite", f"svc_{_SERVICE_ID.hex}"),
            )
        ),
    )
    assert isinstance(result, RecoveryRequiredLifecycleClaim)
    final = _service_state(adapter)
    assert final == concurrent
    assert isinstance(_admit(adapter, _materialise()), RecoveryRequiredLifecycleClaim)
    replay = _write(
        adapter,
        lambda uow: uow.mutations.finalize_recovery_required(
            prepared.intent, _START + timedelta(seconds=3)
        ),
    )
    assert isinstance(replay, RecoveryRequiredLifecycleClaim)
    with sqlite3.connect(tmp_path / "registry" / "registry.sqlite") as connection:
        assert connection.execute(
            'SELECT "action","outcome" FROM "aipcs_registry_audit"'
        ).fetchall() == [("materialise", "recovery_required")]


def test_completed_finalization_and_claim_replay_are_idempotent(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    _add(adapter, _service())
    command = _materialise()
    prepared = _admit(adapter, command)
    assert isinstance(prepared, PreparedLifecycleClaim)
    completion = MaterialiseCompletion(
        prepared.intent,
        _START + timedelta(seconds=1),
        MaterialisationStorage("sqlite", f"svc_{_SERVICE_ID.hex}"),
    )
    completed = _write(adapter, lambda uow: uow.mutations.finalize_completed(completion))
    assert isinstance(completed, CompletedLifecycleClaim)
    assert isinstance(_admit(adapter, command), CompletedLifecycleClaim)
    replay = _write(adapter, lambda uow: uow.mutations.finalize_completed(completion))
    assert isinstance(replay, CompletedLifecycleClaim)
    assert replay.service == completed.service


def test_existing_completed_key_never_reads_service_or_blocker_state(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    _add(adapter, _service())
    command = _materialise(key="guarded-key")
    prepared = _admit(adapter, command)
    assert isinstance(prepared, PreparedLifecycleClaim)
    completion = MaterialiseCompletion(
        prepared.intent,
        _START + timedelta(seconds=1),
        MaterialisationStorage("sqlite", f"svc_{_SERVICE_ID.hex}"),
    )
    assert isinstance(
        _write(adapter, lambda uow: uow.mutations.finalize_completed(completion)),
        CompletedLifecycleClaim,
    )

    uow = adapter.open_uow()
    uow._connection = _ExistingKeyReadGuard(uow._connection)
    try:
        assert isinstance(uow.mutations.resolve_or_admit(command), CompletedLifecycleClaim)
        changed = _materialise(key="guarded-key", service_id=UUID(int=999))
        assert isinstance(uow.mutations.resolve_or_admit(changed), ConflictClaim)
    finally:
        uow.rollback()
        uow.close()


def test_mutation_table_rejects_invalid_kind_phase_and_evidence_combinations(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path)
    _add(adapter, _service())
    command = _materialise()
    intent = prepare_intent(command, _manifest())
    base: dict[str, object] = {
        "principal_id": _PRINCIPAL,
        "idempotency_key": "invalid-shape",
        "fingerprint": intent.fingerprint,
        "service_id": str(_SERVICE_ID),
        "operation_kind": "materialise",
        "phase": "prepared",
        "created_via": intent.created_via,
        "expected_service_revision": 1,
        "expected_schema_version": 1,
        "target_manifest_json": encode_manifest(intent.target_manifest),
        "result_json": None,
        "recovery_category": None,
    }
    invalid_overrides = (
        {"operation_kind": "unknown"},
        {"phase": "unknown"},
        {"phase": "prepared", "result_json": "{}"},
        {"phase": "completed", "result_json": None},
        {"phase": "completed", "result_json": "{}", "recovery_category": "recovery_required"},
        {"phase": "recovery_required", "recovery_category": None},
        {
            "phase": "recovery_required",
            "result_json": "{}",
            "recovery_category": "recovery_required",
        },
        {"operation_kind": "materialise", "expected_schema_version": None},
        {"operation_kind": "materialise", "expected_schema_version": 2},
        {"operation_kind": "evolve", "expected_schema_version": None},
        {"operation_kind": "evolve", "expected_schema_version": 65},
        {
            "operation_kind": "seed",
            "phase": "prepared",
            "created_via": None,
            "expected_service_revision": None,
            "expected_schema_version": None,
            "target_manifest_json": None,
            "result_json": "{}",
        },
    )
    columns = tuple(base)
    placeholders = ",".join("?" for _ in columns)
    quoted_columns = ",".join(f'"{column}"' for column in columns)
    sql = f'INSERT INTO "aipcs_registry_mutation" ({quoted_columns}) VALUES ({placeholders})'
    with sqlite3.connect(_database(tmp_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for index, overrides in enumerate(invalid_overrides):
            candidate = base | overrides | {"idempotency_key": f"invalid-{index}"}
            try:
                connection.execute(sql, tuple(candidate[column] for column in columns))
            except sqlite3.IntegrityError:
                pass
            else:
                pytest.fail(f"invalid mutation shape was accepted: {overrides!r}")
        assert connection.execute(
            'SELECT count(*) FROM "aipcs_registry_mutation"'
        ).fetchone() == (0,)


def test_noncanonical_target_and_malformed_fingerprint_fail_bounded_codec_replay(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path)
    _add(adapter, _service())
    command = _materialise(key="corrupt-intent")
    assert isinstance(_admit(adapter, command), PreparedLifecycleClaim)
    database = _database(tmp_path)

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            'SELECT * FROM "aipcs_registry_mutation" WHERE "idempotency_key"=?',
            (command.idempotency_key,),
        ).fetchone()
        assert row is not None
        columns = [description[0] for description in connection.execute(
            'SELECT * FROM "aipcs_registry_mutation" LIMIT 0'
        ).description]
        codec_row = dict(zip(columns, row, strict=True))
        codec_row["fingerprint"] = "not-a-fingerprint"
        with pytest.raises(StorageMigrationError) as malformed:
            decode_lifecycle_intent(codec_row)
        assert malformed.value.__cause__ is None and malformed.value.__context__ is None

        canonical = json.loads(codec_row["target_manifest_json"])
        connection.execute(
            'UPDATE "aipcs_registry_mutation" SET "target_manifest_json"=? '
            'WHERE "idempotency_key"=?',
            (json.dumps(canonical, indent=2), command.idempotency_key),
        )

    before = _registry_snapshot(database)
    with pytest.raises(StorageMigrationError) as noncanonical:
        _admit(adapter, command)
    assert noncanonical.value.__cause__ is None and noncanonical.value.__context__ is None
    assert _registry_snapshot(database) == before


def test_lifecycle_intent_codec_rejects_phase_evidence_even_without_database_checks() -> None:
    intent = prepare_intent(_materialise(), _manifest())
    base: dict[str, object] = {
        "principal_id": intent.principal_id,
        "idempotency_key": intent.idempotency_key,
        "fingerprint": intent.fingerprint,
        "service_id": str(intent.service_id),
        "operation_kind": intent.kind.value,
        "phase": "prepared",
        "created_via": intent.created_via,
        "expected_service_revision": intent.expected_service_revision,
        "expected_schema_version": intent.expected_schema_version,
        "target_manifest_json": encode_manifest(intent.target_manifest),
        "result_json": None,
        "recovery_category": None,
    }
    invalid_evidence = (
        {"phase": "prepared", "result_json": "{}"},
        {"phase": "completed", "result_json": None},
        {"phase": "completed", "result_json": "{}", "recovery_category": "recovery_required"},
        {"phase": "recovery_required", "recovery_category": None},
        {"phase": "recovery_required", "recovery_category": "wrong"},
        {
            "phase": "recovery_required",
            "result_json": "{}",
            "recovery_category": "recovery_required",
        },
    )

    for overrides in invalid_evidence:
        with pytest.raises(StorageMigrationError) as captured:
            decode_lifecycle_intent(base | overrides)
        assert captured.value.__cause__ is None and captured.value.__context__ is None
        assert captured.value.args == (StorageMigrationError.message,)


def test_invalid_completion_values_and_corrupt_target_do_not_mutate_registry(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    _add(adapter, _service())
    command = _materialise(key="strict-completion")
    prepared = _admit(adapter, command)
    assert isinstance(prepared, PreparedLifecycleClaim)
    database = _database(tmp_path)
    before = _registry_snapshot(database)

    evolve = prepare_intent(_evolve(key="wrong-kind"), _manifest(2))
    storage = MaterialisationStorage("sqlite", f"svc_{_SERVICE_ID.hex}")
    with pytest.raises(ValueError):
        MaterialiseCompletion(evolve, _START + timedelta(seconds=1), storage)
    with pytest.raises(ValueError):
        MaterialiseCompletion(
            prepared.intent,
            _START + timedelta(seconds=1),
            MaterialisationStorage("sqlite", f"svc_{_SECOND_SERVICE_ID.hex}"),
        )
    with pytest.raises(ValueError):
        MaterialiseCompletion(prepared.intent, datetime(2026, 7, 23), storage)
    assert _registry_snapshot(database) == before

    with sqlite3.connect(database) as connection:
        target = json.loads(encode_manifest(prepared.intent.target_manifest))
        target["schema_version"] = 2
        connection.execute(
            'UPDATE "aipcs_registry_mutation" SET "target_manifest_json"=? '
            'WHERE "idempotency_key"=?',
            (
                json.dumps(target, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
                command.idempotency_key,
            ),
        )
    corrupted = _registry_snapshot(database)
    valid_completion = MaterialiseCompletion(
        prepared.intent, _START + timedelta(seconds=1), storage
    )
    with pytest.raises(StorageMigrationError):
        _write(adapter, lambda uow: uow.mutations.finalize_completed(valid_completion))
    assert _registry_snapshot(database) == corrupted


def test_completion_before_current_time_is_rejected_without_terminal_mutation(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    _add(adapter, _service())
    prepared = _admit(adapter, _materialise(key="past-completion"))
    assert isinstance(prepared, PreparedLifecycleClaim)
    database = _database(tmp_path)
    before = _registry_snapshot(database)
    completion = MaterialiseCompletion(
        prepared.intent,
        _START - timedelta(microseconds=1),
        MaterialisationStorage("sqlite", f"svc_{_SERVICE_ID.hex}"),
    )
    with pytest.raises(StorageMigrationError):
        _write(adapter, lambda uow: uow.mutations.finalize_completed(completion))
    assert _registry_snapshot(database) == before


def test_max_service_revision_admission_creates_no_intent_audit_or_service_write(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path)
    maximum = replace(_service(), service_revision=MAX_SERVICE_REVISION)
    _add(adapter, maximum)
    database = _database(tmp_path)
    before = _registry_snapshot(database)
    result = _admit(
        adapter,
        _materialise(key="overflow", expected_service_revision=MAX_SERVICE_REVISION),
    )
    assert isinstance(result, UnsupportedTransition)
    assert _registry_snapshot(database) == before


def test_audit_keeps_highest_thousand_ids_per_principal_and_never_prunes_mutations(
    tmp_path: Path,
) -> None:
    adapter = _adapter(tmp_path)
    _add(adapter, _service())
    _add(adapter, _service(_SECOND_PRINCIPAL, _SECOND_SERVICE_ID))
    prepared = _admit(adapter, _materialise(key="retained-intent"))
    assert isinstance(prepared, PreparedLifecycleClaim)

    def append_many(uow: object) -> None:
        for index in range(1_001):
            at = _START + timedelta(seconds=index)
            uow.audits.append(
                AuditEvent("seed", "created", _SERVICE_ID, _PRINCIPAL, "registry-test", at)
            )
            uow.audits.append(
                AuditEvent(
                    "seed", "created", _SECOND_SERVICE_ID, _SECOND_PRINCIPAL, "registry-test", at
                )
            )

    _write(adapter, append_many)
    with sqlite3.connect(tmp_path / "registry" / "registry.sqlite") as connection:
        for principal in (_PRINCIPAL, _SECOND_PRINCIPAL):
            audit_ids = [
                row[0]
                for row in connection.execute(
                    'SELECT "audit_id" FROM "aipcs_registry_audit" '
                    'WHERE "principal_id"=? ORDER BY "audit_id"',
                    (principal,),
                )
            ]
            assert len(audit_ids) == 1_000
            assert audit_ids == list(range(audit_ids[0], audit_ids[-1] + 1, 2))
        assert connection.execute(
            'SELECT "phase" FROM "aipcs_registry_mutation" '
            'WHERE "principal_id"=? AND "idempotency_key"=?',
            (_PRINCIPAL, "retained-intent"),
        ).fetchone() == ("prepared",)
