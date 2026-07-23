"""Real SQLite restart and cross-store fault proof for the private coordinator."""

from __future__ import annotations

import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from fixtures import valid_manifest

from aipcs_mcp.application.models import PreparedLifecycleClaim, Service
from aipcs_mcp.lifecycle import EvolveCommand, LifecyclePhase, MaterialiseCommand
from aipcs_mcp.lifecycle_coordinator import LifecycleCoordinator
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.relational import classify_transition, compile_manifest
from aipcs_mcp.storage import DomainSchemaState, MigrationState, ServiceStoreLocator
from aipcs_mcp.storage.errors import StorageMigrationError
from aipcs_mcp.storage.sqlite import (
    SQLiteLocationPolicy,
    SQLiteRegistryAdapter,
    SQLiteServiceStoreCatalog,
)
from aipcs_mcp.storage.sqlite.domain_schema import SQLiteDomainSchemaStore

_AT = datetime(2026, 7, 23, tzinfo=UTC)
_PRINCIPAL = "coordinator-sqlite"
_SERVICE_ID = UUID("2b38d5b8-10d0-4fc6-9d1f-2d0600c0ffee")


@dataclass
class _Clock:
    value: datetime = _AT

    def now(self) -> datetime:
        value = self.value
        self.value += timedelta(seconds=1)
        return value


class _PostMaterialiseFailure:
    """Perform one real DDL commit, then model lost post-action certainty."""

    def __init__(self, wrapped: SQLiteDomainSchemaStore) -> None:
        self._wrapped = wrapped
        self._fail_once = True

    def inspect(self, locator: ServiceStoreLocator, specification: object) -> DomainSchemaState:
        return self._wrapped.inspect(locator, specification)  # type: ignore[arg-type]

    def materialise(self, locator: ServiceStoreLocator, specification: object) -> DomainSchemaState:
        result = self._wrapped.materialise(locator, specification)  # type: ignore[arg-type]
        if self._fail_once:
            self._fail_once = False
            raise StorageMigrationError()
        return result

    def evolve(self, locator: ServiceStoreLocator, transition: object) -> DomainSchemaState:
        return self._wrapped.evolve(locator, transition)  # type: ignore[arg-type]


class _RaiseAfterCommit:
    """Expose a committed registry finalisation with an uncertain caller outcome."""

    def __init__(self, wrapped: object) -> None:
        self._wrapped = wrapped

    def commit(self) -> None:
        self._wrapped.commit()
        raise StorageMigrationError()

    def __getattr__(self, name: str) -> object:
        return getattr(self._wrapped, name)


def _manifest() -> ManifestV2:
    return ManifestV2.model_validate(valid_manifest())


def _evolved(current: ManifestV2) -> ManifestV2:
    payload = current.model_dump(mode="json", by_alias=True)
    payload = deepcopy(payload)
    payload["schema_version"] = 2
    payload["migration_history"] = [
        {
            "from_schema_version": 1,
            "to_schema_version": 2,
            "operations": ["add nullable project tag"],
        }
    ]
    payload["entities"][0]["attributes"].append({"name": "tag", "type": "string"})
    return ManifestV2.model_validate(payload)


def _adapter(root: Path) -> SQLiteRegistryAdapter:
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    assert adapter.migrate() == MigrationState("registry", 3, 3, "ready")
    return adapter


def _catalog(root: Path) -> SQLiteServiceStoreCatalog:
    return SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root))


def _domain(root: Path) -> SQLiteDomainSchemaStore:
    return SQLiteDomainSchemaStore(SQLiteLocationPolicy(root))


def _service(manifest: ManifestV2 | None = None) -> Service:
    manifest = manifest or _manifest()
    return Service(
        service_id=_SERVICE_ID,
        principal_id=_PRINCIPAL,
        domain_name="coordinator_sqlite",
        domain_class="project",
        intent_description="Cross-store lifecycle proof",
        created_at=_AT,
        updated_at=_AT,
        last_activity_at=_AT,
        manifest=manifest,
        schema_version=manifest.schema_version,
    )


def _write(adapter: SQLiteRegistryAdapter, operation):
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


def _read_service(adapter: SQLiteRegistryAdapter) -> Service:
    uow = adapter.open_uow()
    try:
        service = uow.services.get(_PRINCIPAL, _SERVICE_ID)
        assert service is not None
        return service
    finally:
        uow.close()


def _admit(adapter: SQLiteRegistryAdapter, command: MaterialiseCommand | EvolveCommand):
    return _write(adapter, lambda uow: uow.mutations.resolve_or_admit(command))


def _materialise(key: str = "materialise") -> MaterialiseCommand:
    return MaterialiseCommand(
        principal_id=_PRINCIPAL,
        created_via="sqlite-coordinator-test",
        service_id=_SERVICE_ID,
        expected_service_revision=1,
        expected_schema_version=1,
        idempotency_key=key,
    )


def _evolve(target: ManifestV2, key: str = "evolve") -> EvolveCommand:
    return EvolveCommand(
        principal_id=_PRINCIPAL,
        created_via="sqlite-coordinator-test",
        service_id=_SERVICE_ID,
        expected_service_revision=2,
        expected_schema_version=1,
        idempotency_key=key,
        target_manifest=target,
    )


def _coordinator(
    adapter: SQLiteRegistryAdapter,
    catalog: SQLiteServiceStoreCatalog,
    domain: object,
    clock: _Clock | None = None,
) -> LifecycleCoordinator:
    return LifecycleCoordinator(adapter.open_uow, clock or _Clock(), catalog, domain)  # type: ignore[arg-type]


def _audit_rows(root: Path) -> list[tuple[str, str, str]]:
    with sqlite3.connect(root / "registry.sqlite") as connection:
        return connection.execute(
            'SELECT "action","outcome","service_id" FROM "aipcs_registry_audit" '
            'WHERE "principal_id"=? ORDER BY "audit_id"',
            (_PRINCIPAL,),
        ).fetchall()


def _mutation_phase(root: Path, key: str) -> str:
    with sqlite3.connect(root / "registry.sqlite") as connection:
        row = connection.execute(
            'SELECT "phase" FROM "aipcs_registry_mutation" '
            'WHERE "principal_id"=? AND "idempotency_key"=?',
            (_PRINCIPAL, key),
        ).fetchone()
    assert row is not None
    return row[0]


def test_materialise_restarts_to_exact_completed_replay_with_one_terminal_audit(tmp_path: Path) -> None:
    root = tmp_path / "root"
    adapter = _adapter(root)
    _write(adapter, lambda uow: uow.services.add(_service()))
    catalog = _catalog(root)
    command = _materialise()

    first = _coordinator(adapter, catalog, _domain(root)).execute(command)
    replay = _coordinator(adapter, catalog, _domain(root)).execute(command)

    assert first.category == replay.category == "completed"
    assert first.service == replay.service
    assert first.service is not None
    assert (first.service.service_revision, first.service.design_state) == (2, "materialised")
    locator = catalog.allocate(_SERVICE_ID)
    assert catalog.inspect_migration(locator) == MigrationState("service_store", 2, 2, "ready")
    assert _domain(root).inspect(locator, compile_manifest(_manifest())) == DomainSchemaState("ready")
    assert _audit_rows(root) == [("materialise", "completed", str(_SERVICE_ID))]


def test_prepared_materialise_adopts_exact_physical_target_after_restart(tmp_path: Path) -> None:
    root = tmp_path / "root"
    adapter = _adapter(root)
    manifest = _manifest()
    _write(adapter, lambda uow: uow.services.add(_service(manifest)))
    catalog = _catalog(root)
    command = _materialise("materialise-target-first")
    prepared = _admit(adapter, command)
    assert isinstance(prepared, PreparedLifecycleClaim)
    locator = catalog.allocate(_SERVICE_ID)
    assert catalog.migrate(locator) == MigrationState("service_store", 2, 2, "ready")
    assert _domain(root).materialise(locator, compile_manifest(manifest)) == DomainSchemaState("ready")

    result = _coordinator(adapter, catalog, _domain(root)).execute(command)

    assert result.category == "completed"
    assert _mutation_phase(root, command.idempotency_key) == LifecyclePhase.COMPLETED.value
    assert _read_service(adapter).service_revision == 2
    assert _audit_rows(root) == [("materialise", "completed", str(_SERVICE_ID))]


def test_prepared_evolve_adopts_exact_target_without_reapplying_transition(tmp_path: Path) -> None:
    root = tmp_path / "root"
    adapter = _adapter(root)
    current = _manifest()
    target = _evolved(current)
    _write(adapter, lambda uow: uow.services.add(_service(current)))
    catalog = _catalog(root)
    assert _coordinator(adapter, catalog, _domain(root)).execute(_materialise()).category == "completed"
    command = _evolve(target, "evolve-target-first")
    prepared = _admit(adapter, command)
    assert isinstance(prepared, PreparedLifecycleClaim)
    locator = catalog.allocate(_SERVICE_ID)
    transition = classify_transition(current, target)
    assert _domain(root).evolve(locator, transition) == DomainSchemaState("ready")

    result = _coordinator(adapter, catalog, _domain(root)).execute(command)

    assert result.category == "completed"
    service = _read_service(adapter)
    assert (service.service_revision, service.schema_version, service.manifest) == (3, 2, target)
    assert _domain(root).inspect(locator, compile_manifest(target)) == DomainSchemaState("ready")
    assert _audit_rows(root) == [
        ("materialise", "completed", str(_SERVICE_ID)),
        ("evolve", "completed", str(_SERVICE_ID)),
    ]


def test_post_domain_commit_failure_leaves_prepared_then_retry_reobserves_and_completes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    adapter = _adapter(root)
    _write(adapter, lambda uow: uow.services.add(_service()))
    catalog = _catalog(root)
    command = _materialise("materialise-uncertain")

    uncertain = _coordinator(
        adapter,
        catalog,
        _PostMaterialiseFailure(_domain(root)),
    ).execute(command)

    assert uncertain.category.value == "operation_uncertain"
    assert _mutation_phase(root, command.idempotency_key) == LifecyclePhase.PREPARED.value
    locator = catalog.allocate(_SERVICE_ID)
    assert _domain(root).inspect(locator, compile_manifest(_manifest())) == DomainSchemaState("ready")
    assert _audit_rows(root) == []

    completed = _coordinator(adapter, catalog, _domain(root)).execute(command)
    assert completed.category == "completed"
    assert _mutation_phase(root, command.idempotency_key) == LifecyclePhase.COMPLETED.value
    assert _audit_rows(root) == [("materialise", "completed", str(_SERVICE_ID))]


def test_post_terminal_commit_failure_is_uncertain_then_exact_retry_replays(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    adapter = _adapter(root)
    _write(adapter, lambda uow: uow.services.add(_service()))
    catalog = _catalog(root)
    command = _materialise("materialise-terminal-uncertain")
    uow_count = 0

    def uows() -> object:
        nonlocal uow_count
        uow_count += 1
        wrapped = adapter.open_uow()
        # Admission is the first UoW. The second is terminal finalisation.
        return _RaiseAfterCommit(wrapped) if uow_count == 2 else wrapped

    uncertain = LifecycleCoordinator(uows, _Clock(), catalog, _domain(root)).execute(command)

    assert uncertain.category.value == "operation_uncertain"
    assert _mutation_phase(root, command.idempotency_key) == LifecyclePhase.COMPLETED.value
    assert _audit_rows(root) == [("materialise", "completed", str(_SERVICE_ID))]

    replay = _coordinator(adapter, catalog, _domain(root)).execute(command)
    assert replay.category == "completed"
    assert replay.service is not None and replay.service.service_revision == 2
    assert _audit_rows(root) == [("materialise", "completed", str(_SERVICE_ID))]


def test_partial_domain_state_becomes_recovery_required_without_automatic_repair(tmp_path: Path) -> None:
    root = tmp_path / "root"
    adapter = _adapter(root)
    _write(adapter, lambda uow: uow.services.add(_service()))
    catalog = _catalog(root)
    command = _materialise("materialise-incompatible")
    assert isinstance(_admit(adapter, command), PreparedLifecycleClaim)
    locator = catalog.allocate(_SERVICE_ID)
    assert catalog.migrate(locator) == MigrationState("service_store", 2, 2, "ready")
    database = root / "service-stores" / f"{locator.namespace}.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE "unexpected" ("id" TEXT PRIMARY KEY) STRICT')

    result = _coordinator(adapter, catalog, _domain(root)).execute(command)

    assert result.category.value == "recovery_required"
    assert _mutation_phase(root, command.idempotency_key) == LifecyclePhase.RECOVERY_REQUIRED.value
    assert _read_service(adapter).design_state == "seeded"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            'SELECT name FROM sqlite_schema WHERE type=\'table\' AND name=\'unexpected\''
        ).fetchone() == ("unexpected",)
    assert _audit_rows(root) == [("materialise", "recovery_required", str(_SERVICE_ID))]
