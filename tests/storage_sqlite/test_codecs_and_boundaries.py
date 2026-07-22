from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fixtures import valid_manifest

from aipcs_mcp.application.models import Service
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.storage.errors import StorageMigrationError, StorageUnavailable
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter
from aipcs_mcp.storage.sqlite import adapter as adapter_module
from aipcs_mcp.storage.sqlite.codecs import (
    decode_result,
    decode_time,
    encode_result,
    validate_service,
)
from aipcs_mcp.storage.sqlite.connection import set_query_only
from aipcs_mcp.storage.sqlite.uow import SQLiteRegistryUnitOfWork


def _service() -> Service:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Service(
        UUID(int=1),
        "principal",
        "notes",
        "project",
        "Notes",
        now,
        now,
        now,
    )


def _assert_bounded(error: BaseException) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "secret" not in str(error)


def test_service_id_must_be_an_actual_uuid_object() -> None:
    value = _service()
    object.__setattr__(value, "service_id", str(value.service_id))
    with pytest.raises(StorageMigrationError) as captured:
        validate_service(value)
    _assert_bounded(captured.value)


def test_replay_result_must_round_trip_to_identical_canonical_bytes() -> None:
    value = _service()
    canonical = encode_result(value)
    noncanonical = json.dumps(json.loads(canonical), indent=2)
    assert noncanonical != canonical
    with pytest.raises(StorageMigrationError) as captured:
        decode_result(noncanonical, value.principal_id, str(value.service_id))
    _assert_bounded(captured.value)
    assert decode_result(canonical, value.principal_id, str(value.service_id)) == value


def test_service_manifest_and_stored_schema_version_must_match() -> None:
    manifest = ManifestV2.model_validate(valid_manifest())
    value = replace(_service(), manifest=manifest, schema_version=2, design_state="materialised")
    with pytest.raises(StorageMigrationError) as captured:
        validate_service(value)
    _assert_bounded(captured.value)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00.00000Z",
        "2026-99-99T99:99:99.999999Z",
        1,
    ],
)
def test_invalid_time_codec_errors_are_bounded(timestamp: object) -> None:
    with pytest.raises(StorageMigrationError) as captured:
        decode_time(timestamp)
    _assert_bounded(captured.value)


class FaultConnection:
    def __init__(self, phase: str) -> None:
        self.phase = phase
        self.close_calls = 0

    @property
    def in_transaction(self) -> bool:
        return self.phase == "close_rollback"

    def execute(self, *args, **kwargs):
        raise RuntimeError(f"secret execute {args} {kwargs}")

    def commit(self):
        raise RuntimeError("secret commit")

    def rollback(self):
        raise RuntimeError("secret rollback")

    def close(self):
        self.close_calls += 1
        if self.phase == "close":
            raise RuntimeError("secret close")


@pytest.mark.parametrize("phase", ["read", "write", "commit", "rollback"])
def test_uow_failure_phases_have_bounded_direct_errors(phase: str) -> None:
    connection = FaultConnection(phase)
    uow = SQLiteRegistryUnitOfWork(connection)  # type: ignore[arg-type]
    with pytest.raises(StorageMigrationError) as captured:
        if phase == "read":
            uow.list("principal", 10)
        elif phase == "write":
            uow.add(_service())
        elif phase == "commit":
            uow.commit()
        else:
            uow.rollback()
    _assert_bounded(captured.value)


@pytest.mark.parametrize("phase", ["close_rollback", "close"])
def test_close_attempts_cleanup_and_becomes_idempotently_closed(phase: str) -> None:
    connection = FaultConnection(phase)
    uow = SQLiteRegistryUnitOfWork(connection)  # type: ignore[arg-type]
    with pytest.raises(StorageMigrationError) as captured:
        uow.close()
    _assert_bounded(captured.value)
    assert connection.close_calls == 1
    uow.close()
    assert connection.close_calls == 1


def test_connection_pragma_failure_is_bounded() -> None:
    connection = FaultConnection("pragma")
    with pytest.raises(StorageUnavailable) as captured:
        set_query_only(connection, True)  # type: ignore[arg-type]
    _assert_bounded(captured.value)


def test_open_uow_failure_is_bounded_and_does_not_reinspect(tmp_path: Path, monkeypatch) -> None:
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(tmp_path / "root"))
    adapter.migrate()

    def fail_connect(*args, **kwargs):
        raise RuntimeError(f"secret connect {args} {kwargs}")

    monkeypatch.setattr(adapter_module, "connect", fail_connect)
    with pytest.raises(StorageMigrationError) as captured:
        adapter.open_uow()
    _assert_bounded(captured.value)


def test_mutation_result_principal_must_match_ledger_principal(tmp_path: Path) -> None:
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(tmp_path / "root"))
    adapter.migrate()
    uow = adapter.open_uow()
    with pytest.raises(StorageMigrationError) as captured:
        uow.complete("other-principal", "key", "0" * 64, _service())
    _assert_bounded(captured.value)
    count = uow._connection.execute('SELECT count(*) FROM "aipcs_registry_mutation"').fetchone()[0]
    assert count == 0
    uow.rollback()
    uow.close()


@pytest.mark.parametrize("terminal", ["commit", "rollback", "close"])
def test_uow_rejects_use_after_terminal_state(tmp_path: Path, terminal: str) -> None:
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(tmp_path / terminal))
    adapter.migrate()
    uow = adapter.open_uow()
    getattr(uow, terminal)()
    if terminal != "close":
        uow.close()
    with pytest.raises(StorageMigrationError) as captured:
        uow.list("principal", 1)
    _assert_bounded(captured.value)
