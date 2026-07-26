"""SYNTHETIC_FIXTURE. V1-11 destructive purge administration behavior."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from aipcs_mcp import cli
from aipcs_mcp.admin_cli import (
    CliExecutionMode,
    CliOutputFormat,
    ServicePurgeInvocation,
)
from aipcs_mcp.application.admin import SafeMigrationState
from aipcs_mcp.application.models import ApplicationContext, SeedCommand
from aipcs_mcp.application.portable import (
    PortableExecutionResult,
    PortableResultCategory,
)
from aipcs_mcp.application.registry_authority import (
    PurgeAuthority,
    PurgeAuthorityKind,
    PurgeCommand,
    PurgeTombstone,
)
from aipcs_mcp.application.services import ServiceApplication
from aipcs_mcp.contracts import ServiceMetadata
from aipcs_mcp.errors import AipcsContractError, ErrorCode
from aipcs_mcp.runtime import SystemUtcClock, Uuid4ServiceIds
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter

SERVICE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OPERATION_ID = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 1, 1, tzinfo=UTC)
CONTEXT = ApplicationContext("principal", "cli")


def _service(status: str = "archived", revision: int = 7) -> ServiceMetadata:
    return ServiceMetadata(
        service_id=SERVICE_ID,
        domain_name="notes",
        domain_class="knowledge",
        intent_description="Keep durable notes.",
        design_state="seeded",
        operational_status=status,  # type: ignore[arg-type]
        service_revision=revision,
        recovery_state="clear",
        created_at=NOW,
        updated_at=NOW,
        last_activity_at=NOW,
    )


class Inspection:
    def registry_status(self) -> SafeMigrationState:
        return SafeMigrationState("registry", "sqlite", "ready", 4, 4, None)

    def service_inspect(
        self, context: ApplicationContext, service_id: UUID
    ) -> ServiceMetadata:
        assert context == CONTEXT
        assert service_id == SERVICE_ID
        return _service()


class Coordinator:
    def __init__(self) -> None:
        self.commands: list[PurgeCommand] = []

    def purge(self, command: PurgeCommand) -> PortableExecutionResult:
        self.commands.append(command)
        tombstone = PurgeTombstone(
            CONTEXT.principal_id,
            SERVICE_ID,
            f"svc_{SERVICE_ID.hex}",
            "cli",
            NOW,
            command.authority,
            command.authority.receipt_id,
        )
        return PortableExecutionResult(
            PortableResultCategory.COMPLETED,
            tombstone=tombstone,
        )


def _runtime() -> tuple[object, Coordinator]:
    coordinator = Coordinator()
    return (
        SimpleNamespace(
            inspection=Inspection(),
            context=CONTEXT,
            portable=lambda: coordinator,
        ),
        coordinator,
    )


def test_machine_override_purge_uses_exact_typed_command() -> None:
    runtime, coordinator = _runtime()
    invocation = ServicePurgeInvocation(
        CliOutputFormat.JSON,
        CliExecutionMode.MACHINE,
        SERVICE_ID,
        7,
        OPERATION_ID,
        None,
        True,
        SERVICE_ID,
        True,
    )

    result = cli._run_purge(runtime, invocation)

    assert result["operation"] == "purge"
    assert result["tombstone"]["service_id"] == str(SERVICE_ID)  # type: ignore[index]
    command = coordinator.commands[0]
    assert command.principal_id == CONTEXT.principal_id
    assert command.created_via == "cli"
    assert command.expected_service_revision == 7
    assert command.idempotency_key == str(OPERATION_ID)
    assert command.authority == PurgeAuthority(
        PurgeAuthorityKind.EXPLICIT_OVERRIDE
    )


def test_machine_receipt_purge_preserves_verified_receipt_authority() -> None:
    runtime, coordinator = _runtime()
    receipt_id = UUID("33333333-3333-4333-8333-333333333333")
    invocation = ServicePurgeInvocation(
        CliOutputFormat.JSON,
        CliExecutionMode.MACHINE,
        SERVICE_ID,
        7,
        OPERATION_ID,
        receipt_id,
        False,
        SERVICE_ID,
        True,
    )

    cli._run_purge(runtime, invocation)

    assert coordinator.commands[0].authority == PurgeAuthority(
        PurgeAuthorityKind.VERIFIED_RECEIPT, receipt_id
    )


def test_interactive_purge_generates_values_then_requires_exact_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, coordinator = _runtime()
    invocation = ServicePurgeInvocation(
        CliOutputFormat.HUMAN,
        CliExecutionMode.INTERACTIVE,
        SERVICE_ID,
        None,
        None,
        None,
        True,
        None,
        False,
    )
    observed: list[ServicePurgeInvocation] = []
    monkeypatch.setattr(cli, "uuid4", lambda: OPERATION_ID)
    monkeypatch.setattr(
        cli,
        "_confirm_purge",
        lambda value, status: observed.append(value) is None and status == "archived",
    )

    cli._run_purge(runtime, invocation)

    assert observed[0].expected_revision == 7
    assert observed[0].operation_id == OPERATION_ID
    assert len(coordinator.commands) == 1


def test_declined_or_mismatched_purge_never_calls_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, coordinator = _runtime()
    interactive = ServicePurgeInvocation(
        CliOutputFormat.HUMAN,
        CliExecutionMode.INTERACTIVE,
        SERVICE_ID,
        7,
        OPERATION_ID,
        None,
        True,
        None,
        False,
    )
    monkeypatch.setattr(cli, "_confirm_purge", lambda *_: False)
    with pytest.raises(AipcsContractError) as declined:
        cli._run_purge(runtime, interactive)
    assert declined.value.code is ErrorCode.INVALID_STATE
    assert coordinator.commands == []

    mismatch = ServicePurgeInvocation(
        CliOutputFormat.HUMAN,
        CliExecutionMode.INTERACTIVE,
        SERVICE_ID,
        7,
        OPERATION_ID,
        None,
        True,
        UUID("33333333-3333-4333-8333-333333333333"),
        False,
    )
    with pytest.raises(AipcsContractError) as rejected:
        cli._run_purge(runtime, mismatch)
    assert rejected.value.code is ErrorCode.VALIDATION_FAILED
    assert coordinator.commands == []


def _config(root: Path, principal: str) -> list[str]:
    return [
        "--profile",
        "sqlite",
        "--principal-id",
        principal,
        "--sqlite-data-root",
        str(root),
    ]


@pytest.mark.requires_sqlite
def test_sqlite_cli_purge_override_replay_and_active_refusal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    os.chmod(tmp_path, 0o700)
    root = tmp_path / "data"
    principal = "purge-principal"
    registry = SQLiteRegistryAdapter(SQLiteLocationPolicy.from_resolved(root, "cli"))
    assert registry.migrate().status == "ready"
    application = ServiceApplication(
        registry.open_uow, SystemUtcClock(), Uuid4ServiceIds()
    )
    context = ApplicationContext(principal, "cli")
    archived = application.seed(
        context,
        SeedCommand("archive_me", "knowledge", "Purge this.", "seed-archive"),
    )
    active = application.seed(
        context,
        SeedCommand("keep_me", "knowledge", "Keep this.", "seed-active"),
    )
    config = _config(root, principal)
    archive_id = "44444444-4444-4444-8444-444444444444"
    assert (
        cli.main(
            [
                "service",
                "archive",
                str(archived.service_id),
                "--expected-revision",
                "1",
                "--operation-id",
                archive_id,
                "--yes",
                *config,
            ]
        )
        == 0
    )
    capsys.readouterr()

    purge_id = "55555555-5555-4555-8555-555555555555"
    purge = [
        "service",
        "purge",
        str(archived.service_id),
        "--expected-revision",
        "2",
        "--operation-id",
        purge_id,
        "--override",
        "--confirm-service-id",
        str(archived.service_id),
        "--yes",
        *config,
    ]
    assert cli.main(purge) == 0
    first = json.loads(capsys.readouterr().out)["result"]
    assert first["tombstone"]["authority"] == "explicit_override"
    assert first["tombstone"]["service_id"] == str(archived.service_id)

    assert cli.main(purge) == 0
    assert json.loads(capsys.readouterr().out)["result"] == first

    assert cli.main(["service", "inspect", str(archived.service_id), *config]) == 3
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "not_found"

    active_purge = [
        "service",
        "purge",
        str(active.service_id),
        "--expected-revision",
        "1",
        "--operation-id",
        "66666666-6666-4666-8666-666666666666",
        "--override",
        "--confirm-service-id",
        str(active.service_id),
        "--yes",
        *config,
    ]
    assert cli.main(active_purge) == 3
    assert (
        json.loads(capsys.readouterr().err)["error"]["code"]
        == "unsupported_transition"
    )
