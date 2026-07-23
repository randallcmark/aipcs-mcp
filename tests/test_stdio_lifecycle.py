"""SYNTHETIC_FIXTURE. Durable lifecycle proof through real stdio processes."""

from __future__ import annotations

import copy
import os
import sqlite3
from pathlib import Path
from uuid import UUID

import anyio
from fixtures import valid_manifest
from stdio_helpers import async_session, call_envelope, sqlite_parameters, successful

from aipcs_mcp.application.models import PreparedLifecycleClaim
from aipcs_mcp.lifecycle import MaterialiseCommand
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.storage.sqlite import (
    SQLiteLocationPolicy,
    SQLiteRegistryAdapter,
    SQLiteServiceStoreCatalog,
)

_TOOL_NAMES = [
    "aipcs_server_info",
    "aipcs_service_seed",
    "aipcs_service_list",
    "aipcs_service_inspect",
    "aipcs_service_design",
    "aipcs_service_materialise",
    "aipcs_service_evolve",
]
_PRIVATE_FIELDS = {
    "principal_id",
    "created_via",
    "path",
    "audit",
    "dsn",
    "idempotency_key",
    "fingerprint",
    "operation_id",
    "target_manifest",
    "phase",
    "fault",
    "sql",
}


def _secure_parent(root: Path) -> None:
    os.chmod(root.parent, 0o700)


def _seed(key: str, *, domain_name: str = "projects") -> dict[str, str]:
    return {
        "domain_name": domain_name,
        "domain_class": "project_memory",
        "intent_description": "Keep compact synthetic project context.",
        "idempotency_key": key,
    }


def _evolved_manifest() -> dict[str, object]:
    target = copy.deepcopy(valid_manifest())
    target["schema_version"] = 2
    target["migration_history"] = [
        {
            "from_schema_version": 1,
            "to_schema_version": 2,
            "operations": ["add optional summary"],
        }
    ]
    target["entities"][0]["attributes"].append({"name": "summary", "type": "string"})
    return target


def _assert_no_private_fields(value: object) -> None:
    if isinstance(value, dict):
        assert _PRIVATE_FIELDS.isdisjoint(value)
        for item in value.values():
            _assert_no_private_fields(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_private_fields(item)


def _assert_public_metadata(
    metadata: dict[str, object],
    *,
    revision: int,
    schema_version: int | None,
    materialised: bool,
) -> None:
    _assert_no_private_fields(metadata)
    assert _PRIVATE_FIELDS.isdisjoint(metadata)
    assert metadata["service_revision"] == revision
    assert metadata["recovery_state"] == "clear"
    assert metadata["schema_version"] == schema_version
    assert metadata["design_state"] == ("materialised" if materialised else "seeded")
    if materialised:
        assert isinstance(metadata["materialised_at"], str)
        assert metadata["storage"] == {
            "backend": "sqlite",
            "namespace": f"svc_{str(metadata['service_id']).replace('-', '')}",
        }
    else:
        assert metadata["materialised_at"] is None
        assert metadata["storage"] is None


def test_source_stdio_process_disables_checkout_bytecode(tmp_path: Path) -> None:
    parameters = sqlite_parameters(tmp_path / "registry", "test-principal")
    assert parameters.env is not None
    assert parameters.env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_sqlite_ready_lifecycle_and_design(tmp_path: Path) -> None:
    root = tmp_path / "aipcs-stdio-root"
    _secure_parent(root)

    async def exercise() -> None:
        async with async_session(sqlite_parameters(root, "test-principal-a")) as session:
            tools = await session.list_tools()
            assert [tool.name for tool in tools.tools] == _TOOL_NAMES

            info, failed = await call_envelope(session, "aipcs_server_info", {})
            assert failed is False
            capability = successful(info)
            assert capability["aipcs_mcp_contract"] == "1.1.0"
            assert capability["features"]["registry_lifecycle"] is True
            assert capability["features"]["materialisation_lifecycle"] is True

            seeded_envelope, failed = await call_envelope(
                session, "aipcs_service_seed", _seed("seed-a")
            )
            assert failed is False
            seeded = successful(seeded_envelope)
            _assert_public_metadata(
                seeded, revision=1, schema_version=None, materialised=False
            )
            assert seeded["schema"] is None
            assert seeded["schema_version"] is None
            service_id = seeded["service_id"]

            listed_envelope, failed = await call_envelope(
                session, "aipcs_service_list", {"limit": 100}
            )
            assert failed is False
            listed = successful(listed_envelope)
            assert listed == {"services": [seeded]}

            inspected_envelope, failed = await call_envelope(
                session, "aipcs_service_inspect", {"service_id": service_id}
            )
            assert failed is False
            assert successful(inspected_envelope) == seeded

            rejected_manifest = copy.deepcopy(valid_manifest())
            rejected_manifest["indices"][0]["name"] = "sqlite_project_owner_idx"
            rejected_design, failed = await call_envelope(
                session,
                "aipcs_service_design",
                {
                    "service_id": service_id,
                    "schema": rejected_manifest,
                    "idempotency_key": "design-a",
                },
            )
            assert failed is True
            assert rejected_design["error"]["code"] == "validation_failed"
            unchanged, failed = await call_envelope(
                session, "aipcs_service_inspect", {"service_id": service_id}
            )
            assert failed is False
            assert successful(unchanged) == seeded

            designed_envelope, failed = await call_envelope(
                session,
                "aipcs_service_design",
                {
                    "service_id": service_id,
                    "schema": copy.deepcopy(valid_manifest()),
                    "idempotency_key": "design-a",
                },
            )
            assert failed is False
            designed = successful(designed_envelope)
            _assert_public_metadata(
                designed, revision=2, schema_version=1, materialised=False
            )
            assert designed["service_id"] == service_id
            assert designed["schema_version"] == 1
            assert designed["schema"] == ManifestV2.model_validate(valid_manifest()).model_dump(
                mode="json", by_alias=True
            )

            materialise = {
                "service_id": service_id,
                "expected_service_revision": 2,
                "expected_schema_version": 1,
                "idempotency_key": "materialise-a",
            }
            materialised_envelope, failed = await call_envelope(
                session, "aipcs_service_materialise", materialise
            )
            assert failed is False
            materialised = successful(materialised_envelope)
            _assert_public_metadata(
                materialised, revision=3, schema_version=1, materialised=True
            )
            replayed_materialise, failed = await call_envelope(
                session, "aipcs_service_materialise", materialise
            )
            assert failed is False
            assert replayed_materialise == materialised_envelope

            target = _evolved_manifest()
            evolve = {
                "service_id": service_id,
                "expected_service_revision": 3,
                "expected_schema_version": 1,
                "idempotency_key": "evolve-a",
                "schema": target,
            }
            evolved_envelope, failed = await call_envelope(
                session, "aipcs_service_evolve", evolve
            )
            assert failed is False
            evolved = successful(evolved_envelope)
            _assert_public_metadata(
                evolved, revision=4, schema_version=2, materialised=True
            )
            assert evolved["schema"] == ManifestV2.model_validate(target).model_dump(
                mode="json", by_alias=True
            )
            assert evolved["materialised_at"] == materialised["materialised_at"]
            assert evolved["storage"] == materialised["storage"]
            replayed_evolve, failed = await call_envelope(
                session, "aipcs_service_evolve", evolve
            )
            assert failed is False
            assert replayed_evolve == evolved_envelope

            changed_target = copy.deepcopy(target)
            changed_target["retrieval_guidance"] = "Changed synthetic guidance."
            changed_fingerprint, failed = await call_envelope(
                session,
                "aipcs_service_evolve",
                dict(evolve, schema=changed_target),
            )
            assert failed is True
            assert changed_fingerprint["error"]["code"] == "changed_fingerprint"
            assert changed_fingerprint["error"]["retryable"] is False

            stale, failed = await call_envelope(
                session,
                "aipcs_service_evolve",
                dict(evolve, idempotency_key="stale-evolve-a"),
            )
            assert failed is True
            assert stale["error"]["code"] == "stale_revision"
            assert stale["error"]["retryable"] is False

            listed_after, failed = await call_envelope(
                session, "aipcs_service_list", {"limit": 100}
            )
            assert failed is False
            assert successful(listed_after) == {"services": [evolved]}
            inspected_after, failed = await call_envelope(
                session, "aipcs_service_inspect", {"service_id": service_id}
            )
            assert failed is False
            assert successful(inspected_after) == evolved

    anyio.run(exercise)


def test_restart_replay_and_conflict(tmp_path: Path) -> None:
    root = tmp_path / "aipcs-restart-root"
    _secure_parent(root)
    seed = _seed("seed-replay")
    manifest = valid_manifest()

    async def exercise() -> None:
        async with async_session(sqlite_parameters(root, "test-principal-replay")) as first:
            seeded_envelope, failed = await call_envelope(first, "aipcs_service_seed", seed)
            assert failed is False
            seeded = successful(seeded_envelope)
            service_id = seeded["service_id"]
            design = {
                "service_id": service_id,
                "schema": manifest,
                "idempotency_key": "design-replay",
            }
            designed_envelope, failed = await call_envelope(first, "aipcs_service_design", design)
            assert failed is False
            designed = successful(designed_envelope)
            materialise = {
                "service_id": service_id,
                "expected_service_revision": 2,
                "expected_schema_version": 1,
                "idempotency_key": "materialise-replay",
            }
            materialised_envelope, failed = await call_envelope(
                first, "aipcs_service_materialise", materialise
            )
            assert failed is False
            materialised = successful(materialised_envelope)

        async with async_session(sqlite_parameters(root, "test-principal-replay")) as second:
            replay_seed, failed = await call_envelope(second, "aipcs_service_seed", seed)
            assert failed is False
            assert successful(replay_seed) == seeded
            replay_design, failed = await call_envelope(second, "aipcs_service_design", design)
            assert failed is False
            assert successful(replay_design) == designed
            replay_materialise, failed = await call_envelope(
                second, "aipcs_service_materialise", materialise
            )
            assert failed is False
            assert successful(replay_materialise) == materialised

            target = _evolved_manifest()
            evolve = {
                "service_id": service_id,
                "expected_service_revision": 3,
                "expected_schema_version": 1,
                "idempotency_key": "evolve-replay",
                "schema": target,
            }
            evolved_envelope, failed = await call_envelope(
                second, "aipcs_service_evolve", evolve
            )
            assert failed is False
            evolved = successful(evolved_envelope)

            changed_seed = dict(seed, intent_description="Changed synthetic project intent.")
            conflict, failed = await call_envelope(second, "aipcs_service_seed", changed_seed)
            assert failed is True
            assert conflict["error"]["code"] == "conflict"

            changed_manifest = copy.deepcopy(manifest)
            changed_manifest["retrieval_guidance"] = "Changed synthetic instruction."
            changed_design = dict(design, schema=changed_manifest)
            conflict, failed = await call_envelope(second, "aipcs_service_design", changed_design)
            assert failed is True
            assert conflict["error"]["code"] == "conflict"

            listed, failed = await call_envelope(second, "aipcs_service_list", {"limit": 100})
            assert failed is False
            assert successful(listed) == {"services": [evolved]}
            inspected, failed = await call_envelope(
                second, "aipcs_service_inspect", {"service_id": service_id}
            )
            assert failed is False
            assert successful(inspected) == evolved

        async with async_session(sqlite_parameters(root, "test-principal-replay")) as third:
            replay_evolve, failed = await call_envelope(
                third, "aipcs_service_evolve", evolve
            )
            assert failed is False
            assert successful(replay_evolve) == evolved
            inspected, failed = await call_envelope(
                third, "aipcs_service_inspect", {"service_id": service_id}
            )
            assert failed is False
            assert successful(inspected) == evolved

    anyio.run(exercise)


def test_public_pending_and_recovery_required_projection(tmp_path: Path) -> None:
    root = tmp_path / "aipcs-recovery-root"
    principal = "test-principal-recovery"
    _secure_parent(root)

    async def prepare_service() -> tuple[str, dict[str, object]]:
        async with async_session(sqlite_parameters(root, principal)) as session:
            seeded_envelope, failed = await call_envelope(
                session, "aipcs_service_seed", _seed("seed-recovery")
            )
            assert failed is False
            service_id = successful(seeded_envelope)["service_id"]
            designed_envelope, failed = await call_envelope(
                session,
                "aipcs_service_design",
                {
                    "service_id": service_id,
                    "schema": valid_manifest(),
                    "idempotency_key": "design-recovery",
                },
            )
            assert failed is False
            return service_id, successful(designed_envelope)

    service_id, designed = anyio.run(prepare_service)
    location = SQLiteLocationPolicy(root)
    registry = SQLiteRegistryAdapter(location)
    uow = registry.open_uow()
    command = MaterialiseCommand(
        principal_id=principal,
        created_via="mcp",
        service_id=UUID(service_id),
        expected_service_revision=2,
        expected_schema_version=1,
        idempotency_key="materialise-recovery",
    )
    try:
        assert isinstance(uow.mutations.resolve_or_admit(command), PreparedLifecycleClaim)
        uow.commit()
    finally:
        uow.close()

    catalog = SQLiteServiceStoreCatalog(location)
    locator = catalog.allocate(UUID(service_id))
    assert catalog.migrate(locator).status == "ready"
    database = root / "service-stores" / f"{locator.namespace}.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE "unexpected" ("id" INTEGER PRIMARY KEY) STRICT')

    async def exercise_recovery() -> None:
        async with async_session(sqlite_parameters(root, principal)) as session:
            pending_envelope, failed = await call_envelope(
                session, "aipcs_service_inspect", {"service_id": service_id}
            )
            assert failed is False
            pending = successful(pending_envelope)
            assert pending["service_revision"] == 2
            assert pending["recovery_state"] == "pending"
            assert pending["materialised_at"] is None
            assert pending["storage"] is None

            recovery, failed = await call_envelope(
                session,
                "aipcs_service_materialise",
                {
                    "service_id": service_id,
                    "expected_service_revision": 2,
                    "expected_schema_version": 1,
                    "idempotency_key": "materialise-recovery",
                },
            )
            assert failed is True
            assert recovery["result"] is None
            assert recovery["error"]["code"] == "recovery_required"
            assert recovery["error"]["retryable"] is False
            _assert_no_private_fields(recovery)

            terminal_envelope, failed = await call_envelope(
                session, "aipcs_service_inspect", {"service_id": service_id}
            )
            assert failed is False
            terminal = successful(terminal_envelope)
            assert terminal["service_revision"] == 2
            assert terminal["recovery_state"] == "recovery_required"
            assert terminal["schema"] == designed["schema"]
            assert terminal["materialised_at"] is None
            assert terminal["storage"] is None

    anyio.run(exercise_recovery)


def test_second_principal_isolated_and_can_reuse_key(tmp_path: Path) -> None:
    root = tmp_path / "aipcs-principal-root"
    _secure_parent(root)
    key = "shared-key"

    async def exercise() -> None:
        async with async_session(sqlite_parameters(root, "test-principal-one")) as first:
            first_seed, failed = await call_envelope(first, "aipcs_service_seed", _seed(key))
            assert failed is False
            first_metadata = successful(first_seed)
            first_id = first_metadata["service_id"]

        async with async_session(sqlite_parameters(root, "test-principal-two")) as second:
            listed, failed = await call_envelope(second, "aipcs_service_list", {"limit": 100})
            assert failed is False
            assert successful(listed) == {"services": []}
            missing, failed = await call_envelope(
                second, "aipcs_service_inspect", {"service_id": first_id}
            )
            assert failed is True
            assert missing["error"]["code"] == "not_found"

            second_seed, failed = await call_envelope(
                second, "aipcs_service_seed", _seed(key, domain_name="notes")
            )
            assert failed is False
            second_metadata = successful(second_seed)
            assert second_metadata["service_id"] != first_id
            rejected, failed = await call_envelope(
                second,
                "aipcs_service_seed",
                dict(_seed("private-input"), principal_id="caller-control"),
            )
            assert failed is True
            assert rejected["error"]["code"] == "validation_failed"
            rejected, failed = await call_envelope(
                second,
                "aipcs_service_seed",
                dict(_seed("provenance-input"), created_via="caller-control"),
            )
            assert failed is True
            assert rejected["error"]["code"] == "validation_failed"

        async with async_session(sqlite_parameters(root, "test-principal-one")) as first_restart:
            listed, failed = await call_envelope(
                first_restart, "aipcs_service_list", {"limit": 100}
            )
            assert failed is False
            assert successful(listed) == {"services": [first_metadata]}
        async with async_session(sqlite_parameters(root, "test-principal-two")) as second_restart:
            listed, failed = await call_envelope(
                second_restart, "aipcs_service_list", {"limit": 100}
            )
            assert failed is False
            assert successful(listed) == {"services": [second_metadata]}

    anyio.run(exercise)
