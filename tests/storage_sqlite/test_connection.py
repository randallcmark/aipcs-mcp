"""Focused tests for the SQLite connection/error policy."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from aipcs_mcp.storage.errors import StorageBusy, StorageUnavailable
from aipcs_mcp.storage.sqlite.connection import (
    SQLiteConnectionPolicy,
    SQLiteConnectionPurpose,
    connect,
    set_query_only,
)
from aipcs_mcp.storage.sqlite.location import SQLiteLocationPolicy
from aipcs_mcp.storage.sqlite.result_codes import is_sqlite_busy


class _Location:
    def __init__(self, database: Path) -> None:
        self._database = database
        self.sidecar_calls: list[tuple[str, bool]] = []

    def _sqlite_path(self) -> Path:
        return self._database

    def verify_database_identity(self) -> None:
        pass

    def verify_live_database_identity(self) -> None:
        pass

    def adopt_live_sidecars(self, *, allow_journal: bool) -> None:
        self.sidecar_calls.append(("adopt", allow_journal))

    def verify_live_sidecars(self, *, allow_journal: bool) -> None:
        self.sidecar_calls.append(("verify-live", allow_journal))

    def verify_closed_sidecars(self, *, allow_journal: bool) -> None:
        self.sidecar_calls.append(("verify-closed", allow_journal))


def _wal_database(tmp_path: Path) -> Path:
    database = tmp_path / "state.sqlite"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    return database


def _sqlite_error(code: int, message: str = "driver message") -> sqlite3.OperationalError:
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code
    return error


def test_ready_connection_applies_and_verifies_full_wal_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_connect = sqlite3.connect
    observed: dict[str, object] = {}

    def capture_timeout(*args: object, **kwargs: object) -> sqlite3.Connection:
        observed["timeout"] = kwargs["timeout"]
        return original_connect(*args, **kwargs)  # type: ignore[arg-type]

    database = _wal_database(tmp_path)
    location = _Location(database)
    monkeypatch.setattr(sqlite3, "connect", capture_timeout)
    connection = connect(
        location,  # type: ignore[arg-type]
        "rw",
        query_only=False,
        policy=SQLiteConnectionPolicy(1_234),
        purpose=SQLiteConnectionPurpose.READY_OPERATION,
    )
    try:
        expected = {
            "journal_mode": "wal",
            "locking_mode": "normal",
            "synchronous": 2,
            "foreign_keys": 1,
            "trusted_schema": 0,
            "recursive_triggers": 0,
            "query_only": 0,
            "wal_autocheckpoint": 1_000,
            "busy_timeout": 1_234,
        }
        assert {
            name: connection.execute(f"PRAGMA {name}").fetchone()[0]
            for name in expected
        } == expected
        assert observed["timeout"] == 1.234
        assert location.sidecar_calls == [("adopt", False), ("verify-live", False)]
    finally:
        connection.close()


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_migration_connection_allows_only_explicit_transition_modes(
    tmp_path: Path, journal_mode: str
) -> None:
    database = tmp_path / "state.sqlite"
    with sqlite3.connect(database) as raw:
        assert raw.execute(f"PRAGMA journal_mode={journal_mode.upper()}").fetchone()[0] == journal_mode

    connection = connect(
        _Location(database),  # type: ignore[arg-type]
        "rw",
        query_only=False,
        purpose=SQLiteConnectionPurpose.MIGRATION,
    )
    connection.close()


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_policy_inspection_reads_both_transition_modes_without_writes(
    tmp_path: Path, journal_mode: str
) -> None:
    database = tmp_path / "state.sqlite"
    with sqlite3.connect(database) as raw:
        assert raw.execute(f"PRAGMA journal_mode={journal_mode.upper()}").fetchone()[0] == journal_mode

    connection = connect(
        _Location(database),  # type: ignore[arg-type]
        "ro",
        query_only=True,
        purpose=SQLiteConnectionPurpose.POLICY_INSPECTION,
    )
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == journal_mode
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        connection.close()


def test_ready_connection_rejects_delete_mode(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    with sqlite3.connect(database):
        pass

    with pytest.raises(StorageUnavailable) as captured:
        connect(
            _Location(database),  # type: ignore[arg-type]
            "rw",
            query_only=False,
            purpose=SQLiteConnectionPurpose.READY_OPERATION,
        )

    assert str(captured.value) == "The requested storage is unavailable."
    assert captured.value.__cause__ is None


def test_connection_handoff_rejects_database_replacement_before_pragmas(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    database = root / "registry.sqlite"
    with sqlite3.connect(database) as raw:
        assert raw.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    database.chmod(0o600)
    anchored = SQLiteLocationPolicy(root).acquire(
        create_root=False,
        allow_journal=False,
    )
    assert anchored is not None
    original = root / "registry.original"
    real_connect = sqlite3.connect

    def replace_before_open(*args: object, **kwargs: object) -> sqlite3.Connection:
        database.rename(original)
        database.touch(mode=0o600)
        return real_connect(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sqlite3, "connect", replace_before_open)
    try:
        with pytest.raises(StorageUnavailable):
            connect(
                anchored,
                "rw",
                query_only=False,
                purpose=SQLiteConnectionPurpose.READY_OPERATION,
            )
    finally:
        anchored.close()

    assert database.stat().st_size == 0


def test_ready_read_only_connection_applies_the_same_wal_policy(tmp_path: Path) -> None:
    database = _wal_database(tmp_path)
    connection = connect(
        _Location(database),  # type: ignore[arg-type]
        "ro",
        query_only=True,
        purpose=SQLiteConnectionPurpose.READY_INSPECTION,
    )
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 1_000
    finally:
        connection.close()


def test_busy_classification_uses_primary_numeric_code_only() -> None:
    assert is_sqlite_busy(_sqlite_error(sqlite3.SQLITE_BUSY | (7 << 8)))
    assert not is_sqlite_busy(_sqlite_error(sqlite3.SQLITE_LOCKED))
    assert not is_sqlite_busy(_sqlite_error(sqlite3.SQLITE_IOERR, "database is locked"))
    assert not is_sqlite_busy(RuntimeError("database is busy"))


def test_connect_and_query_only_preserve_numeric_busy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = _wal_database(tmp_path)
    error = _sqlite_error(sqlite3.SQLITE_BUSY | (1 << 8))

    def busy_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise error

    monkeypatch.setattr(sqlite3, "connect", busy_connect)
    with pytest.raises(StorageBusy) as captured:
        connect(
            _Location(database),  # type: ignore[arg-type]
            "rw",
            query_only=False,
            purpose=SQLiteConnectionPurpose.READY_OPERATION,
        )
    assert str(captured.value) == "The requested storage is busy."
    assert captured.value.__cause__ is None

    class _BusyConnection:
        def execute(self, statement: str) -> None:
            raise _sqlite_error(sqlite3.SQLITE_BUSY)

    with pytest.raises(StorageBusy):
        set_query_only(_BusyConnection(), True)  # type: ignore[arg-type]


def test_direct_connection_rejects_sqlite_before_3513_before_location_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HostileLocation:
        def verify_database_identity(self) -> None:
            raise AssertionError("location I/O must not occur")

        def verify_live_database_identity(self) -> None:
            raise AssertionError("location I/O must not occur")

    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 51, 2))
    with pytest.raises(StorageUnavailable):
        connect(
            _HostileLocation(),  # type: ignore[arg-type]
            "rw",
            query_only=False,
            purpose=SQLiteConnectionPurpose.MIGRATION,
        )
