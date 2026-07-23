"""Adversarial checks for WAL/SHM operational-file containment."""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

from aipcs_mcp.storage.errors import StorageUnavailable
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy
from aipcs_mcp.storage.sqlite import connection as connection_module
from aipcs_mcp.storage.sqlite.connection import SQLiteConnectionPurpose, connect

_SERVICE_NAMESPACE = "svc_" + "0" * 32


def _private_file(path: Path, contents: bytes = b"") -> None:
    path.write_bytes(contents)
    path.chmod(0o600)


def _delete_database(path: Path) -> None:
    with sqlite3.connect(path) as database:
        assert database.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    path.chmod(0o600)


@pytest.mark.parametrize("sidecar_suffix", ["-wal", "-shm"])
@pytest.mark.parametrize("database_kind", ["empty", "delete"])
@pytest.mark.parametrize("component", ["registry", "service_store"])
def test_wal_operational_artifacts_are_rejected_without_a_wal_main_header(
    tmp_path: Path, component: str, database_kind: str, sidecar_suffix: str
) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    policy = SQLiteLocationPolicy(root)
    if component == "registry":
        database = root / "registry.sqlite"
    else:
        container = root / "service-stores"
        container.mkdir(mode=0o700)
        database = container / f"{_SERVICE_NAMESPACE}.sqlite"

    if database_kind == "empty":
        _private_file(database)
    else:
        _delete_database(database)
    _private_file(database.with_name(database.name + sidecar_suffix), b"untrusted sidecar")

    with pytest.raises(StorageUnavailable):
        if component == "registry":
            policy.acquire(create_root=False, allow_journal=False)
        else:
            policy.acquire_service_store(
                _SERVICE_NAMESPACE, create=False, allow_journal=False
            )


@pytest.mark.parametrize("sidecar_suffix", ["-wal", "-shm"])
def test_descriptor_relative_sidecar_rejection_never_follows_or_mutates_sentinel(
    tmp_path: Path, sidecar_suffix: str
) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    database = root / "registry.sqlite"
    _delete_database(database)
    sentinel = tmp_path / "outside-sentinel"
    _private_file(sentinel, b"must-not-change")
    before = sentinel.stat()
    (root / f"registry.sqlite{sidecar_suffix}").symlink_to(sentinel)

    with pytest.raises(StorageUnavailable):
        SQLiteLocationPolicy(root).acquire(create_root=False, allow_journal=False)

    after = sentinel.stat()
    assert sentinel.read_bytes() == b"must-not-change"
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
    assert after.st_mtime_ns == before.st_mtime_ns


def test_connect_error_validates_new_sidecars_without_mutating_outside_sentinel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    database = root / "registry.sqlite"
    with sqlite3.connect(database) as raw:
        assert raw.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    database.chmod(0o600)
    sentinel = tmp_path / "outside-sentinel"
    _private_file(sentinel, b"must-not-change")
    before = sentinel.stat()
    location = SQLiteLocationPolicy(root).acquire(create_root=False, allow_journal=False)
    assert location is not None

    def hostile_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        (root / "registry.sqlite-wal").symlink_to(sentinel)
        error = sqlite3.OperationalError("busy")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise error

    monkeypatch.setattr(connection_module.sqlite3, "connect", hostile_connect)
    try:
        with pytest.raises(StorageUnavailable):
            connect(
                location,
                "rw",
                query_only=False,
                purpose=SQLiteConnectionPurpose.READY_OPERATION,
            )
    finally:
        location.close()

    after = sentinel.stat()
    assert sentinel.read_bytes() == b"must-not-change"
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
    assert after.st_mtime_ns == before.st_mtime_ns
