from __future__ import annotations

import os
import socket
import sqlite3
import stat
import tempfile
from pathlib import Path
from uuid import UUID

import pytest

from aipcs_mcp.configuration.models import ConfigOverrides
from aipcs_mcp.configuration.resolver import resolve_configuration
from aipcs_mcp.storage.errors import StorageTransientJournal, StorageUnavailable
from aipcs_mcp.storage.sqlite import (
    SQLiteLocationPolicy,
    SQLiteRegistryAdapter,
    SQLiteServiceStoreCatalog,
)
from aipcs_mcp.storage.sqlite import location as location_module
from aipcs_mcp.storage.sqlite.connection import SQLiteConnectionPurpose, connect
from aipcs_mcp.storage.sqlite.location import create_database


def _resolved_policy(monkeypatch, platform: str, root_env: dict[str, str]):
    monkeypatch.setattr("aipcs_mcp.configuration.resolver.sys.platform", platform)
    config = resolve_configuration(
        overrides=ConfigOverrides(profile="sqlite", principal_id="operator"),
        environ=root_env,
        config_path=None,
    )
    assert config.sqlite_data_root is not None
    return config, SQLiteLocationPolicy.from_resolved(
        config.sqlite_data_root, config.sources["sqlite_data_root"]
    )


@pytest.mark.parametrize(
    ("platform", "suffix"),
    [
        ("linux", (".local", "share", "aipcs")),
        ("darwin", ("Library", "Application Support", "aipcs")),
    ],
)
def test_central_default_fallback_chain_is_created_only_by_migrate(
    tmp_path: Path, monkeypatch, platform: str, suffix: tuple[str, ...]
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    config, policy = _resolved_policy(monkeypatch, platform, {"HOME": str(home)})
    root = home.joinpath(*suffix)
    assert config.sqlite_data_root == root
    adapter = SQLiteRegistryAdapter(policy)
    assert adapter.inspect_migration().status == "uninitialised"
    assert not root.exists()
    assert adapter.migrate().status == "ready"
    assert root.is_dir()


def test_xdg_default_owns_only_the_application_leaf(tmp_path: Path, monkeypatch) -> None:
    base = tmp_path / "xdg"
    base.mkdir(mode=0o700)
    config, policy = _resolved_policy(
        monkeypatch, "linux", {"HOME": str(tmp_path), "XDG_DATA_HOME": str(base)}
    )
    assert config.sqlite_data_root == base / "aipcs"
    adapter = SQLiteRegistryAdapter(policy)
    assert adapter.inspect_migration().status == "uninitialised"
    assert list(base.iterdir()) == []
    assert adapter.migrate().status == "ready"
    assert {entry.name for entry in base.iterdir()} == {"aipcs"}


def test_explicit_resolved_root_requires_only_its_immediate_parent(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "operator-parent"
    parent.mkdir(mode=0o700)
    root = parent / "registry"
    config = resolve_configuration(
        overrides=ConfigOverrides(
            profile="sqlite", principal_id="operator", sqlite_data_root=str(root)
        ),
        environ={},
        config_path=None,
    )
    assert config.sources["sqlite_data_root"] == "cli"
    policy = SQLiteLocationPolicy.from_resolved(
        config.sqlite_data_root,
        config.sources["sqlite_data_root"],  # type: ignore[arg-type]
    )
    adapter = SQLiteRegistryAdapter(policy)
    assert adapter.inspect_migration().status == "uninitialised"
    assert adapter.migrate().status == "ready"

    missing_parent = tmp_path / "missing" / "registry"
    unavailable = SQLiteRegistryAdapter(SQLiteLocationPolicy(missing_parent))
    with pytest.raises(StorageUnavailable):
        unavailable.migrate()
    assert not missing_parent.parent.exists()


def test_windows_default_is_resolved_but_posix_adapter_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("aipcs_mcp.configuration.resolver.sys.platform", "win32")
    config = resolve_configuration(
        overrides=ConfigOverrides(profile="sqlite", principal_id="operator"),
        environ={"LOCALAPPDATA": str(tmp_path / "local")},
        config_path=None,
    )
    assert config.sqlite_data_root == tmp_path / "local" / "aipcs"
    monkeypatch.setattr(location_module.os, "name", "nt")
    with pytest.raises(StorageUnavailable):
        SQLiteLocationPolicy.from_resolved(
            config.sqlite_data_root, config.sources["sqlite_data_root"]
        )


def test_policy_never_reads_environment(tmp_path: Path, monkeypatch) -> None:
    class HostileEnvironment(dict):
        def __getitem__(self, key):
            raise AssertionError(key)

        def get(self, key, default=None):
            raise AssertionError(key)

    monkeypatch.setattr(location_module.os, "environ", HostileEnvironment())
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(tmp_path / "root"))
    assert adapter.migrate().status == "ready"


@pytest.mark.parametrize("kind", ["regular", "symlink"])
def test_root_must_be_a_private_owned_directory(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "root"
    if kind == "regular":
        root.write_bytes(b"not-a-directory")
    else:
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        root.symlink_to(target, target_is_directory=True)
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    with pytest.raises(StorageUnavailable):
        adapter.inspect_migration()


def test_missing_leaf_under_unsafe_parent_fails_closed(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o770)
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(unsafe / "root"))
    with pytest.raises(StorageUnavailable):
        adapter.inspect_migration()


@pytest.mark.parametrize("component", ["registry", "service_store"])
def test_explicit_root_rejects_an_intermediate_symlink(tmp_path: Path, component: str) -> None:
    actual_parent = tmp_path / "actual-parent"
    nested_parent = actual_parent / "nested-parent"
    nested_parent.mkdir(mode=0o700, parents=True)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    # The symlink is deliberately not the final component of root.parent:
    # a single O_NOFOLLOW open of root.parent would follow it.
    root = linked_parent / nested_parent.name / "root"
    policy = SQLiteLocationPolicy(root)

    if component == "registry":
        with pytest.raises(StorageUnavailable):
            SQLiteRegistryAdapter(policy).migrate()
    else:
        catalog = SQLiteServiceStoreCatalog(policy)
        locator = catalog.allocate(UUID(int=1))
        with pytest.raises(StorageUnavailable):
            catalog.migrate(locator)
    assert not (nested_parent / "root").exists()


def test_service_catalog_rejects_broad_default_root_for_inspection_and_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(location_module.sys, "platform", "linux")
    home = tmp_path / "home"
    root = home / ".local" / "share" / "aipcs"
    root.mkdir(mode=0o755, parents=True)
    root.chmod(0o755)
    policy = SQLiteLocationPolicy.from_resolved(root, "default")
    catalog = SQLiteServiceStoreCatalog(policy)
    locator = catalog.allocate(UUID(int=1))

    with pytest.raises(StorageUnavailable):
        catalog.inspect_migration(locator)
    with pytest.raises(StorageUnavailable):
        catalog.migrate(locator)
    assert not (root / "service-stores").exists()


@pytest.mark.parametrize("kind", ["hardlink", "fifo", "broad_mode"])
def test_database_must_be_a_single_private_regular_file(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    database = root / "registry.sqlite"
    if kind == "fifo":
        os.mkfifo(database, 0o600)
    else:
        descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        if kind == "hardlink":
            os.link(database, tmp_path / "second-link")
        else:
            database.chmod(0o644)
    with pytest.raises(StorageUnavailable):
        SQLiteRegistryAdapter(SQLiteLocationPolicy(root)).inspect_migration()


def test_database_socket_is_rejected_from_short_private_root() -> None:
    with tempfile.TemporaryDirectory(prefix="ap", dir="/tmp") as temporary:
        root = Path(temporary) / "root"
        root.mkdir(mode=0o700)
        bound_socket = socket.socket(socket.AF_UNIX)
        try:
            try:
                bound_socket.bind(str(root / "registry.sqlite"))
            except PermissionError:
                socket_stat = os.stat_result(
                    (stat.S_IFSOCK | 0o600, 0, 0, 1, os.geteuid(), 0, 0, 0, 0, 0)
                )
                with pytest.raises(StorageUnavailable):
                    location_module._validate_file_stat(socket_stat)
                return
            with pytest.raises(StorageUnavailable):
                SQLiteRegistryAdapter(SQLiteLocationPolicy(root)).inspect_migration()
        finally:
            bound_socket.close()


def test_creation_modes_ignore_permissive_umask(tmp_path: Path) -> None:
    root = tmp_path / "root"
    previous = os.umask(0)
    try:
        assert SQLiteRegistryAdapter(SQLiteLocationPolicy(root)).migrate().status == "ready"
    finally:
        os.umask(previous)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "registry.sqlite").stat().st_mode) == 0o600


def test_uri_metacharacters_are_opaque_path_data(tmp_path: Path) -> None:
    root = tmp_path / "percent%question?hash#"
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    assert adapter.migrate().status == "ready"
    uow = adapter.open_uow()
    uow.close()


@pytest.mark.parametrize("capability", ["mkdir", "stat_follow", "pread"])
def test_missing_descriptor_capability_fails_before_io(
    tmp_path: Path, monkeypatch, capability: str
) -> None:
    if capability == "mkdir":
        supported = set(location_module.os.supports_dir_fd)
        supported.discard(location_module.os.mkdir)
        monkeypatch.setattr(location_module.os, "supports_dir_fd", supported)
    elif capability == "stat_follow":
        supported = set(location_module.os.supports_follow_symlinks)
        supported.discard(location_module.os.stat)
        monkeypatch.setattr(location_module.os, "supports_follow_symlinks", supported)
    else:
        monkeypatch.delattr(location_module.os, "pread")
    with pytest.raises(StorageUnavailable):
        SQLiteLocationPolicy(tmp_path / "root")
    assert not (tmp_path / "root").exists()


def test_wrong_owner_seam_fails_closed(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "root"
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    adapter.migrate()
    monkeypatch.setattr(location_module.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(StorageUnavailable):
        adapter.inspect_migration()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "broad_mode"])
def test_unsafe_rollback_journal_entry_is_never_recovered(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "root"
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    adapter.migrate()
    outside = tmp_path / "outside"
    outside.write_bytes(b"sentinel")
    outside.chmod(0o600)
    journal = root / "registry.sqlite-journal"
    if kind == "symlink":
        journal.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, journal)
    else:
        journal.write_bytes(b"journal")
        journal.chmod(0o644)
    with pytest.raises(StorageUnavailable):
        adapter.inspect_migration()
    with pytest.raises(StorageUnavailable):
        adapter.migrate()
    assert outside.read_bytes() == b"sentinel"


@pytest.mark.parametrize(
    "root",
    [
        Path("/"),
        Path("/tmp/control\nname"),
        Path("/tmp/nul\x00name"),
        Path("//server/share"),
    ],
)
def test_nonlocal_or_nonprintable_roots_are_rejected(root: Path) -> None:
    with pytest.raises(StorageUnavailable):
        SQLiteLocationPolicy(root)


def test_identity_swap_is_detected_without_touching_unrelated_sentinel(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    policy = SQLiteLocationPolicy(root)
    adapter = SQLiteRegistryAdapter(policy)
    adapter.migrate()
    anchored = policy.acquire(create_root=False, allow_journal=False)
    assert anchored is not None
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"must-remain-unchanged")
    database = root / "registry.sqlite"
    database.rename(root / "original.sqlite")
    descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        with pytest.raises(StorageUnavailable):
            anchored.verify_database_identity()
    finally:
        anchored.close()
    assert sentinel.read_bytes() == b"must-remain-unchanged"


def test_location_objects_never_render_root_paths(tmp_path: Path) -> None:
    root = tmp_path / "secret-root"
    policy = SQLiteLocationPolicy(root)
    assert str(root) not in repr(policy)
    adapter = SQLiteRegistryAdapter(policy)
    adapter.migrate()
    anchored = policy.acquire(create_root=False, allow_journal=False)
    assert anchored is not None
    try:
        assert str(root) not in repr(anchored)
        assert not hasattr(anchored, "database_path")
    finally:
        anchored.close()


def test_safe_wal_sidecars_are_revalidated_across_live_peer_lifecycle(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    database = root / "registry.sqlite"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    database.chmod(0o600)
    wal = root / "registry.sqlite-wal"
    shm = root / "registry.sqlite-shm"
    wal.write_bytes(b"wal")
    shm.write_bytes(b"shm")
    wal.chmod(0o600)
    shm.chmod(0o600)

    anchored = SQLiteLocationPolicy(root).acquire(create_root=False, allow_journal=False)
    assert anchored is not None
    try:
        anchored.adopt_live_sidecars(allow_journal=False)
        anchored.verify_live_sidecars(allow_journal=False)

        wal.unlink()
        wal.write_bytes(b"replacement")
        wal.chmod(0o600)
        anchored.verify_live_sidecars(allow_journal=False)

        shm.unlink()
        shm.write_bytes(b"second-replacement")
        shm.chmod(0o600)
        anchored.verify_live_sidecars(allow_journal=False)

        wal.unlink()
        anchored.verify_live_sidecars(allow_journal=False)
    finally:
        anchored.close()


def test_live_metadata_checks_never_open_or_close_sqlite_locked_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    connection = connect(
        anchored,
        "rw",
        query_only=False,
        purpose=SQLiteConnectionPurpose.READY_OPERATION,
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        with monkeypatch.context() as live:
            live.setattr(
                location_module.os,
                "open",
                lambda *args, **kwargs: pytest.fail(
                    f"live validation opened SQLite file: {args!r} {kwargs!r}"
                ),
            )
            anchored.verify_live_database_identity()
            anchored.adopt_live_sidecars(allow_journal=False)
            anchored.verify_live_sidecars(allow_journal=False)
        connection.rollback()
    finally:
        connection.close()
        anchored.verify_database_identity()
        anchored.verify_closed_sidecars(allow_journal=False)
        anchored.close()


def test_late_safe_journal_uses_cached_header_without_opening_locked_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    database = root / "registry.sqlite"
    with sqlite3.connect(database) as raw:
        raw.execute("CREATE TABLE example (value INTEGER)")
    database.chmod(0o600)
    anchored = SQLiteLocationPolicy(root).acquire(
        create_root=False,
        allow_journal=False,
    )
    assert anchored is not None
    journal = root / "registry.sqlite-journal"
    journal.write_bytes(b"journal")
    journal.chmod(0o600)
    try:
        with monkeypatch.context() as live:
            live.setattr(
                location_module.os,
                "open",
                lambda *args, **kwargs: pytest.fail(
                    f"live validation opened SQLite file: {args!r} {kwargs!r}"
                ),
            )
            with pytest.raises(StorageTransientJournal):
                anchored.adopt_live_sidecars(allow_journal=False)
    finally:
        anchored.close()


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_orphan_wal_sidecars_are_rejected(tmp_path: Path, suffix: str) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    sidecar = root / f"registry.sqlite{suffix}"
    sidecar.write_bytes(b"orphan")
    sidecar.chmod(0o600)

    with pytest.raises(StorageUnavailable):
        SQLiteLocationPolicy(root).acquire(create_root=False, allow_journal=False)


def test_after_close_accepts_absence_or_a_new_safe_sidecar_identity(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    database = root / "registry.sqlite"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    database.chmod(0o600)
    wal = root / "registry.sqlite-wal"
    wal.write_bytes(b"wal")
    wal.chmod(0o600)

    anchored = SQLiteLocationPolicy(root).acquire(create_root=False, allow_journal=False)
    assert anchored is not None
    try:
        anchored.adopt_live_sidecars(allow_journal=False)
        wal.unlink()
        anchored.verify_closed_sidecars(allow_journal=False)
        wal.write_bytes(b"new")
        wal.chmod(0o600)
        anchored.verify_closed_sidecars(allow_journal=False)
    finally:
        anchored.close()


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "broad_mode"])
def test_unsafe_wal_sidecar_metadata_fails_closed(
    tmp_path: Path, suffix: str, kind: str
) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    database = root / "registry.sqlite"
    database.write_bytes(b"")
    database.chmod(0o600)
    outside = tmp_path / "outside"
    outside.write_bytes(b"sentinel")
    outside.chmod(0o600)
    sidecar = root / f"registry.sqlite{suffix}"
    if kind == "symlink":
        sidecar.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, sidecar)
    elif kind == "fifo":
        os.mkfifo(sidecar, 0o600)
    else:
        sidecar.write_bytes(b"sidecar")
        sidecar.chmod(0o640)

    with pytest.raises(StorageUnavailable):
        SQLiteLocationPolicy(root).acquire(create_root=False, allow_journal=False)
    assert outside.read_bytes() == b"sentinel"


def test_rollback_journal_is_rejected_beside_wal_operational_state(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    database = root / "registry.sqlite"
    database.write_bytes(b"")
    database.chmod(0o600)
    journal = root / "registry.sqlite-journal"
    wal = root / "registry.sqlite-wal"
    journal.write_bytes(b"journal")
    wal.write_bytes(b"wal")
    journal.chmod(0o600)
    wal.chmod(0o600)

    with pytest.raises(StorageUnavailable):
        SQLiteLocationPolicy(root).acquire(create_root=False, allow_journal=True)


def test_database_creation_reacquires_an_exact_concurrent_winner(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        first = create_database(root_fd)
        second = create_database(root_fd)
    finally:
        os.close(root_fd)

    assert (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)
    assert stat.S_IMODE(second.st_mode) == 0o600
    assert second.st_nlink == 1
