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


async def _tool_names(server: object) -> list[str]:
    handler = server.request_handlers[types.ListToolsRequest]  # type: ignore[attr-defined]
    result = await handler(types.ListToolsRequest())
    return [tool.name for tool in result.root.tools]


def test_stateless_composition_constructs_no_storage() -> None:
    server = runtime.compose_server(_config(None, profile="stateless"))
    assert anyio.run(_tool_names, server) == ["aipcs_server_info"]


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
