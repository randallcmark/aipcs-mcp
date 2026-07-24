"""SYNTHETIC_FIXTURE. Composition and startup-readiness boundaries."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from mcp import types

import aipcs_mcp.runtime as runtime
from aipcs_mcp.configuration.models import ResolvedConfiguration
from aipcs_mcp.storage.contracts import MigrationState
from aipcs_mcp.storage.postgresql import PostgreSQLDsn
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter

ROOT = Path(__file__).resolve().parents[1]


def _config(
    root: Path | None, *, profile: str = "sqlite", busy_timeout_ms: int = 5000
) -> ResolvedConfiguration:
    return ResolvedConfiguration(
        profile=profile,  # type: ignore[arg-type]
        transport="stdio",
        principal_id="configured-principal" if root is not None else None,
        sqlite_data_root=root,
        postgres_dsn_env=None,
        log_level="warning",
        sources={
            "profile": "cli",
            "transport": "default",
            "principal_id": "cli" if root is not None else "default",
            "sqlite_data_root": "cli" if root is not None else "default",
            "sqlite_busy_timeout_ms": "default",
            "postgres_dsn_env": "default",
            "log_level": "default",
        },
        sqlite_busy_timeout_ms=busy_timeout_ms,
    )


def _secure_parent(root: Path) -> None:
    os.chmod(root.parent, 0o700)


def _postgres_config() -> ResolvedConfiguration:
    return ResolvedConfiguration(
        profile="postgresql",
        transport="stdio",
        principal_id="configured-principal",
        sqlite_data_root=None,
        postgres_dsn_env="AIPCS_SYNTHETIC_DSN",
        log_level="warning",
        sources={
            "profile": "cli",
            "transport": "default",
            "principal_id": "cli",
            "sqlite_data_root": "default",
            "sqlite_busy_timeout_ms": "default",
            "postgres_dsn_env": "cli",
            "postgres_connect_timeout_seconds": "cli",
            "postgres_lock_timeout_ms": "cli",
            "postgres_statement_timeout_ms": "cli",
            "log_level": "default",
        },
        postgres_connect_timeout_seconds=7,
        postgres_lock_timeout_ms=321,
        postgres_statement_timeout_ms=6543,
    )


async def _tool_names(server: object) -> list[str]:
    handler = server.request_handlers[types.ListToolsRequest]  # type: ignore[attr-defined]
    result = await handler(types.ListToolsRequest())
    return [tool.name for tool in result.root.tools]


def test_stateless_composition_constructs_no_storage() -> None:
    server = runtime.compose_server(_config(None, profile="stateless"))
    assert anyio.run(_tool_names, server) == ["aipcs_server_info"]


def test_read_only_admin_composition_does_not_create_or_migrate_sqlite(
    tmp_path: Path,
) -> None:
    root = tmp_path / "admin-read-only"
    _secure_parent(root)

    admin = runtime.compose_admin_runtime(_config(root))

    assert not root.exists()
    result = admin.inspection.status(admin.context)
    assert result.registry is not None
    assert result.registry.status == "uninitialised"
    assert result.overall == "attention_required"
    assert not root.exists()


def test_stateless_admin_composition_has_no_persistent_runtime() -> None:
    admin = runtime.compose_admin_runtime(_config(None, profile="stateless"))
    assert admin.inspection.status(admin.context).profile == "stateless"


def test_private_portable_composition_selects_the_configured_sqlite_backend(
    tmp_path: Path,
) -> None:
    root = tmp_path / "portable"
    _secure_parent(root)
    coordinator = runtime._compose_portable_coordinator(_config(root))
    assert isinstance(coordinator, runtime.PortableCoordinator)
    assert coordinator._backend.value == "sqlite"
    assert isinstance(coordinator._store, runtime.SQLitePortableServiceStore)


def test_ready_sqlite_migrates_once_before_mcp_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "ready-root"
    calls: list[str] = []
    location = object()
    catalog: object | None = None
    domain: object | None = None
    coordinator: object | None = None
    coordinator_clock: object | None = None

    class Location:
        @classmethod
        def from_resolved(cls, value: Path, source: str) -> object:
            assert value == root
            assert source == "cli"
            calls.append("location")
            return location

    class Adapter:
        def __init__(self, location: object, *, busy_timeout_ms: int) -> None:
            assert location is not None
            assert busy_timeout_ms == 123
            calls.append("adapter")

        def migrate(self) -> MigrationState:
            calls.append("migrate")
            return MigrationState("registry", 1, 1, "ready")

        def open_uow(self) -> object:
            calls.append("open_uow")
            raise AssertionError("server-info must not open a unit of work")

    class Catalog:
        def __init__(self, value: object, *, busy_timeout_ms: int) -> None:
            nonlocal catalog
            assert value is location
            assert busy_timeout_ms == 123
            catalog = self
            calls.append("catalog")

    class Domain:
        def __init__(self, value: object, *, busy_timeout_ms: int) -> None:
            nonlocal domain
            assert value is location
            assert busy_timeout_ms == 123
            domain = self
            calls.append("domain")

    class Coordinator:
        def __init__(
            self,
            uows: object,
            clock: object,
            catalog_value: object,
            domain_value: object,
        ) -> None:
            nonlocal coordinator, coordinator_clock
            assert callable(uows)
            assert isinstance(clock, runtime.SystemUtcClock)
            assert catalog_value is catalog
            assert domain_value is domain
            coordinator = self
            coordinator_clock = clock
            calls.append("coordinator")

    class DataStore:
        def __init__(self, value: object, **kwargs: object) -> None:
            assert value is location
            assert kwargs["busy_timeout_ms"] == 123
            assert callable(kwargs["clock"])
            assert callable(kwargs["record_ids"])
            calls.append("data_store")

    server = object()

    def create(**kwargs: object) -> object:
        application = kwargs["application"]
        assert isinstance(application, runtime.ServiceApplication)
        assert application._clock is coordinator_clock
        assert kwargs["principal_id"] == "configured-principal"
        assert kwargs["registry_lifecycle"] is True
        assert kwargs["lifecycle_executor"] is coordinator
        assert isinstance(kwargs["data_application"], runtime.DataApplication)
        calls.append("server")
        return server

    monkeypatch.setattr(runtime, "SQLiteLocationPolicy", Location)
    monkeypatch.setattr(runtime, "SQLiteRegistryAdapter", Adapter)
    monkeypatch.setattr(runtime, "SQLiteServiceStoreCatalog", Catalog)
    monkeypatch.setattr(runtime, "SQLiteDomainSchemaStore", Domain)
    monkeypatch.setattr(runtime, "LifecycleCoordinator", Coordinator)
    monkeypatch.setattr(runtime, "SQLiteMaterialisedDataStore", DataStore)
    monkeypatch.setattr(runtime, "create_server", create)
    assert runtime.compose_server(_config(root, busy_timeout_ms=123)) is server
    assert calls == [
        "location", "adapter", "migrate", "catalog", "domain", "coordinator", "data_store", "server"
    ]


def test_ready_postgresql_resolves_the_dsn_once_and_composes_one_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    descriptor: PostgreSQLDsn | None = None
    policy = object()
    catalog: object | None = None
    domain: object | None = None
    coordinator: object | None = None
    clock: object | None = None

    class Environment(dict[str, str]):
        reads = 0

        def get(self, key: str, default: object = None) -> str | object:  # type: ignore[override]
            self.reads += 1
            assert key == "AIPCS_SYNTHETIC_DSN"
            return super().get(key, default)

    environment = Environment(AIPCS_SYNTHETIC_DSN="postgresql://synthetic-secret")

    class Policy:
        @classmethod
        def from_configuration(cls, config: ResolvedConfiguration) -> object:
            assert config is _postgres_config_value
            calls.append("policy")
            return policy

    class Adapter:
        def __init__(self, received_dsn: object, received_policy: object) -> None:
            nonlocal descriptor
            assert isinstance(received_dsn, PostgreSQLDsn)
            assert received_policy is policy
            descriptor = received_dsn
            calls.append("adapter")

        def migrate(self) -> MigrationState:
            calls.append("migrate")
            return MigrationState("registry", 1, 1, "ready")

        def open_uow(self) -> object:
            raise AssertionError("composition must not open a registry unit of work")

    class Catalog:
        def __init__(self, received_dsn: object, **kwargs: object) -> None:
            nonlocal catalog
            assert received_dsn is descriptor
            assert kwargs == {
                "connect_timeout_seconds": 7,
                "lock_timeout_ms": 321,
                "statement_timeout_ms": 6543,
            }
            catalog = self
            calls.append("catalog")

    class Domain:
        def __init__(self, received_dsn: object, **kwargs: object) -> None:
            nonlocal domain
            assert received_dsn is descriptor
            assert kwargs == {
                "connect_timeout_seconds": 7,
                "lock_timeout_ms": 321,
                "statement_timeout_ms": 6543,
                "service_store_catalog": catalog,
            }
            domain = self
            calls.append("domain")

    class Coordinator:
        def __init__(
            self,
            uows: object,
            received_clock: object,
            received_catalog: object,
            received_domain: object,
        ) -> None:
            nonlocal coordinator, clock
            assert callable(uows)
            assert isinstance(received_clock, runtime.SystemUtcClock)
            assert received_catalog is catalog
            assert received_domain is domain
            coordinator, clock = self, received_clock
            calls.append("coordinator")

        def execute(self, command: object) -> object:
            raise AssertionError(f"tool registration must not execute {command!r}")

    class DataStore:
        def __init__(self, received_dsn: object, **kwargs: object) -> None:
            assert received_dsn is descriptor
            assert kwargs["connect_timeout_seconds"] == 7
            assert kwargs["lock_timeout_ms"] == 321
            assert kwargs["statement_timeout_ms"] == 6543
            assert kwargs["service_store_catalog"] is catalog
            assert kwargs["domain_schema_store"] is domain
            assert callable(kwargs["clock"])
            assert callable(kwargs["record_ids"])
            assert callable(kwargs["branch_ids"])
            calls.append("data_store")

    real_create_server = runtime.create_server
    server: object | None = None

    def create(**kwargs: object) -> object:
        nonlocal server
        application = kwargs["application"]
        assert isinstance(application, runtime.ServiceApplication)
        assert application._clock is clock
        assert kwargs["principal_id"] == "configured-principal"
        assert kwargs["registry_lifecycle"] is True
        assert kwargs["lifecycle_executor"] is coordinator
        assert isinstance(kwargs["data_application"], runtime.DataApplication)
        calls.append("server")
        server = real_create_server(**kwargs)
        return server

    _postgres_config_value = _postgres_config()
    monkeypatch.setattr(runtime, "PostgreSQLConnectionPolicy", Policy)
    monkeypatch.setattr(runtime, "PostgreSQLRegistryAdapter", Adapter)
    monkeypatch.setattr(runtime, "PostgreSQLServiceStoreCatalog", Catalog)
    monkeypatch.setattr(runtime, "PostgreSQLDomainSchemaStore", Domain)
    monkeypatch.setattr(runtime, "LifecycleCoordinator", Coordinator)
    monkeypatch.setattr(runtime, "PostgreSQLMaterialisedDataStore", DataStore)
    monkeypatch.setattr(runtime, "create_server", create)

    composed = runtime.compose_server(_postgres_config_value, environ=environment)
    assert composed is server
    assert environment.reads == 1
    assert calls == [
        "policy",
        "adapter",
        "migrate",
        "catalog",
        "domain",
        "coordinator",
        "data_store",
        "server",
    ]
    assert anyio.run(_tool_names, composed) == [
        "aipcs_server_info",
        "aipcs_service_seed",
        "aipcs_service_list",
        "aipcs_service_inspect",
        "aipcs_service_design",
        "aipcs_service_materialise",
        "aipcs_service_evolve",
        "aipcs_record_create",
        "aipcs_record_get",
        "aipcs_record_list",
        "aipcs_record_search",
        "aipcs_record_update",
        "aipcs_record_delete",
        "aipcs_record_history",
        "aipcs_bootstrap",
        "aipcs_service_summary",
        "aipcs_branch_create",
        "aipcs_branch_list",
        "aipcs_branch_update",
        "aipcs_branch_assign_records",
        "aipcs_maintenance_scan",
    ]


@pytest.mark.parametrize("secret", [None, "\x00invalid"])
def test_postgresql_secret_failures_are_bounded_before_adapter_construction(
    monkeypatch: pytest.MonkeyPatch, secret: str | None
) -> None:
    config = _postgres_config()
    monkeypatch.setattr(
        runtime,
        "PostgreSQLRegistryAdapter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid secret must not construct an adapter")
        ),
    )
    with pytest.raises(RuntimeError) as raised:
        runtime.compose_server(config, environ={"AIPCS_SYNTHETIC_DSN": secret} if secret else {})
    rendered = str(raised.value)
    assert rendered == "PostgreSQL runtime could not be composed."
    assert "AIPCS_SYNTHETIC_DSN" not in rendered
    assert "invalid" not in rendered


def test_postgresql_composition_bounds_lower_layer_runtime_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _postgres_config()
    synthetic_dsn = "postgresql://synthetic-secret"
    monkeypatch.setattr(runtime, "resolve_postgresql_dsn_for_serve", lambda *_: object())
    monkeypatch.setattr(
        runtime,
        "PostgreSQLConnectionPolicy",
        type("Policy", (), {"from_configuration": classmethod(lambda cls, _: object())}),
    )
    monkeypatch.setattr(
        runtime,
        "PostgreSQLRegistryAdapter",
        lambda *_args: (_ for _ in ()).throw(RuntimeError(synthetic_dsn)),
    )
    with pytest.raises(RuntimeError) as raised:
        runtime.compose_server(config, environ={"AIPCS_SYNTHETIC_DSN": synthetic_dsn})
    assert str(raised.value) == "PostgreSQL runtime could not be composed."
    assert synthetic_dsn not in str(raised.value)


@pytest.mark.parametrize("state", ["uninitialised", "incompatible"])
def test_non_ready_postgresql_fails_before_service_store_construction(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    descriptor = object()

    class Policy:
        @classmethod
        def from_configuration(cls, config: ResolvedConfiguration) -> object:
            return object()

    class Adapter:
        def __init__(self, *_args: object) -> None:
            pass

        def migrate(self) -> MigrationState:
            return MigrationState("registry", 0 if state == "uninitialised" else 1, 1, state)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime, "resolve_postgresql_dsn_for_serve", lambda *_: descriptor)
    monkeypatch.setattr(runtime, "PostgreSQLConnectionPolicy", Policy)
    monkeypatch.setattr(runtime, "PostgreSQLRegistryAdapter", Adapter)
    monkeypatch.setattr(
        runtime,
        "PostgreSQLServiceStoreCatalog",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("non-ready registry must not construct service stores")
        ),
    )
    with pytest.raises(RuntimeError, match="PostgreSQL runtime could not be composed"):
        runtime.compose_server(_postgres_config(), environ={"AIPCS_SYNTHETIC_DSN": "ignored"})


def test_ready_sqlite_server_info_does_not_allocate_or_migrate_a_service_store(tmp_path: Path) -> None:
    root = tmp_path / "ready-root"
    _secure_parent(root)
    server = runtime.compose_server(_config(root))
    assert anyio.run(_tool_names, server) == [
        "aipcs_server_info",
        "aipcs_service_seed",
        "aipcs_service_list",
        "aipcs_service_inspect",
        "aipcs_service_design",
        "aipcs_service_materialise",
        "aipcs_service_evolve",
        "aipcs_record_create",
        "aipcs_record_get",
        "aipcs_record_list",
        "aipcs_record_search",
        "aipcs_record_update",
        "aipcs_record_delete",
        "aipcs_record_history",
        "aipcs_bootstrap",
        "aipcs_service_summary",
        "aipcs_branch_create",
        "aipcs_branch_list",
        "aipcs_branch_update",
        "aipcs_branch_assign_records",
        "aipcs_maintenance_scan",
    ]
    assert (root / "registry.sqlite").is_file()
    assert not (root / "service-stores").exists()


@pytest.mark.parametrize("state", ["uninitialised", "dirty", "incompatible"])
def test_non_ready_sqlite_fails_before_mcp_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str
) -> None:
    root = tmp_path / "not-ready-root"
    called = False

    class Location:
        @classmethod
        def from_resolved(cls, value: Path, source: str) -> object:
            return object()

    class Adapter:
        def __init__(self, location: object, *, busy_timeout_ms: int) -> None:
            pass

        def migrate(self) -> MigrationState:
            revision = 0 if state == "uninitialised" else 1
            return MigrationState("registry", revision, 1, state)  # type: ignore[arg-type]

    def unexpected_server(**_: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("non-ready storage must fail before MCP construction")

    monkeypatch.setattr(runtime, "SQLiteLocationPolicy", Location)
    monkeypatch.setattr(runtime, "SQLiteRegistryAdapter", Adapter)
    monkeypatch.setattr(runtime, "create_server", unexpected_server)
    monkeypatch.setattr(
        runtime,
        "SQLiteServiceStoreCatalog",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("non-ready storage must not construct a service catalog")
        ),
    )
    monkeypatch.setattr(
        runtime,
        "SQLiteDomainSchemaStore",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("non-ready storage must not construct a domain store")
        ),
    )
    monkeypatch.setattr(
        runtime,
        "LifecycleCoordinator",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("non-ready storage must not construct a coordinator")
        ),
    )
    with pytest.raises(RuntimeError, match="Registry is not ready"):
        runtime.compose_server(_config(root))
    assert called is False


def test_unsupported_sqlite_runtime_fails_before_location_or_adapter_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime, "is_supported_sqlite_runtime", lambda: False)
    monkeypatch.setattr(
        runtime,
        "SQLiteLocationPolicy",
        type("UnexpectedLocation", (), {"from_resolved": lambda *_: (_ for _ in ()).throw(AssertionError)}),
    )
    with pytest.raises(RuntimeError, match="Unsupported runtime profile"):
        runtime.compose_server(_config(tmp_path / "root"))


def _run_bad_startup(root: Path) -> subprocess.CompletedProcess[str]:
    environment = {
        "PYTHONPATH": str(ROOT / "src"),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "aipcs_mcp",
            "serve",
            "--profile",
            "sqlite",
            "--principal-id",
            "secret-principal",
            "--sqlite-data-root",
            str(root),
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def _migrated_root(root: Path) -> Path:
    _secure_parent(root)
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
    assert adapter.migrate().status == "ready"
    return root / "registry.sqlite"


@pytest.mark.parametrize("kind", ["unsafe", "dirty", "incompatible"])
def test_startup_failures_are_one_bounded_stderr_envelope(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "secret-root-component"
    if kind == "unsafe":
        root.mkdir(mode=0o755)
        os.chmod(root, 0o755)
    else:
        database = _migrated_root(root)
        connection = sqlite3.connect(database)
        if kind == "dirty":
            connection.execute('UPDATE "aipcs_registry_meta" SET "dirty"=1')
        else:
            connection.execute('ALTER TABLE "aipcs_registry_meta" ADD COLUMN "hostile" INTEGER')
        connection.commit()
        connection.close()
    completed = _run_bad_startup(root)
    assert completed.returncode == 2
    assert completed.stdout == ""
    lines = completed.stderr.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "ok": False,
        "result": None,
        "error": {
            "code": "internal_error",
            "message": "Server could not be started safely.",
            "issues": [],
            "retryable": False,
        },
    }
    for secret in ("secret-principal", "secret-root-component", "registry.sqlite", "sqlite3"):
        assert secret not in completed.stderr.lower()
