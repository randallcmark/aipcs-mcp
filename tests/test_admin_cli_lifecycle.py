"""SYNTHETIC_FIXTURE. V1-11 lifecycle administration behavior."""

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
    CliLifecycleAction,
    CliOutputFormat,
    ServiceLifecycleInvocation,
)
from aipcs_mcp.application.admin import SafeMigrationState
from aipcs_mcp.application.models import ApplicationContext, SeedCommand
from aipcs_mcp.application.operational_lifecycle import (
    ArchiveCommand,
    RestoreCommand,
    ResumeCommand,
    SuspendCommand,
)
from aipcs_mcp.application.portable import (
    PortableExecutionResult,
    PortableResultCategory,
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


def _service(status: str, revision: int) -> ServiceMetadata:
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
    def __init__(self, current: ServiceMetadata, completed: ServiceMetadata) -> None:
        self.current = current
        self.completed = completed
        self.inspect_calls = 0
        self.transitioned = False

    def registry_status(self) -> SafeMigrationState:
        return SafeMigrationState("registry", "sqlite", "ready", 4, 4, None)

    def service_inspect(
        self, context: ApplicationContext, service_id: UUID
    ) -> ServiceMetadata:
        assert context == CONTEXT
        assert service_id == SERVICE_ID
        self.inspect_calls += 1
        return self.completed if self.transitioned else self.current


class Coordinator:
    def __init__(
        self, result: PortableExecutionResult, inspection: Inspection | None = None
    ) -> None:
        self.result = result
        self.inspection = inspection
        self.commands: list[object] = []

    def transition(self, command: object) -> PortableExecutionResult:
        self.commands.append(command)
        if (
            self.inspection is not None
            and self.result.category is PortableResultCategory.COMPLETED
        ):
            self.inspection.transitioned = True
        return self.result


def _runtime(
    current: ServiceMetadata,
    completed: ServiceMetadata,
    result: PortableExecutionResult | None = None,
) -> tuple[object, Coordinator, Inspection]:
    inspection = Inspection(current, completed)
    coordinator = Coordinator(
        result or PortableExecutionResult(PortableResultCategory.COMPLETED),
        inspection,
    )
    runtime = SimpleNamespace(
        inspection=inspection,
        context=CONTEXT,
        portable=lambda: coordinator,
    )
    return runtime, coordinator, inspection


@pytest.mark.parametrize(
    ("action", "current", "target", "command_type"),
    [
        (CliLifecycleAction.SUSPEND, "active", "suspended", SuspendCommand),
        (CliLifecycleAction.RESUME, "suspended", "active", ResumeCommand),
        (CliLifecycleAction.ARCHIVE, "suspended", "archived", ArchiveCommand),
        (CliLifecycleAction.RESTORE, "archived", "suspended", RestoreCommand),
    ],
)
def test_machine_lifecycle_calls_exact_typed_transition(
    monkeypatch: pytest.MonkeyPatch,
    action: CliLifecycleAction,
    current: str,
    target: str,
    command_type: type[object],
) -> None:
    runtime, coordinator, _ = _runtime(_service(current, 7), _service(target, 8))
    invocation = ServiceLifecycleInvocation(
        CliOutputFormat.JSON,
        CliExecutionMode.MACHINE,
        action,
        SERVICE_ID,
        7,
        OPERATION_ID,
        action is CliLifecycleAction.ARCHIVE,
    )
    monkeypatch.setattr(
        cli,
        "_confirm_lifecycle",
        lambda *_: (_ for _ in ()).throw(
            AssertionError("Machine lifecycle must not prompt.")
        ),
    )

    result = cli._run_lifecycle(runtime, invocation)

    assert result["operation"] == action.value
    assert result["operation_id"] == str(OPERATION_ID)
    assert result["service"]["operational_status"] == target  # type: ignore[index]
    assert len(coordinator.commands) == 1
    command = coordinator.commands[0]
    assert type(command) is command_type
    assert command.principal_id == "principal"  # type: ignore[attr-defined]
    assert command.created_via == "cli"  # type: ignore[attr-defined]
    assert command.expected_service_revision == 7  # type: ignore[attr-defined]
    assert command.idempotency_key == str(OPERATION_ID)  # type: ignore[attr-defined]


def test_interactive_missing_values_are_displayed_and_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, coordinator, inspection = _runtime(
        _service("active", 9), _service("suspended", 10)
    )
    invocation = ServiceLifecycleInvocation(
        CliOutputFormat.HUMAN,
        CliExecutionMode.INTERACTIVE,
        CliLifecycleAction.SUSPEND,
        SERVICE_ID,
        None,
        None,
        False,
    )
    observed: list[ServiceLifecycleInvocation] = []
    monkeypatch.setattr(cli, "uuid4", lambda: OPERATION_ID)
    monkeypatch.setattr(
        cli,
        "_confirm_lifecycle",
        lambda value, status: observed.append(value) is None and status == "active",
    )

    result = cli._run_lifecycle(runtime, invocation)

    assert result["operation_id"] == str(OPERATION_ID)
    assert observed[0].expected_revision == 9
    assert observed[0].operation_id == OPERATION_ID
    assert inspection.inspect_calls == 2
    assert len(coordinator.commands) == 1


def test_declined_interactive_lifecycle_never_constructs_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspection = Inspection(_service("active", 2), _service("suspended", 3))
    runtime = SimpleNamespace(
        inspection=inspection,
        context=CONTEXT,
        portable=lambda: (_ for _ in ()).throw(
            AssertionError("Declined operation must not compose the coordinator.")
        ),
    )
    invocation = ServiceLifecycleInvocation(
        CliOutputFormat.HUMAN,
        CliExecutionMode.INTERACTIVE,
        CliLifecycleAction.SUSPEND,
        SERVICE_ID,
        None,
        None,
        False,
    )
    monkeypatch.setattr(cli, "uuid4", lambda: OPERATION_ID)
    monkeypatch.setattr(cli, "_confirm_lifecycle", lambda *_: False)

    with pytest.raises(AipcsContractError) as captured:
        cli._run_lifecycle(runtime, invocation)

    assert captured.value.code is ErrorCode.INVALID_STATE


@pytest.mark.parametrize(
    ("category", "code", "retryable"),
    [
        (PortableResultCategory.STALE_REVISION, ErrorCode.STALE_REVISION, False),
        (
            PortableResultCategory.UNSUPPORTED_TRANSITION,
            ErrorCode.UNSUPPORTED_TRANSITION,
            False,
        ),
        (
            PortableResultCategory.OPERATION_IN_PROGRESS,
            ErrorCode.OPERATION_IN_PROGRESS,
            True,
        ),
        (
            PortableResultCategory.RECOVERY_REQUIRED,
            ErrorCode.RECOVERY_REQUIRED,
            False,
        ),
        (
            PortableResultCategory.OPERATION_UNCERTAIN,
            ErrorCode.OPERATION_UNCERTAIN,
            True,
        ),
        (PortableResultCategory.STORAGE_BUSY, ErrorCode.STORAGE_BUSY, True),
        (
            PortableResultCategory.STORAGE_UNAVAILABLE,
            ErrorCode.STORAGE_UNAVAILABLE,
            False,
        ),
    ],
)
def test_lifecycle_result_categories_map_to_stable_errors(
    category: PortableResultCategory,
    code: ErrorCode,
    retryable: bool,
) -> None:
    runtime, _, _ = _runtime(
        _service("active", 7),
        _service("active", 7),
        PortableExecutionResult(category),
    )
    invocation = ServiceLifecycleInvocation(
        CliOutputFormat.JSON,
        CliExecutionMode.MACHINE,
        CliLifecycleAction.SUSPEND,
        SERVICE_ID,
        7,
        OPERATION_ID,
        False,
    )

    with pytest.raises(AipcsContractError) as captured:
        cli._run_lifecycle(runtime, invocation)

    assert captured.value.code is code
    assert captured.value.retryable is retryable


def test_lifecycle_failure_mapping_covers_every_portable_category() -> None:
    assert set(cli._PORTABLE_FAILURES) == set(PortableResultCategory)


@pytest.mark.requires_sqlite
def test_sqlite_cli_lifecycle_replay_stale_and_exact_transitions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    os.chmod(tmp_path, 0o700)
    root = tmp_path / "data"
    location = SQLiteLocationPolicy.from_resolved(root, "cli")
    registry = SQLiteRegistryAdapter(location)
    assert registry.migrate().status == "ready"
    application = ServiceApplication(
        registry.open_uow, SystemUtcClock(), Uuid4ServiceIds()
    )
    service = application.seed(
        CONTEXT,
        SeedCommand("notes", "knowledge", "Keep durable notes.", "seed-operation"),
    )
    common = [
        "--profile",
        "sqlite",
        "--principal-id",
        CONTEXT.principal_id,
        "--sqlite-data-root",
        str(root),
    ]

    suspend = [
        "service",
        "suspend",
        str(service.service_id),
        "--expected-revision",
        "1",
        "--operation-id",
        str(OPERATION_ID),
        *common,
    ]
    assert cli.main(suspend) == 0
    first = json.loads(capsys.readouterr().out)["result"]
    assert first["service"]["operational_status"] == "suspended"
    assert first["service"]["service_revision"] == 2

    assert cli.main(suspend) == 0
    replay = json.loads(capsys.readouterr().out)["result"]
    assert replay == first

    stale = [
        "service",
        "resume",
        str(service.service_id),
        "--expected-revision",
        "1",
        "--operation-id",
        "33333333-3333-4333-8333-333333333333",
        *common,
    ]
    assert cli.main(stale) == 3
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "stale_revision"

    archive = [
        "service",
        "archive",
        str(service.service_id),
        "--expected-revision",
        "2",
        "--operation-id",
        "44444444-4444-4444-8444-444444444444",
        "--yes",
        *common,
    ]
    assert cli.main(archive) == 0
    archived = json.loads(capsys.readouterr().out)["result"]
    assert archived["service"]["operational_status"] == "archived"
    assert archived["service"]["service_revision"] == 3

    restore = [
        "service",
        "restore",
        str(service.service_id),
        "--expected-revision",
        "3",
        "--operation-id",
        "55555555-5555-4555-8555-555555555555",
        *common,
    ]
    assert cli.main(restore) == 0
    restored = json.loads(capsys.readouterr().out)["result"]
    assert restored["service"]["operational_status"] == "suspended"
    assert restored["service"]["service_revision"] == 4
