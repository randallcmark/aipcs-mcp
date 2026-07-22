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


def _config(root: Path | None, *, profile: str = "sqlite") -> ResolvedConfiguration:
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
            "postgres_dsn_env": "default",
            "log_level": "default",
        },
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

    class Location:
        @classmethod
        def from_resolved(cls, value: Path, source: str) -> object:
            assert value == root
            assert source == "cli"
            calls.append("location")
            return object()

    class Adapter:
        def __init__(self, location: object) -> None:
            assert location is not None
            calls.append("adapter")

        def migrate(self) -> MigrationState:
            calls.append("migrate")
            return MigrationState("registry", 1, 1, "ready")

        def open_uow(self) -> object:
            calls.append("open_uow")
            raise AssertionError("server-info must not open a unit of work")

    monkeypatch.setattr(runtime, "SQLiteLocationPolicy", Location)
    monkeypatch.setattr(runtime, "SQLiteRegistryAdapter", Adapter)
    server = runtime.compose_server(_config(root))
    assert calls == ["location", "adapter", "migrate"]
    assert anyio.run(_tool_names, server) == [
        "aipcs_server_info",
        "aipcs_service_seed",
        "aipcs_service_list",
        "aipcs_service_inspect",
        "aipcs_service_design",
    ]
    assert calls == ["location", "adapter", "migrate"]


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
        def __init__(self, location: object) -> None:
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
    with pytest.raises(RuntimeError, match="Registry is not ready"):
        runtime.compose_server(_config(root))
    assert called is False


def _run_bad_startup(root: Path) -> subprocess.CompletedProcess[str]:
    environment = {"PYTHONPATH": str(ROOT / "src"), "PATH": os.environ.get("PATH", "")}
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
