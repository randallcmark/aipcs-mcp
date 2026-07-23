"""SYNTHETIC_FIXTURE. SQLite service-store foundation security and migration tests."""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from uuid import UUID

import pytest
from storage_contracts.conformance import assert_service_store_catalog_conformance

from aipcs_mcp.storage import MigrationState, ServiceStoreLocator, StorageContractError
from aipcs_mcp.storage.errors import (
    StorageMigrationError,
    StorageTransientJournal,
    StorageUnavailable,
)
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteServiceStoreCatalog
from aipcs_mcp.storage.sqlite import service_store as catalog_module
from aipcs_mcp.storage.sqlite import service_store_inspection as inspection_module
from aipcs_mcp.storage.sqlite.connection import SQLiteConnectionPurpose, connect
from aipcs_mcp.storage.sqlite.service_store_migrations import (
    CHECKSUM,
    EXPECTED_SQL,
    META,
    MIGRATION,
    RESERVED_PREFIX,
    TARGET_REVISION,
)


def _catalog(root: Path) -> SQLiteServiceStoreCatalog:
    return SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root))


def _locator(value: int = 1) -> ServiceStoreLocator:
    return ServiceStoreLocator.for_service("sqlite", UUID(int=value))


def _database(root: Path, locator: ServiceStoreLocator) -> Path:
    return root / "service-stores" / f"{locator.namespace}.sqlite"


def _ready(
    tmp_path: Path, value: int = 1
) -> tuple[SQLiteServiceStoreCatalog, ServiceStoreLocator, Path, Path]:
    root = tmp_path / "root"
    catalog = _catalog(root)
    locator = _locator(value)
    assert catalog.migrate(locator) == MigrationState(
        "service_store", TARGET_REVISION, TARGET_REVISION, "ready"
    )
    return catalog, locator, root, _database(root, locator)


def _assert_bounded(error: BaseException, root: Path) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    assert str(root) not in str(error)
    assert "sqlite" not in str(error).lower()


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


def test_sqlite_catalog_satisfies_backend_neutral_conformance(tmp_path: Path) -> None:
    root = tmp_path / "root"
    assert_service_store_catalog_conformance(lambda: _catalog(root), backend="sqlite")


def test_allocate_and_absent_inspection_are_pure_and_side_effect_free(tmp_path: Path) -> None:
    root = tmp_path / "root"
    catalog = _catalog(root)
    first = catalog.allocate(UUID(int=1))
    assert first == _locator(1)
    assert catalog.allocate(UUID(int=1)) == first
    assert not root.exists()
    assert catalog.inspect_migration(first) == MigrationState(
        "service_store", 0, TARGET_REVISION, "uninitialised"
    )
    assert not root.exists()


def test_foreign_and_untrusted_locators_fail_before_location_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = SQLiteLocationPolicy(tmp_path / "root")
    catalog = SQLiteServiceStoreCatalog(policy)
    monkeypatch.setattr(
        policy,
        "acquire_service_store",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("location touched")),
    )
    foreign = ServiceStoreLocator("postgresql", _locator().namespace)
    for operation in (catalog.inspect_migration, catalog.migrate):
        with pytest.raises(StorageContractError):
            operation(foreign)
        with pytest.raises(StorageContractError):
            operation(object())  # type: ignore[arg-type]
    assert not (tmp_path / "root").exists()


def test_two_locators_are_isolated_and_registry_file_is_untouched(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    registry = root / "registry.sqlite"
    registry.write_bytes(b"synthetic-registry-sentinel")
    registry.chmod(0o600)
    before = (registry.read_bytes(), registry.stat().st_mtime_ns)
    catalog = _catalog(root)
    first, second = _locator(1), _locator(2)
    assert catalog.migrate(first).status == "ready"
    assert catalog.inspect_migration(second).status == "uninitialised"
    assert not _database(root, second).exists()
    assert catalog.migrate(second).status == "ready"
    assert catalog.inspect_migration(first).status == "ready"
    assert (registry.read_bytes(), registry.stat().st_mtime_ns) == before


def test_migration_is_idempotent_and_preserves_ledger_timestamp(tmp_path: Path) -> None:
    catalog, locator, _, database = _ready(tmp_path)
    with sqlite3.connect(database) as connection:
        before = connection.execute(
            f'SELECT migration_id,checksum,applied_at FROM "{MIGRATION}"'
        ).fetchone()
    assert catalog.migrate(locator).status == "ready"
    with sqlite3.connect(database) as connection:
        after = connection.execute(
            f'SELECT migration_id,checksum,applied_at FROM "{MIGRATION}"'
        ).fetchone()
    assert after == before


def test_connection_inspector_reports_ready_for_existing_hardened_connection(
    tmp_path: Path,
) -> None:
    catalog, locator, _, _ = _ready(tmp_path)
    anchored = catalog._location.acquire_service_store(
        locator.namespace,
        create=False,
        allow_journal=False,
    )
    assert anchored is not None

    connection = None
    try:
        connection = connect(
            anchored,
            "ro",
            query_only=True,
            purpose=SQLiteConnectionPurpose.POLICY_INSPECTION,
        )
        assert inspection_module.inspect_connection(
            connection,
            locator.namespace,
            integrity=True,
        ) == MigrationState("service_store", TARGET_REVISION, TARGET_REVISION, "ready")
    finally:
        if connection is not None:
            connection.close()
        anchored.close()


def test_revision_one_contains_only_exact_reserved_metadata(tmp_path: Path) -> None:
    _, _, _, database = _ready(tmp_path)
    with sqlite3.connect(database) as connection:
        objects = connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE substr(name,1,7) <> 'sqlite_'"
        ).fetchall()
    assert {name: sql for name, sql in objects} == EXPECTED_SQL


def test_future_plain_table_and_index_do_not_redefine_adapter_readiness(
    tmp_path: Path,
) -> None:
    catalog, locator, _, database = _ready(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE "future_entity" ("id" TEXT PRIMARY KEY NOT NULL) STRICT')
        connection.execute('CREATE INDEX "future_entity_id" ON "future_entity" ("id")')
    assert catalog.inspect_migration(locator) == MigrationState(
        "service_store", TARGET_REVISION, TARGET_REVISION, "ready"
    )


def test_reserved_prefix_text_in_future_table_and_index_ddl_remains_ready(
    tmp_path: Path,
) -> None:
    catalog, locator, _, database = _ready(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE "future_entity" (
                "id" TEXT PRIMARY KEY NOT NULL,
                "label" TEXT /* __aipcs_table_comment */ NOT NULL
                    DEFAULT '__aipcs_default'
                    CHECK ("label" <> '__aipcs_check')
            ) STRICT"""
        )
        connection.execute(
            """CREATE INDEX "future_entity_label" ON "future_entity" ("label")
                WHERE "label" <> '__aipcs_index' /* __aipcs_index_comment */"""
        )
        stored_sql = "\n".join(
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_schema WHERE name LIKE 'future_entity%'"
            )
        )
    assert RESERVED_PREFIX in stored_sql.lower()
    assert catalog.inspect_migration(locator) == MigrationState(
        "service_store", TARGET_REVISION, TARGET_REVISION, "ready"
    )


@pytest.mark.parametrize(
    "statement",
    [
        'CREATE TABLE "__aipcs_hostile" ("id" INTEGER) STRICT',
        'ALTER TABLE "__aipcs_service_store_migration" ADD COLUMN "drift" TEXT',
        'CREATE VIEW "future_view" AS SELECT singleton FROM "__aipcs_service_store_meta"',
        'CREATE TABLE "future_entity" ("id" INTEGER PRIMARY KEY) STRICT; '
        'CREATE TRIGGER "future_trigger" AFTER INSERT ON "future_entity" BEGIN SELECT 1; END',
        'CREATE TABLE "future_reference" ("meta" INTEGER REFERENCES '
        '"__aipcs_service_store_meta"("singleton")) STRICT',
        'CREATE INDEX "future_reserved_index" ON "__aipcs_service_store_meta" ("namespace")',
    ],
)
def test_reserved_objects_views_triggers_and_references_are_incompatible(
    tmp_path: Path, statement: str
) -> None:
    catalog, locator, _, database = _ready(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.executescript(statement)
    expected = MigrationState("service_store", TARGET_REVISION, TARGET_REVISION, "incompatible")
    assert catalog.inspect_migration(locator) == expected
    assert catalog.migrate(locator) == expected


def test_namespace_binding_rejects_cross_locator_database_copy(tmp_path: Path) -> None:
    catalog, first, root, source = _ready(tmp_path)
    second = _locator(2)
    destination = _database(root, second)
    shutil.copyfile(source, destination)
    destination.chmod(0o600)
    assert catalog.inspect_migration(first) == MigrationState(
        "service_store", TARGET_REVISION, TARGET_REVISION, "ready"
    )
    expected = MigrationState("service_store", 0, TARGET_REVISION, "incompatible")
    assert catalog.inspect_migration(second) == expected
    assert catalog.migrate(second) == expected


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("checksum", MigrationState("service_store", TARGET_REVISION, TARGET_REVISION, "incompatible")),
        ("newer", MigrationState("service_store", TARGET_REVISION + 1, TARGET_REVISION, "incompatible")),
        ("missing_history", MigrationState("service_store", TARGET_REVISION, TARGET_REVISION, "incompatible")),
        ("namespace", MigrationState("service_store", 0, TARGET_REVISION, "incompatible")),
        ("dirty", MigrationState("service_store", TARGET_REVISION, TARGET_REVISION, "incompatible")),
    ],
)
def test_migration_state_matrix_detects_history_and_binding_drift(
    tmp_path: Path, mutation: str, expected: MigrationState
) -> None:
    catalog, locator, _, database = _ready(tmp_path)
    with sqlite3.connect(database) as connection:
        if mutation == "checksum":
            replacement = "0" * 64 if CHECKSUM != "0" * 64 else "1" * 64
            connection.execute(f'UPDATE "{MIGRATION}" SET checksum=?', (replacement,))
        elif mutation == "newer":
            connection.execute(f'UPDATE "{META}" SET applied_revision=?', (TARGET_REVISION + 1,))
        elif mutation == "missing_history":
            connection.execute(f'DELETE FROM "{MIGRATION}"')
        elif mutation == "namespace":
            connection.execute(f'UPDATE "{META}" SET namespace=?', (_locator(2).namespace,))
        else:
            connection.execute(f'UPDATE "{META}" SET dirty=1')
    assert catalog.inspect_migration(locator) == expected
    assert catalog.migrate(locator) == expected


def test_empty_and_nonempty_unversioned_databases_classify_without_ddl(tmp_path: Path) -> None:
    root = tmp_path / "root"
    container = root / "service-stores"
    container.mkdir(mode=0o700, parents=True)
    root.chmod(0o700)
    empty_locator = _locator(1)
    empty = _database(root, empty_locator)
    descriptor = os.open(empty, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    catalog = _catalog(root)
    assert catalog.inspect_migration(empty_locator).status == "uninitialised"

    unversioned_locator = _locator(2)
    with sqlite3.connect(_database(root, unversioned_locator)) as connection:
        connection.execute('CREATE TABLE "foreign" ("id" INTEGER) STRICT')
    _database(root, unversioned_locator).chmod(0o600)
    assert catalog.inspect_migration(unversioned_locator).status == "incompatible"


def test_preparation_ddl_fault_rolls_back_without_durable_dirty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    catalog = _catalog(root)
    locator = _locator()
    with monkeypatch.context() as context:
        context.setattr(
            catalog_module,
            "_prepare_predecessor",
            lambda connection: connection.execute("NOT VALID SQL"),
        )
        with pytest.raises(StorageMigrationError) as captured:
            catalog.migrate(locator)
        _assert_bounded(captured.value, root)
    assert catalog.inspect_migration(locator).status == "uninitialised"


def test_precommit_readiness_fault_rolls_back_to_uninitialised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    catalog = _catalog(root)
    locator = _locator()
    with monkeypatch.context() as context:
        context.setattr(
            inspection_module,
            "inspect_connection",
            lambda connection, namespace, *, integrity: MigrationState(
                "service_store", TARGET_REVISION, TARGET_REVISION, "incompatible"
            ),
        )
        assert catalog.migrate(locator) == MigrationState(
            "service_store", TARGET_REVISION, TARGET_REVISION, "incompatible"
        )
    assert catalog.inspect_migration(locator).status == "uninitialised"


def test_postcommit_exception_is_bounded_and_idempotently_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    catalog = _catalog(root)
    locator = _locator()
    real_connect = catalog_module.connect

    class CommitAfterDurable:
        def __init__(self, wrapped: sqlite3.Connection) -> None:
            self.wrapped = wrapped
            self.commits = 0

        def __getattr__(self, name: str):
            return getattr(self.wrapped, name)

        def commit(self) -> None:
            self.wrapped.commit()
            self.commits += 1
            if self.commits == 2:
                raise sqlite3.OperationalError("secret commit uncertainty")

    with monkeypatch.context() as context:
        context.setattr(
            catalog_module,
            "connect",
            lambda *args, **kwargs: CommitAfterDurable(real_connect(*args, **kwargs)),
        )
        with pytest.raises(StorageMigrationError) as captured:
            catalog.migrate(locator)
        _assert_bounded(captured.value, root)
    assert catalog.inspect_migration(locator).status == "outdated"
    assert catalog.migrate(locator).status == "ready"


@pytest.mark.parametrize("kind", ["hardlink", "broad_mode", "fifo"])
def test_store_database_must_be_private_single_link_regular_file(tmp_path: Path, kind: str) -> None:
    catalog, locator, root, database = _ready(tmp_path)
    if kind == "hardlink":
        os.link(database, tmp_path / "second-link")
    elif kind == "broad_mode":
        database.chmod(0o644)
    else:
        database.unlink()
        os.mkfifo(database, 0o600)
    with pytest.raises(StorageUnavailable) as captured:
        catalog.inspect_migration(locator)
    _assert_bounded(captured.value, root)


@pytest.mark.parametrize("kind", ["symlink", "broad_mode"])
def test_store_container_must_be_private_owned_directory(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    container = root / "service-stores"
    if kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        container.symlink_to(outside, target_is_directory=True)
    else:
        container.mkdir(mode=0o755)
    catalog = _catalog(root)
    with pytest.raises(StorageUnavailable) as captured:
        catalog.inspect_migration(_locator())
    _assert_bounded(captured.value, root)


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_service_store_unsafe_sidecars_fail_closed(
    tmp_path: Path, suffix: str
) -> None:
    catalog, locator, root, database = _ready(tmp_path)
    sidecar = Path(str(database) + suffix)
    if sidecar.exists():
        sidecar.chmod(0o644)
    else:
        descriptor = os.open(sidecar, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(descriptor, b"synthetic-sidecar")
        os.close(descriptor)
    with pytest.raises(StorageUnavailable) as inspected:
        catalog.inspect_migration(locator)
    with pytest.raises(StorageUnavailable) as migrated:
        catalog.migrate(locator)
    assert type(inspected.value) is StorageUnavailable
    assert type(migrated.value) is StorageUnavailable
    assert sidecar.exists()
    assert str(root) not in repr(catalog)


def test_safe_rollback_journal_has_narrow_transient_inspection_signal(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    container = root / "service-stores"
    container.mkdir(mode=0o700)
    locator = _locator()
    database = _database(root, locator)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=0")
    database.chmod(0o600)
    journal = Path(str(database) + "-journal")
    journal.write_bytes(b"synthetic-journal")
    journal.chmod(0o600)
    before = database.read_bytes()

    with pytest.raises(StorageTransientJournal) as captured:
        _catalog(root).inspect_migration(locator)

    _assert_bounded(captured.value, root)
    assert journal.read_bytes() == b"synthetic-journal"
    assert database.read_bytes() == before


@pytest.mark.parametrize("main_contents", [b"", b"not-a-sqlite-database"])
def test_journal_with_non_sqlite_main_is_generic_unavailable(
    tmp_path: Path, main_contents: bytes
) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    container = root / "service-stores"
    container.mkdir(mode=0o700)
    locator = _locator()
    database = _database(root, locator)
    database.write_bytes(main_contents)
    database.chmod(0o600)
    journal = Path(str(database) + "-journal")
    journal.write_bytes(b"synthetic-journal")
    journal.chmod(0o600)

    with pytest.raises(StorageUnavailable) as captured:
        _catalog(root).inspect_migration(locator)

    assert type(captured.value) is StorageUnavailable
    assert database.read_bytes() == main_contents
    assert journal.read_bytes() == b"synthetic-journal"


def test_ready_wal_header_without_sidecars_is_inspectable(tmp_path: Path) -> None:
    catalog, locator, _, database = _ready(tmp_path)
    _leave_wal_header_without_sidecars(database, malformed=False)
    assert catalog.inspect_migration(locator).status == "ready"
    assert catalog.migrate(locator).status == "ready"


def test_malformed_wal_header_fails_bounded(tmp_path: Path) -> None:
    catalog, locator, _, database = _ready(tmp_path)
    _leave_wal_header_without_sidecars(database, malformed=True)
    with pytest.raises(StorageUnavailable):
        catalog.inspect_migration(locator)
    with pytest.raises(StorageUnavailable):
        catalog.migrate(locator)


def test_observable_database_replacement_is_detected_at_identity_checkpoint(
    tmp_path: Path,
) -> None:
    catalog, locator, root, database = _ready(tmp_path)
    policy = catalog._location
    anchored = policy.acquire_service_store(locator.namespace, create=False, allow_journal=False)
    assert anchored is not None
    original = database.with_suffix(".original")
    database.rename(original)
    descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        with pytest.raises(StorageUnavailable):
            anchored.verify_database_identity()
    finally:
        anchored.close()
    assert database.stat().st_size == 0
    assert original.stat().st_size > 0
    with pytest.raises(StorageUnavailable):
        catalog.inspect_migration(locator)
    assert str(root) not in repr(catalog)


def test_observable_replacement_before_connect_never_mutates_substitute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    catalog = _catalog(root)
    locator = _locator()
    database = _database(root, locator)
    original = database.with_suffix(".connect-original")
    real_connect = catalog_module.connect
    swapped = False

    def replace_then_connect(location, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            database.rename(original)
            descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            swapped = True
        return real_connect(location, *args, **kwargs)

    monkeypatch.setattr(catalog_module, "connect", replace_then_connect)
    with pytest.raises(StorageUnavailable) as captured:
        catalog.migrate(locator)
    _assert_bounded(captured.value, root)
    assert database.stat().st_size == 0
    assert original.stat().st_size == 0


def test_observable_replacement_at_precommit_does_not_mutate_substitute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    catalog = _catalog(root)
    locator = _locator()
    database = _database(root, locator)
    original = database.with_suffix(".precommit-original")
    real_inspect = inspection_module.inspect_connection
    swapped = False

    def replace_then_inspect(connection, namespace, *, integrity):
        nonlocal swapped
        if not swapped:
            database.rename(original)
            descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            swapped = True
        return real_inspect(connection, namespace, integrity=integrity)

    monkeypatch.setattr(inspection_module, "inspect_connection", replace_then_inspect)
    with pytest.raises(StorageUnavailable) as captured:
        catalog.migrate(locator)
    _assert_bounded(captured.value, root)
    assert database.stat().st_size == 0
    with sqlite3.connect(original) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE substr(name,1,7) <> 'sqlite_'"
            )
        }
        assert names in (set(), {META, MIGRATION})
        if names:
            assert connection.execute(f'SELECT dirty FROM "{META}"').fetchone() == (0,)
