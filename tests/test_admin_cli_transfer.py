"""SYNTHETIC_FIXTURE. V1-11 SQLite CLI portable transfer workflow."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from uuid import UUID

import pytest

from aipcs_mcp import cli
from aipcs_mcp.application.models import ApplicationContext, SeedCommand
from aipcs_mcp.application.services import ServiceApplication
from aipcs_mcp.runtime import SystemUtcClock, Uuid4ServiceIds
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter

PRINCIPAL = "transfer-principal"
SUSPEND_ID = "11111111-1111-4111-8111-111111111111"
EXPORT_ID = "22222222-2222-4222-8222-222222222222"
IMPORT_ID = "33333333-3333-4333-8333-333333333333"


def _config(root: Path) -> list[str]:
    return [
        "--profile",
        "sqlite",
        "--principal-id",
        PRINCIPAL,
        "--sqlite-data-root",
        str(root),
    ]


def _ready_registry(root: Path) -> SQLiteRegistryAdapter:
    registry = SQLiteRegistryAdapter(SQLiteLocationPolicy.from_resolved(root, "cli"))
    assert registry.migrate().status == "ready"
    return registry


def _source_service(root: Path) -> str:
    registry = _ready_registry(root)
    service = ServiceApplication(
        registry.open_uow, SystemUtcClock(), Uuid4ServiceIds()
    ).seed(
        ApplicationContext(PRINCIPAL, "cli"),
        SeedCommand("notes", "knowledge", "Keep durable notes.", "seed-transfer"),
    )
    return str(service.service_id)


@pytest.mark.requires_sqlite
def test_export_dry_run_import_replay_collision_and_tamper(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    os.chmod(tmp_path, 0o700)
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    service_id = _source_service(source_root)
    source_config = _config(source_root)
    destination_config = _config(destination_root)

    assert (
        cli.main(
            [
                "service",
                "suspend",
                service_id,
                "--expected-revision",
                "1",
                "--operation-id",
                SUSPEND_ID,
                *source_config,
            ]
        )
        == 0
    )
    capsys.readouterr()

    bundle = tmp_path / "bundle.jsonl"
    export = [
        "export",
        service_id,
        "--output",
        str(bundle),
        "--expected-revision",
        "2",
        "--operation-id",
        EXPORT_ID,
        *source_config,
    ]
    assert cli.main(export) == 0
    exported = json.loads(capsys.readouterr().out)["result"]
    assert exported["operation"] == "export"
    assert exported["summary"]["source_backend"] == "sqlite"
    assert exported["receipt"]["service_id"] == service_id
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o600

    replay_bundle = tmp_path / "bundle-replay.jsonl"
    export[export.index(str(bundle))] = str(replay_bundle)
    assert cli.main(export) == 0
    replayed = json.loads(capsys.readouterr().out)["result"]
    assert replayed == exported
    assert replay_bundle.read_bytes() == bundle.read_bytes()

    dry_run = [
        "import",
        "--input",
        str(bundle),
        "--dry-run",
        *destination_config,
    ]
    assert cli.main(dry_run) == 0
    validated = json.loads(capsys.readouterr().out)["result"]
    assert validated["operation"] == "import_dry_run"
    assert validated["service"]["service_id"] == service_id
    assert not destination_root.exists()

    destination_registry = _ready_registry(destination_root)
    actual_import = [
        "import",
        "--input",
        str(bundle),
        "--operation-id",
        IMPORT_ID,
        "--yes",
        *destination_config,
    ]
    assert cli.main(actual_import) == 0
    imported = json.loads(capsys.readouterr().out)["result"]
    assert imported["operation"] == "import"
    assert imported["service"]["service_id"] == service_id
    assert imported["service"]["operational_status"] == "suspended"

    assert cli.main(actual_import) == 0
    assert json.loads(capsys.readouterr().out)["result"] == imported

    collision = actual_import.copy()
    collision[collision.index(IMPORT_ID)] = "44444444-4444-4444-8444-444444444444"
    assert cli.main(collision) == 3
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "already_exists"

    inspected = ServiceApplication(
        destination_registry.open_uow, SystemUtcClock(), Uuid4ServiceIds()
    ).inspect(ApplicationContext(PRINCIPAL, "cli"), UUID(service_id))
    assert str(inspected.service_id) == service_id

    tampered = tmp_path / "tampered.jsonl"
    content = bytearray(bundle.read_bytes())
    content[len(content) // 2] ^= 1
    tampered.write_bytes(content)
    empty_root = tmp_path / "empty"
    empty_registry = _ready_registry(empty_root)
    tamper_import = [
        "import",
        "--input",
        str(tampered),
        "--operation-id",
        "55555555-5555-4555-8555-555555555555",
        "--yes",
        *_config(empty_root),
    ]
    assert cli.main(tamper_import) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "validation_failed"
    uow = empty_registry.open_uow()
    assert uow.services.count(PRINCIPAL) == 0
    uow.close()


@pytest.mark.requires_sqlite
def test_import_rejects_symlink_without_echoing_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "private-name.jsonl"
    source.write_bytes(b"not-a-bundle")
    link = tmp_path / "public-link.jsonl"
    link.symlink_to(source)

    assert (
        cli.main(
            [
                "import",
                "--input",
                str(link),
                "--dry-run",
                *_config(tmp_path / "destination"),
            ]
        )
        == 2
    )
    error = capsys.readouterr()
    assert "private-name" not in error.err
    assert "public-link" not in error.err
    assert json.loads(error.err)["error"]["code"] == "validation_failed"
