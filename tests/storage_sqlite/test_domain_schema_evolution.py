"""SYNTHETIC_FIXTURE. Adversarial V1-07C additive SQLite evolution proof."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest
from fixtures import entity, valid_manifest

from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.relational import RelationalTransition, classify_transition, compile_manifest
from aipcs_mcp.storage import (
    DomainSchemaState,
    ServiceStoreLocator,
    StorageContractError,
    StorageMigrationError,
    StorageUnavailable,
)
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteServiceStoreCatalog
from aipcs_mcp.storage.sqlite import domain_schema as domain_module
from aipcs_mcp.storage.sqlite.domain_schema import SQLiteDomainSchemaStore
from aipcs_mcp.storage.sqlite.domain_schema_layout import build_domain_schema_layout


def _locator(value: int = 1) -> ServiceStoreLocator:
    return ServiceStoreLocator.for_service("sqlite", UUID(int=value))


def _database(root: Path, locator: ServiceStoreLocator) -> Path:
    return root / "service-stores" / f"{locator.namespace}.sqlite"


def _manifest(payload: dict[str, object]) -> ManifestV2:
    return ManifestV2.model_validate(payload)


def _advance(payload: dict[str, object], version: int, operation: str) -> dict[str, object]:
    result = deepcopy(payload)
    result["schema_version"] = version
    history = list(result["migration_history"])
    history.append(
        {
            "from_schema_version": version - 1,
            "to_schema_version": version,
            "operations": [operation],
        }
    )
    result["migration_history"] = history
    return result


def _foundation(root: Path, locator: ServiceStoreLocator) -> None:
    assert SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root)).migrate(locator).status == "ready"


def _store(root: Path) -> SQLiteDomainSchemaStore:
    return SQLiteDomainSchemaStore(SQLiteLocationPolicy(root))


def _v1() -> dict[str, object]:
    payload = valid_manifest()
    payload["entities"].append(entity("note", [{"name": "body", "type": "string"}]))
    return payload


def _v2_additive(payload: dict[str, object]) -> dict[str, object]:
    result = _advance(payload, 2, "add task and nullable project tag")
    result["entities"][0]["attributes"].append({"name": "tag", "type": "string"})
    result["entities"].append(
        entity(
            "task",
            [
                {"name": "project_id", "type": "uuid"},
                {"name": "title", "type": "string", "required": True},
            ],
        )
    )
    result["relationships"].append(
        {
            "name": "task_project_fk",
            "from": {"entity": "task", "field": "project_id"},
            "to": {"entity": "project", "field": "id"},
            "on_delete": "restrict",
        }
    )
    result["indices"].extend(
        [
            {"name": "project_tag_idx", "entity": "project", "fields": ["tag"]},
            {
                "name": "task_project_title_unique_idx",
                "entity": "task",
                "fields": ["project_id", "title"],
                "unique": True,
            },
        ]
    )
    return result


def _v3_additive(payload: dict[str, object]) -> dict[str, object]:
    result = _advance(payload, 3, "add nullable task estimate")
    task = next(item for item in result["entities"] if item["name"] == "task")
    task["attributes"].append({"name": "estimate", "type": "integer"})
    result["indices"].append(
        {"name": "task_estimate_idx", "entity": "task", "fields": ["estimate"]}
    )
    return result


def _assert_bounded(error: BaseException, root: Path) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    assert str(root) not in str(error)
    assert "sqlite" not in str(error).lower()


def _structural_copy(transition: RelationalTransition) -> RelationalTransition:
    copied = object.__new__(RelationalTransition)
    object.__setattr__(copied, "current", transition.current)
    object.__setattr__(copied, "target", transition.target)
    object.__setattr__(copied, "additions", transition.additions)
    object.__setattr__(copied, "metadata_changes", transition.metadata_changes)
    return copied


def test_additive_evolution_creates_new_entity_fields_indices_and_preserves_rows(tmp_path: Path) -> None:
    root = tmp_path / "root"
    locator = _locator()
    current_payload = _v1()
    target_payload = _v2_additive(current_payload)
    current = compile_manifest(_manifest(current_payload))
    target = compile_manifest(_manifest(target_payload))
    transition = classify_transition(_manifest(current_payload), _manifest(target_payload))
    _foundation(root, locator)
    store = _store(root)
    assert store.materialise(locator, current) == DomainSchemaState("ready")
    database = _database(root, locator)
    with sqlite3.connect(database) as connection:
        connection.execute(
            'INSERT INTO "project" ("id","owner_id","created_at","updated_at","created_via",'
            '"record_version","title") VALUES (?,?,?,?,?,?,?)',
            ("project-1", "owner", "t", "t", "test", 1, "Before"),
        )

    assert store.evolve(locator, transition) == DomainSchemaState("ready")
    assert store.inspect(locator, target) == DomainSchemaState("ready")
    assert store.inspect(locator, current) == DomainSchemaState("incompatible")
    with sqlite3.connect(database, isolation_level=None) as connection:
        assert connection.execute('SELECT "title","tag" FROM "project"').fetchall() == [
            ("Before", None)
        ]
        assert connection.execute('SELECT COUNT(*) FROM "task"').fetchone() == (0,)
        assert connection.execute('PRAGMA foreign_key_list("task")').fetchone()[2:7] == (
            "project",
            "project_id",
            "id",
            "RESTRICT",
            "RESTRICT",
        )
        assert {
            row[1] for row in connection.execute('PRAGMA index_list("project")')
        } >= {"project_tag_idx"}
        assert {
            row[1] for row in connection.execute('PRAGMA index_list("task")')
        } >= {"task_project_title_unique_idx"}
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                'INSERT INTO "task" ("id","owner_id","created_at","updated_at","created_via",'
                '"record_version","title","project_id") VALUES (?,?,?,?,?,?,?,?)',
                ("task-1", "owner", "t", "t", "test", 1, "Bad", "missing"),
            )


def test_metadata_only_and_sequential_transitions_are_exact_retries(tmp_path: Path) -> None:
    root = tmp_path / "root"
    locator = _locator()
    v1 = _v1()
    metadata_v2 = _advance(v1, 2, "metadata only")
    metadata_v2["entities"][0]["description"] = "changed only in metadata"
    v2 = _v2_additive(v1)
    v3 = _v3_additive(v2)
    _foundation(root, locator)
    store = _store(root)
    current = compile_manifest(_manifest(v1))
    assert store.materialise(locator, current) == DomainSchemaState("ready")
    database = _database(root, locator)
    before_metadata = hashlib.sha256(database.read_bytes()).digest()
    metadata_transition = classify_transition(_manifest(v1), _manifest(metadata_v2))
    metadata_target = compile_manifest(_manifest(metadata_v2))
    assert store.evolve(locator, metadata_transition) == DomainSchemaState("ready")
    assert store.inspect(locator, current) == DomainSchemaState("ready")
    assert store.inspect(locator, metadata_target) == DomainSchemaState("ready")
    assert hashlib.sha256(database.read_bytes()).digest() == before_metadata

    transition_12 = classify_transition(_manifest(v1), _manifest(v2))
    transition_23 = classify_transition(_manifest(v2), _manifest(v3))
    target_v2 = compile_manifest(_manifest(v2))
    target_v3 = compile_manifest(_manifest(v3))
    assert store.evolve(locator, transition_12) == DomainSchemaState("ready")
    after_v2 = hashlib.sha256(database.read_bytes()).digest()
    assert store.evolve(locator, transition_12) == DomainSchemaState("ready")
    assert hashlib.sha256(database.read_bytes()).digest() == after_v2
    assert store.inspect(locator, target_v2) == DomainSchemaState("ready")
    assert store.evolve(locator, transition_23) == DomainSchemaState("ready")
    assert store.inspect(locator, target_v3) == DomainSchemaState("ready")


def test_partial_target_is_refused_without_repair_or_row_loss(tmp_path: Path) -> None:
    root = tmp_path / "root"
    locator = _locator()
    v1 = _v1()
    v2 = _v2_additive(v1)
    current = compile_manifest(_manifest(v1))
    transition = classify_transition(_manifest(v1), _manifest(v2))
    _foundation(root, locator)
    store = _store(root)
    assert store.materialise(locator, current).status == "ready"
    database = _database(root, locator)
    with sqlite3.connect(database) as connection:
        connection.execute('ALTER TABLE "project" ADD COLUMN "tag" TEXT')
        connection.execute(
            'INSERT INTO "project" ("id","owner_id","created_at","updated_at","created_via",'
            '"record_version","title") VALUES (?,?,?,?,?,?,?)',
            ("project-1", "owner", "t", "t", "test", 1, "Preserve"),
        )
    before = hashlib.sha256(database.read_bytes()).digest()

    assert store.evolve(locator, transition) == DomainSchemaState("incompatible")
    assert hashlib.sha256(database.read_bytes()).digest() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT "title","tag" FROM "project"').fetchall() == [
            ("Preserve", None)
        ]
        assert connection.execute("SELECT name FROM sqlite_schema WHERE name='task'").fetchone() is None


def test_transition_validation_and_detachment_precede_location_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    locator = _locator()
    v1, v2 = _v1(), _v2_additive(_v1())
    transition = classify_transition(_manifest(v1), _manifest(v2))
    policy = SQLiteLocationPolicy(root)
    store = SQLiteDomainSchemaStore(policy)
    monkeypatch.setattr(
        policy,
        "acquire_service_store",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("location touched")),
    )
    malformed = _structural_copy(transition)
    object.__setattr__(malformed, "additions", object())
    with pytest.raises(StorageContractError):
        store.evolve(locator, malformed)
    with pytest.raises(StorageContractError):
        store.evolve(object(), transition)  # type: ignore[arg-type]
    assert not root.exists()


def test_evolution_admission_detaches_a_transition_before_policy_mutation(tmp_path: Path) -> None:
    root = tmp_path / "root"
    locator = _locator()
    v1, v2 = _v1(), _v2_additive(_v1())
    transition = classify_transition(_manifest(v1), _manifest(v2))
    target = compile_manifest(_manifest(v2))
    _foundation(root, locator)
    mutate = False

    class MutatingPolicy(SQLiteLocationPolicy):
        def acquire_service_store(self, *args: object, **kwargs: object):
            if mutate:
                object.__setattr__(transition.target, "entities", (object(),))
            return super().acquire_service_store(*args, **kwargs)

    store = SQLiteDomainSchemaStore(MutatingPolicy(root))
    assert store.materialise(locator, transition.current) == DomainSchemaState("ready")
    mutate = True
    assert store.evolve(locator, transition) == DomainSchemaState("ready")
    assert _store(root).inspect(locator, target) == DomainSchemaState("ready")


@pytest.mark.parametrize("fault_index", range(4))
def test_evolve_begins_exclusive_then_rolls_back_each_transition_ddl_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_index: int
) -> None:
    root = tmp_path / "root"
    locator = _locator()
    v1, v2 = _v1(), _v2_additive(_v1())
    current = compile_manifest(_manifest(v1))
    transition = classify_transition(_manifest(v1), _manifest(v2))
    _foundation(root, locator)
    store = _store(root)
    assert store.materialise(locator, current) == DomainSchemaState("ready")
    database = _database(root, locator)
    with sqlite3.connect(database) as connection:
        connection.execute(
            'INSERT INTO "project" ("id","owner_id","created_at","updated_at","created_via",'
            '"record_version","title") VALUES (?,?,?,?,?,?,?)',
            ("project-1", "owner", "t", "t", "test", 1, "Preserve"),
        )
    real_connect = domain_module.connect
    statements: list[str] = []
    ddl_seen = 0

    class FaultingConnection:
        def __init__(self, wrapped: sqlite3.Connection) -> None:
            self._wrapped = wrapped

        def __getattr__(self, name: str) -> object:
            return getattr(self._wrapped, name)

        def execute(self, sql: str, *args: object, **kwargs: object):
            nonlocal ddl_seen
            statements.append(sql)
            if sql.startswith(
                ("ALTER TABLE", "CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX")
            ):
                if ddl_seen == fault_index:
                    raise sqlite3.OperationalError("synthetic evolution ddl fault")
                ddl_seen += 1
            return self._wrapped.execute(sql, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(
            domain_module,
            "connect",
            lambda *args, **kwargs: FaultingConnection(real_connect(*args, **kwargs)),
        )
        with pytest.raises(StorageMigrationError) as captured:
            store.evolve(locator, transition)
        _assert_bounded(captured.value, root)
    assert statements[0] == "BEGIN EXCLUSIVE"
    assert _store(root).inspect(locator, current) == DomainSchemaState("ready")
    assert _store(root).inspect(locator, transition.target) == DomainSchemaState("incompatible")
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT "title" FROM "project"').fetchall() == [("Preserve",)]


def test_postddl_target_mismatch_rolls_back_to_exact_current_with_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    locator = _locator()
    v1, v2 = _v1(), _v2_additive(_v1())
    current = compile_manifest(_manifest(v1))
    transition = classify_transition(_manifest(v1), _manifest(v2))
    _foundation(root, locator)
    store = _store(root)
    assert store.materialise(locator, current) == DomainSchemaState("ready")
    database = _database(root, locator)
    with sqlite3.connect(database) as connection:
        connection.execute(
            'INSERT INTO "project" ("id","owner_id","created_at","updated_at","created_via",'
            '"record_version","title") VALUES (?,?,?,?,?,?,?)',
            ("project-1", "owner", "t", "t", "test", 1, "Preserve"),
        )
    real_inspect = domain_module._inspect_domain
    inspections = 0

    def mismatch_after_ddl(connection: sqlite3.Connection, layout: object) -> DomainSchemaState:
        nonlocal inspections
        inspections += 1
        if inspections == 3:
            return DomainSchemaState("incompatible")
        return real_inspect(connection, layout)  # type: ignore[arg-type]

    with monkeypatch.context() as context:
        context.setattr(domain_module, "_inspect_domain", mismatch_after_ddl)
        with pytest.raises(StorageMigrationError) as captured:
            store.evolve(locator, transition)
        _assert_bounded(captured.value, root)
    assert inspections == 3
    assert _store(root).inspect(locator, current) == DomainSchemaState("ready")
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT "title" FROM "project"').fetchall() == [("Preserve",)]


def test_complete_domain_absence_is_not_an_evolvable_current_state(tmp_path: Path) -> None:
    root = tmp_path / "root"
    locator = _locator()
    v1, v2 = _v1(), _v2_additive(_v1())
    current = compile_manifest(_manifest(v1))
    transition = classify_transition(_manifest(v1), _manifest(v2))
    _foundation(root, locator)
    store = _store(root)
    assert store.materialise(locator, current) == DomainSchemaState("ready")
    database = _database(root, locator)
    with sqlite3.connect(database) as connection:
        connection.execute('DROP TABLE "note"')
        connection.execute('DROP TABLE "project"')
    before = hashlib.sha256(database.read_bytes()).digest()

    assert store.inspect(locator, current) == DomainSchemaState("unmaterialised")
    assert store.evolve(locator, transition) == DomainSchemaState("incompatible")
    assert hashlib.sha256(database.read_bytes()).digest() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT name FROM sqlite_schema WHERE name='task'").fetchone() is None


def test_evolved_table_raw_sql_differs_from_fresh_canonical_but_inspects_target_ready(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    locator = _locator()
    v1, v2 = _v1(), _v2_additive(_v1())
    current = compile_manifest(_manifest(v1))
    target = compile_manifest(_manifest(v2))
    transition = classify_transition(_manifest(v1), _manifest(v2))
    _foundation(root, locator)
    store = _store(root)
    assert store.materialise(locator, current) == DomainSchemaState("ready")
    assert store.evolve(locator, transition) == DomainSchemaState("ready")
    canonical = dict(build_domain_schema_layout(target).table_sql)["project"]
    with sqlite3.connect(_database(root, locator)) as connection:
        evolved = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='project'"
        ).fetchone()[0]
    assert evolved != canonical
    assert store.inspect(locator, target) == DomainSchemaState("ready")


@pytest.mark.parametrize("fault", ["commit", "close"])
def test_evolution_postcommit_uncertainty_is_bounded_and_restart_retry_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    root = tmp_path / "root"
    locator = _locator()
    v1, v2 = _v1(), _v2_additive(_v1())
    current = compile_manifest(_manifest(v1))
    target = compile_manifest(_manifest(v2))
    transition = classify_transition(_manifest(v1), _manifest(v2))
    _foundation(root, locator)
    store = _store(root)
    assert store.materialise(locator, current) == DomainSchemaState("ready")
    real_connect = domain_module.connect

    class CommitOrCloseAfterDurable:
        def __init__(self, wrapped: sqlite3.Connection) -> None:
            self._wrapped = wrapped

        def __getattr__(self, name: str) -> object:
            return getattr(self._wrapped, name)

        def commit(self) -> None:
            self._wrapped.commit()
            if fault == "commit":
                raise sqlite3.OperationalError("secret durable commit uncertainty")

        def close(self) -> None:
            self._wrapped.close()
            if fault == "close":
                raise sqlite3.OperationalError("secret durable close uncertainty")

    with monkeypatch.context() as context:
        context.setattr(
            domain_module,
            "connect",
            lambda *args, **kwargs: CommitOrCloseAfterDurable(real_connect(*args, **kwargs)),
        )
        with pytest.raises(StorageMigrationError) as captured:
            store.evolve(locator, transition)
        _assert_bounded(captured.value, root)
    recovered = _store(root)
    assert recovered.inspect(locator, target) == DomainSchemaState("ready")
    assert recovered.evolve(locator, transition) == DomainSchemaState("ready")


def test_postcommit_reacquisition_unavailability_has_precedence_over_retryable_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    locator = _locator()
    v1, v2 = _v1(), _v2_additive(_v1())
    current = compile_manifest(_manifest(v1))
    target = compile_manifest(_manifest(v2))
    transition = classify_transition(_manifest(v1), _manifest(v2))
    _foundation(root, locator)
    assert _store(root).materialise(locator, current) == DomainSchemaState("ready")
    policy = SQLiteLocationPolicy(root)
    acquire = policy.acquire_service_store
    calls = 0

    def fail_final_acquire(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StorageUnavailable()
        return acquire(*args, **kwargs)

    monkeypatch.setattr(policy, "acquire_service_store", fail_final_acquire)
    with pytest.raises(StorageUnavailable) as captured:
        SQLiteDomainSchemaStore(policy).evolve(locator, transition)
    _assert_bounded(captured.value, root)
    assert calls == 2
    assert _store(root).inspect(locator, target) == DomainSchemaState("ready")


@pytest.mark.parametrize(
    "replacement_check",
    [2, 3],
    ids=["after_locked_current_before_ddl", "after_postddl_target_before_commit"],
)
def test_identity_replacement_at_each_precommit_checkpoint_never_mutates_substitute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement_check: int
) -> None:
    root = tmp_path / "root"
    locator = _locator()
    v1, v2 = _v1(), _v2_additive(_v1())
    current = compile_manifest(_manifest(v1))
    transition = classify_transition(_manifest(v1), _manifest(v2))
    _foundation(root, locator)
    store = _store(root)
    assert store.materialise(locator, current) == DomainSchemaState("ready")
    database = _database(root, locator)
    original = database.with_suffix(".evolution-original")
    real_inspect = domain_module._inspect_domain
    inspections = 0

    def replace_after_current_check(connection: sqlite3.Connection, layout: object) -> DomainSchemaState:
        nonlocal inspections
        inspections += 1
        state = real_inspect(connection, layout)  # type: ignore[arg-type]
        if inspections == replacement_check:
            database.rename(original)
            descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        return state

    with monkeypatch.context() as context:
        context.setattr(domain_module, "_inspect_domain", replace_after_current_check)
        with pytest.raises(StorageUnavailable) as captured:
            store.evolve(locator, transition)
        _assert_bounded(captured.value, root)
    assert database.stat().st_size == 0
    with sqlite3.connect(original) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND substr(name,1,7) <> 'sqlite_'"
            )
        }
    assert tables == {"__aipcs_service_store_meta", "__aipcs_service_store_migration", "note", "project"}


def test_target_ready_noop_identity_replacement_is_unavailable_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    locator = _locator()
    v1, v2 = _v1(), _v2_additive(_v1())
    current = compile_manifest(_manifest(v1))
    transition = classify_transition(_manifest(v1), _manifest(v2))
    _foundation(root, locator)
    store = _store(root)
    assert store.materialise(locator, current) == DomainSchemaState("ready")
    assert store.evolve(locator, transition) == DomainSchemaState("ready")
    database = _database(root, locator)
    original = database.with_suffix(".target-ready-original")
    real_inspect = domain_module._inspect_domain
    inspections = 0

    def replace_after_target_ready(connection: sqlite3.Connection, layout: object) -> DomainSchemaState:
        nonlocal inspections
        inspections += 1
        state = real_inspect(connection, layout)  # type: ignore[arg-type]
        if inspections == 1:
            database.rename(original)
            descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        return state

    with monkeypatch.context() as context:
        context.setattr(domain_module, "_inspect_domain", replace_after_target_ready)
        with pytest.raises(StorageUnavailable) as captured:
            store.evolve(locator, transition)
        _assert_bounded(captured.value, root)
    assert database.stat().st_size == 0
    with sqlite3.connect(original) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND substr(name,1,7) <> 'sqlite_'"
            )
        }
    assert tables == {
        "__aipcs_service_store_meta",
        "__aipcs_service_store_migration",
        "note",
        "project",
        "task",
    }


def test_neither_exact_identity_replacement_is_unavailable_not_incompatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    locator = _locator()
    v1, v2 = _v1(), _v2_additive(_v1())
    current = compile_manifest(_manifest(v1))
    transition = classify_transition(_manifest(v1), _manifest(v2))
    _foundation(root, locator)
    store = _store(root)
    assert store.materialise(locator, current) == DomainSchemaState("ready")
    database = _database(root, locator)
    with sqlite3.connect(database) as connection:
        connection.execute('ALTER TABLE "project" ADD COLUMN "tag" TEXT')
    original = database.with_suffix(".incompatible-original")
    real_inspect = domain_module._inspect_domain
    inspections = 0

    def replace_after_neither_exact(connection: sqlite3.Connection, layout: object) -> DomainSchemaState:
        nonlocal inspections
        inspections += 1
        state = real_inspect(connection, layout)  # type: ignore[arg-type]
        if inspections == 2:
            database.rename(original)
            descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        return state

    with monkeypatch.context() as context:
        context.setattr(domain_module, "_inspect_domain", replace_after_neither_exact)
        with pytest.raises(StorageUnavailable) as captured:
            store.evolve(locator, transition)
        _assert_bounded(captured.value, root)
    assert database.stat().st_size == 0
    with sqlite3.connect(original) as connection:
        assert connection.execute("SELECT name FROM sqlite_schema WHERE name='task'").fetchone() is None
        assert [row[1] for row in connection.execute('PRAGMA table_xinfo("project")')][-1] == "tag"
