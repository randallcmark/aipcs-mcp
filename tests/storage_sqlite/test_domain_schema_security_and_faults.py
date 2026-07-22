"""SYNTHETIC_FIXTURE. Adversarial V1-07B domain-schema boundary tests."""

from __future__ import annotations

import ast
import os
import sqlite3
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from inspect import Parameter, signature
from pathlib import Path
from typing import get_type_hints
from uuid import UUID

import pytest
from fixtures import entity, valid_manifest

from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.relational import (
    RelationalSpecification,
    RelationalTransition,
    classify_transition,
    compile_manifest,
)
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


def _locator(value: int = 1) -> ServiceStoreLocator:
    return ServiceStoreLocator.for_service("sqlite", UUID(int=value))


def _database(root: Path, locator: ServiceStoreLocator) -> Path:
    return root / "service-stores" / f"{locator.namespace}.sqlite"


def _manifest(payload: dict[str, object]) -> ManifestV2:
    return ManifestV2.model_validate(payload)


def _specification(*, relationship: bool = False) -> RelationalSpecification:
    payload = valid_manifest()
    payload["entities"].append(entity("note", [{"name": "body", "type": "string"}]))
    payload["indices"].append(
        {"name": "project_title_idx", "entity": "project", "fields": ["title"]}
    )
    if relationship:
        payload["entities"][0]["attributes"].append({"name": "parent_id", "type": "uuid"})
        payload["relationships"] = [
            {
                "name": "project_parent_fk",
                "from": {"entity": "project", "field": "parent_id"},
                "to": {"entity": "project", "field": "id"},
                "on_delete": "restrict",
            }
        ]
    return compile_manifest(_manifest(payload))


def _transition() -> RelationalTransition:
    current = valid_manifest()
    target = deepcopy(current)
    target["schema_version"] = 2
    target["migration_history"] = [
        {"from_schema_version": 1, "to_schema_version": 2, "operations": ["metadata only"]}
    ]
    target["indices"].append(
        {"name": "project_title_idx", "entity": "project", "fields": ["title"]}
    )
    return classify_transition(_manifest(current), _manifest(target))


def _foundation(root: Path, locator: ServiceStoreLocator) -> None:
    assert SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root)).migrate(locator).status == "ready"


def _ready(
    tmp_path: Path,
) -> tuple[SQLiteDomainSchemaStore, ServiceStoreLocator, Path, Path, RelationalSpecification]:
    root = tmp_path / "root"
    locator = _locator()
    _foundation(root, locator)
    specification = _specification()
    store = SQLiteDomainSchemaStore(SQLiteLocationPolicy(root))
    return store, locator, root, _database(root, locator), specification


def _assert_bounded(error: BaseException, root: Path) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    assert str(root) not in str(error)
    assert "sqlite" not in str(error).lower()


def _forged_specification(specification: RelationalSpecification) -> RelationalSpecification:
    forged = object.__new__(RelationalSpecification)
    object.__setattr__(forged, "schema_version", specification.schema_version)
    object.__setattr__(forged, "entities", (object(),))
    object.__setattr__(forged, "relationships", specification.relationships)
    object.__setattr__(forged, "indices", specification.indices)
    return forged


def _structural_transition_copy(transition: RelationalTransition) -> RelationalTransition:
    copied = object.__new__(RelationalTransition)
    object.__setattr__(copied, "current", transition.current)
    object.__setattr__(copied, "target", transition.target)
    object.__setattr__(copied, "additions", transition.additions)
    object.__setattr__(copied, "metadata_changes", transition.metadata_changes)
    return copied


def test_private_adapter_has_exact_protocol_surface_without_runtime_composition() -> None:
    expected = {
        "inspect": (
            ["self", "locator", "specification"],
            {
                "locator": ServiceStoreLocator,
                "specification": RelationalSpecification,
                "return": DomainSchemaState,
            },
        ),
        "materialise": (
            ["self", "locator", "specification"],
            {
                "locator": ServiceStoreLocator,
                "specification": RelationalSpecification,
                "return": DomainSchemaState,
            },
        ),
        "evolve": (
            ["self", "locator", "transition"],
            {
                "locator": ServiceStoreLocator,
                "transition": RelationalTransition,
                "return": DomainSchemaState,
            },
        ),
    }
    for name, (names, hints) in expected.items():
        method = getattr(SQLiteDomainSchemaStore, name)
        parameters = list(signature(method).parameters.values())
        assert [parameter.name for parameter in parameters] == names
        assert all(parameter.kind is Parameter.POSITIONAL_OR_KEYWORD for parameter in parameters)
        assert all(parameter.default is Parameter.empty for parameter in parameters)
        assert get_type_hints(method) == hints

    import aipcs_mcp.storage.sqlite as sqlite_package

    assert "SQLiteDomainSchemaStore" not in sqlite_package.__all__
    imports = {
        alias.name
        for node in ast.walk(ast.parse(Path(domain_module.__file__).read_text()))
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(ast.parse(Path(domain_module.__file__).read_text()))
        if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        name.startswith(("aipcs_mcp.application", "aipcs_mcp.server", "aipcs_mcp.config"))
        for name in imports
    )


def test_malformed_subclassed_and_forged_values_fail_before_location_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = SQLiteLocationPolicy(tmp_path / "root")
    store = SQLiteDomainSchemaStore(policy)
    locator = _locator()
    specification = _specification()
    transition = _transition()

    class LocatorSubclass(ServiceStoreLocator):
        pass

    class SpecificationSubclass(RelationalSpecification):
        pass

    subclass_locator = LocatorSubclass(locator.backend, locator.namespace)
    subclass_specification = SpecificationSubclass(
        specification.schema_version,
        specification.entities,
        specification.relationships,
        specification.indices,
    )
    forged_locator = object.__new__(ServiceStoreLocator)
    object.__setattr__(forged_locator, "backend", "sqlite")
    object.__setattr__(forged_locator, "namespace", True)
    monkeypatch.setattr(
        policy,
        "acquire_service_store",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("location touched")),
    )

    for bad_locator in (object(), subclass_locator, forged_locator):
        for operation in (store.inspect, store.materialise):
            with pytest.raises(StorageContractError):
                operation(bad_locator, specification)  # type: ignore[arg-type]
    for bad_specification in (
        object(),
        subclass_specification,
        _forged_specification(specification),
    ):
        for operation in (store.inspect, store.materialise):
            with pytest.raises(StorageContractError):
                operation(locator, bad_specification)  # type: ignore[arg-type]
    with pytest.raises(StorageContractError):
        store.evolve(object(), transition)  # type: ignore[arg-type]
    with pytest.raises(StorageContractError):
        store.evolve(locator, object())  # type: ignore[arg-type]
    malformed_transition = _structural_transition_copy(transition)
    object.__setattr__(malformed_transition, "additions", object())
    with pytest.raises(StorageContractError):
        store.evolve(locator, malformed_transition)
    with pytest.raises(StorageMigrationError):
        store.evolve(locator, _structural_transition_copy(transition))
    assert not (tmp_path / "root").exists()


def test_absent_and_empty_foundations_are_safe_migration_preconditions(tmp_path: Path) -> None:
    root = tmp_path / "root"
    locator = _locator()
    store = SQLiteDomainSchemaStore(SQLiteLocationPolicy(root))
    specification = _specification()
    for operation in (store.inspect, store.materialise):
        with pytest.raises(StorageMigrationError) as captured:
            operation(locator, specification)
        _assert_bounded(captured.value, root)
    assert not root.exists()

    container = root / "service-stores"
    container.mkdir(mode=0o700, parents=True)
    root.chmod(0o700)
    descriptor = os.open(_database(root, locator), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    for operation in (store.inspect, store.materialise):
        with pytest.raises(StorageMigrationError) as captured:
            operation(locator, specification)
        _assert_bounded(captured.value, root)


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_domain_store_refuses_sidecars_without_recovery_or_disclosure(
    tmp_path: Path, suffix: str
) -> None:
    store, locator, root, database, specification = _ready(tmp_path)
    sidecar = Path(str(database) + suffix)
    descriptor = os.open(sidecar, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(descriptor, b"untrusted-sidecar")
    os.close(descriptor)

    for operation in (store.inspect, store.materialise):
        with pytest.raises(StorageUnavailable) as captured:
            operation(locator, specification)
        _assert_bounded(captured.value, root)
    assert sidecar.exists()
    assert repr(store) == "SQLiteDomainSchemaStore(<redacted>)"


def test_domain_materialisation_isolates_locators_and_never_touches_registry(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    registry = root / "registry.sqlite"
    registry.write_bytes(b"synthetic-registry-sentinel")
    registry.chmod(0o600)
    before = (registry.read_bytes(), registry.stat().st_mtime_ns)
    first, second = _locator(1), _locator(2)
    _foundation(root, first)
    _foundation(root, second)
    store = SQLiteDomainSchemaStore(SQLiteLocationPolicy(root))
    specification = _specification()

    assert store.materialise(first, specification) == DomainSchemaState("ready")
    assert store.inspect(second, specification) == DomainSchemaState("unmaterialised")
    assert (registry.read_bytes(), registry.stat().st_mtime_ns) == before


@pytest.mark.parametrize("fault_index", range(4))
def test_each_domain_ddl_failure_rolls_back_without_materialising_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_index: int
) -> None:
    store, locator, root, _, specification = _ready(tmp_path)
    actual_layout = domain_module.build_domain_schema_layout(specification)
    assert len(actual_layout.ddl) == 4
    faulty_ddl = list(actual_layout.ddl)
    faulty_ddl[fault_index] = "NOT VALID SQL"
    faulty_layout = replace(actual_layout, ddl=tuple(faulty_ddl))
    with monkeypatch.context() as context:
        context.setattr(domain_module, "build_domain_schema_layout", lambda value: faulty_layout)
        with pytest.raises(StorageMigrationError) as captured:
            store.materialise(locator, specification)
        _assert_bounded(captured.value, root)
    assert SQLiteDomainSchemaStore(SQLiteLocationPolicy(root)).inspect(
        locator, specification
    ) == DomainSchemaState("unmaterialised")


def test_precommit_mismatch_rolls_back_without_materialising_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, locator, root, _, specification = _ready(tmp_path)
    real_inspect = domain_module._inspect_domain
    calls = 0

    def incompatible_after_ddl(connection: sqlite3.Connection, layout: object) -> DomainSchemaState:
        nonlocal calls
        calls += 1
        if calls == 2:
            return DomainSchemaState("incompatible")
        return real_inspect(connection, layout)  # type: ignore[arg-type]

    with monkeypatch.context() as context:
        context.setattr(domain_module, "_inspect_domain", incompatible_after_ddl)
        with pytest.raises(StorageMigrationError) as captured:
            store.materialise(locator, specification)
        _assert_bounded(captured.value, root)
    assert calls == 2
    assert SQLiteDomainSchemaStore(SQLiteLocationPolicy(root)).inspect(
        locator, specification
    ) == DomainSchemaState("unmaterialised")


@pytest.mark.parametrize("fault", ["commit", "close"])
def test_postcommit_uncertainty_is_bounded_and_durably_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    store, locator, root, _, specification = _ready(tmp_path)
    real_connect = domain_module.connect

    class CommitAfterDurable:
        def __init__(self, wrapped: sqlite3.Connection) -> None:
            self._wrapped = wrapped

        def __getattr__(self, name: str) -> object:
            return getattr(self._wrapped, name)

        def commit(self) -> None:
            self._wrapped.commit()
            if fault == "commit":
                raise sqlite3.OperationalError("secret durable commit")

        def close(self) -> None:
            self._wrapped.close()
            if fault == "close":
                raise sqlite3.OperationalError("secret close uncertainty")

    with monkeypatch.context() as context:
        context.setattr(
            domain_module,
            "connect",
            lambda *args, **kwargs: CommitAfterDurable(real_connect(*args, **kwargs)),
        )
        with pytest.raises(StorageMigrationError) as captured:
            store.materialise(locator, specification)
        _assert_bounded(captured.value, root)
    recovered = SQLiteDomainSchemaStore(SQLiteLocationPolicy(root))
    assert recovered.inspect(locator, specification) == DomainSchemaState("ready")
    assert recovered.materialise(locator, specification) == DomainSchemaState("ready")


def test_identity_and_reacquire_unavailability_take_precedence_after_domain_ddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, locator, root, database, specification = _ready(tmp_path)
    original_database = database.with_suffix(".identity-original")
    real_inspect = domain_module._inspect_domain
    calls = 0

    def replace_after_ddl(connection: sqlite3.Connection, layout: object) -> DomainSchemaState:
        nonlocal calls
        calls += 1
        result = real_inspect(connection, layout)  # type: ignore[arg-type]
        if calls == 2:
            database.rename(original_database)
            descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        return result

    with monkeypatch.context() as context:
        context.setattr(domain_module, "_inspect_domain", replace_after_ddl)
        with pytest.raises(StorageUnavailable) as captured:
            store.materialise(locator, specification)
        _assert_bounded(captured.value, root)
    assert database.stat().st_size == 0
    with pytest.raises(StorageMigrationError):
        SQLiteDomainSchemaStore(SQLiteLocationPolicy(root)).inspect(locator, specification)
    with sqlite3.connect(original_database) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND substr(name, 1, 7) <> 'sqlite_'"
            )
        }
    assert names == {"__aipcs_service_store_meta", "__aipcs_service_store_migration"}

    store, locator, root, _, specification = _ready(tmp_path)
    policy = store._location
    acquire = policy.acquire_service_store
    acquisitions = 0

    def fail_on_postcommit_reacquire(*args: object, **kwargs: object):
        nonlocal acquisitions
        acquisitions += 1
        if acquisitions == 2:
            raise StorageUnavailable()
        return acquire(*args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(policy, "acquire_service_store", fail_on_postcommit_reacquire)
        with pytest.raises(StorageUnavailable) as captured:
            store.materialise(locator, specification)
        _assert_bounded(captured.value, root)
    assert acquisitions == 2
    assert SQLiteDomainSchemaStore(SQLiteLocationPolicy(root)).inspect(
        locator, specification
    ) == DomainSchemaState("ready")


def test_materialise_reinspects_under_exclusive_transaction_before_applying_ddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, locator, _, _, specification = _ready(tmp_path)
    real_inspect = domain_module._inspect_domain
    real_connect = domain_module.connect
    transactions: list[bool] = []
    statements: list[str] = []

    class TracedConnection:
        def __init__(self, wrapped: sqlite3.Connection) -> None:
            self._wrapped = wrapped

        def __getattr__(self, name: str) -> object:
            return getattr(self._wrapped, name)

        def execute(self, sql: str, *args: object, **kwargs: object):
            statements.append(sql)
            return self._wrapped.execute(sql, *args, **kwargs)

    def record_locked_inspection(
        connection: sqlite3.Connection, layout: object
    ) -> DomainSchemaState:
        transactions.append(connection.in_transaction)
        return real_inspect(connection, layout)  # type: ignore[arg-type]

    monkeypatch.setattr(
        domain_module,
        "connect",
        lambda *args, **kwargs: TracedConnection(real_connect(*args, **kwargs)),
    )
    monkeypatch.setattr(domain_module, "_inspect_domain", record_locked_inspection)
    assert store.materialise(locator, specification) == DomainSchemaState("ready")
    assert statements[0] == "BEGIN EXCLUSIVE"
    assert transactions[:2] == [True, True]


def test_materialise_uses_one_detached_layout_after_caller_value_is_mutated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, locator, _, _, specification = _ready(tmp_path)
    real_build = domain_module.build_domain_schema_layout

    def detach_then_mutate(value: RelationalSpecification):
        layout = real_build(value)
        object.__setattr__(value, "entities", (object(),))
        object.__setattr__(value, "schema_version", 2)
        return layout

    monkeypatch.setattr(domain_module, "build_domain_schema_layout", detach_then_mutate)
    assert store.materialise(locator, specification) == DomainSchemaState("ready")


def test_foreign_key_check_cancellation_is_bounded_and_clears_progress_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, locator, root, _, specification = _ready(tmp_path)
    assert store.materialise(locator, specification).status == "ready"
    real_connect = domain_module.connect
    handler_calls: list[tuple[object, int]] = []

    class CancellingConnection:
        def __init__(self, wrapped: sqlite3.Connection) -> None:
            self._wrapped = wrapped
            self._handler: Callable[[], int] | None = None

        def __getattr__(self, name: str) -> object:
            return getattr(self._wrapped, name)

        def set_progress_handler(self, handler: Callable[[], int] | None, steps: int) -> None:
            handler_calls.append((handler, steps))
            self._handler = handler
            self._wrapped.set_progress_handler(handler, steps)

        def execute(self, sql: str, *args: object, **kwargs: object):
            if sql == "PRAGMA foreign_key_check":
                assert self._handler is not None
                for _ in range(100):
                    self._handler()
                raise sqlite3.OperationalError("interrupted foreign-key check")
            return self._wrapped.execute(sql, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(
            domain_module,
            "connect",
            lambda *args, **kwargs: CancellingConnection(real_connect(*args, **kwargs)),
        )
        with pytest.raises(StorageMigrationError) as captured:
            store.inspect(locator, specification)
        _assert_bounded(captured.value, root)
    assert handler_calls[-1] == (None, 0)
