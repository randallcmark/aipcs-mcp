"""Pure C2a operational lifecycle policy tests."""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path
from uuid import UUID

import pytest

from aipcs_mcp.application.operational_lifecycle import (
    ArchiveCommand,
    OperationalLifecycleContractError,
    OperationalLifecycleIntent,
    OperationalLifecycleKind,
    OperationalLifecyclePhase,
    RestoreCommand,
    ResumeCommand,
    SuspendCommand,
    canonical_operational_lifecycle_json,
    deterministic_target_status,
    operational_lifecycle_fingerprint,
    prepare_operational_intent,
    transition_target_status,
)
from aipcs_mcp.lifecycle import MAX_SERVICE_REVISION

_SERVICE_ID = UUID("5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23")


def _command(kind: OperationalLifecycleKind = OperationalLifecycleKind.SUSPEND):
    fields = {
        "principal_id": "principal-a",
        "created_via": "policy-test",
        "service_id": _SERVICE_ID,
        "expected_service_revision": 7,
        "idempotency_key": "operation-1",
    }
    return {
        OperationalLifecycleKind.SUSPEND: SuspendCommand,
        OperationalLifecycleKind.RESUME: ResumeCommand,
        OperationalLifecycleKind.ARCHIVE: ArchiveCommand,
        OperationalLifecycleKind.RESTORE: RestoreCommand,
    }[kind](**fields)


def test_operational_lifecycle_module_is_structurally_pure() -> None:
    import aipcs_mcp.application.operational_lifecycle as policy

    tree = ast.parse(Path(policy.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert not any(name.startswith("aipcs_mcp.storage") for name in imported)
    assert not any(name.startswith("sqlite3") for name in imported)


def test_fingerprint_uses_exact_canonical_payload_and_excludes_idempotency_key() -> None:
    command = _command()

    assert canonical_operational_lifecycle_json(command) == (
        '{"created_via":"policy-test","expected_service_revision":7,"kind":"suspend",'
        '"principal":"principal-a","service_id":"5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23",'
        '"target_operational_status":"suspended"}'
    )
    assert operational_lifecycle_fingerprint(command) == (
        "fd840f72c18f5c26f2da09793f52af4998a69a76d234c0da21df0f00570e2558"
    )
    changed_key = SuspendCommand(
        principal_id=command.principal_id,
        created_via=command.created_via,
        service_id=command.service_id,
        expected_service_revision=command.expected_service_revision,
        idempotency_key="operation-2",
    )
    changed_revision = SuspendCommand(
        principal_id=command.principal_id,
        created_via=command.created_via,
        service_id=command.service_id,
        expected_service_revision=command.expected_service_revision + 1,
        idempotency_key=command.idempotency_key,
    )

    assert operational_lifecycle_fingerprint(changed_key) == operational_lifecycle_fingerprint(command)
    assert operational_lifecycle_fingerprint(changed_revision) != operational_lifecycle_fingerprint(command)


def test_prepare_intent_is_immutable_and_finalizes_once() -> None:
    intent = prepare_operational_intent(_command(OperationalLifecycleKind.ARCHIVE))

    assert intent.phase is OperationalLifecyclePhase.PREPARED
    assert intent.target_operational_status == "archived"
    with pytest.raises(FrozenInstanceError):
        intent.fingerprint = "0" * 64  # type: ignore[misc]

    completed = intent.with_phase(OperationalLifecyclePhase.COMPLETED)
    recovered = intent.with_phase(OperationalLifecyclePhase.RECOVERY_REQUIRED)
    assert completed.phase is OperationalLifecyclePhase.COMPLETED
    assert recovered.phase is OperationalLifecyclePhase.RECOVERY_REQUIRED
    for terminal in (completed, recovered):
        with pytest.raises(OperationalLifecycleContractError):
            terminal.with_phase(OperationalLifecyclePhase.COMPLETED)
        with pytest.raises(OperationalLifecycleContractError):
            terminal.with_phase(OperationalLifecyclePhase.PREPARED)


def test_intent_rejects_changed_or_malformed_fingerprints_and_phases() -> None:
    command = _command()
    fingerprint = operational_lifecycle_fingerprint(command)
    fields = {
        "kind": command.kind,
        "phase": OperationalLifecyclePhase.PREPARED,
        "principal_id": command.principal_id,
        "created_via": command.created_via,
        "service_id": command.service_id,
        "expected_service_revision": command.expected_service_revision,
        "target_operational_status": "suspended",
        "idempotency_key": command.idempotency_key,
    }

    with pytest.raises(OperationalLifecycleContractError):
        OperationalLifecycleIntent(fingerprint="0" * 64, **fields)
    with pytest.raises(OperationalLifecycleContractError):
        OperationalLifecycleIntent(fingerprint="not-a-fingerprint", **fields)
    with pytest.raises(OperationalLifecycleContractError):
        OperationalLifecycleIntent(
            fingerprint=fingerprint,
            **{**fields, "phase": "prepared"},  # type: ignore[arg-type]
        )
    with pytest.raises(OperationalLifecycleContractError):
        OperationalLifecycleIntent(
            fingerprint=fingerprint,
            **{**fields, "kind": "suspend"},  # type: ignore[arg-type]
        )
    with pytest.raises(OperationalLifecycleContractError):
        OperationalLifecycleIntent(
            fingerprint=fingerprint,
            **{**fields, "target_operational_status": "active"},  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("command_type", "field", "value"),
    [
        (SuspendCommand, "principal_id", ""),
        (SuspendCommand, "principal_id", " principal-a"),
        (SuspendCommand, "principal_id", "x" * 129),
        (ResumeCommand, "created_via", ""),
        (ResumeCommand, "created_via", "x" * 65),
        (ArchiveCommand, "service_id", UUID(int=0)),
        (RestoreCommand, "service_id", "not-a-uuid"),
        (SuspendCommand, "expected_service_revision", 0),
        (SuspendCommand, "expected_service_revision", True),
        (SuspendCommand, "expected_service_revision", MAX_SERVICE_REVISION),
        (SuspendCommand, "idempotency_key", ""),
        (SuspendCommand, "idempotency_key", " key"),
        (SuspendCommand, "idempotency_key", "x" * 129),
    ],
)
def test_commands_reject_malformed_common_values(
    command_type: type[SuspendCommand | ResumeCommand | ArchiveCommand | RestoreCommand],
    field: str,
    value: object,
) -> None:
    fields = {
        "principal_id": "principal-a",
        "created_via": "policy-test",
        "service_id": _SERVICE_ID,
        "expected_service_revision": 1,
        "idempotency_key": "operation-1",
    }
    fields[field] = value

    with pytest.raises(OperationalLifecycleContractError):
        command_type(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("design_state", ["seeded", "materialised"])
@pytest.mark.parametrize(
    ("kind", "source", "target"),
    [
        (OperationalLifecycleKind.SUSPEND, "active", "suspended"),
        (OperationalLifecycleKind.RESUME, "suspended", "active"),
        (OperationalLifecycleKind.ARCHIVE, "active", "archived"),
        (OperationalLifecycleKind.ARCHIVE, "suspended", "archived"),
        (OperationalLifecycleKind.RESTORE, "archived", "suspended"),
    ],
)
def test_transition_matrix_applies_to_seeded_and_materialised_services(
    design_state: str, kind: OperationalLifecycleKind, source: str, target: str
) -> None:
    assert transition_target_status(_command(kind), design_state, source) == target  # type: ignore[arg-type]
    assert deterministic_target_status(kind) == target


@pytest.mark.parametrize("design_state", ["seeded", "materialised"])
@pytest.mark.parametrize("kind", list(OperationalLifecycleKind))
@pytest.mark.parametrize("source", ["active", "suspended", "archived"])
def test_transition_matrix_rejects_every_unsupported_state_pair(
    design_state: str, kind: OperationalLifecycleKind, source: str
) -> None:
    supported = {
        (OperationalLifecycleKind.SUSPEND, "active"),
        (OperationalLifecycleKind.RESUME, "suspended"),
        (OperationalLifecycleKind.ARCHIVE, "active"),
        (OperationalLifecycleKind.ARCHIVE, "suspended"),
        (OperationalLifecycleKind.RESTORE, "archived"),
    }
    if (kind, source) in supported:
        return

    with pytest.raises(OperationalLifecycleContractError):
        transition_target_status(_command(kind), design_state, source)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_design_state", ["", "draft", True])
def test_transition_policy_rejects_malformed_design_states(bad_design_state: object) -> None:
    with pytest.raises(OperationalLifecycleContractError):
        transition_target_status(_command(), bad_design_state, "active")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_status", ["", "deleted", True])
def test_transition_policy_rejects_malformed_operational_statuses(bad_status: object) -> None:
    with pytest.raises(OperationalLifecycleContractError):
        transition_target_status(_command(), "seeded", bad_status)  # type: ignore[arg-type]
