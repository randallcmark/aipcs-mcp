"""SYNTHETIC_FIXTURE. Durable lifecycle proof through real stdio processes."""

from __future__ import annotations

import copy
import os
from pathlib import Path

import anyio
from fixtures import valid_manifest
from stdio_helpers import async_session, call_envelope, sqlite_parameters, successful

from aipcs_mcp.manifest_v2 import ManifestV2

_TOOL_NAMES = [
    "aipcs_server_info",
    "aipcs_service_seed",
    "aipcs_service_list",
    "aipcs_service_inspect",
    "aipcs_service_design",
]
_PRIVATE_FIELDS = {"principal_id", "created_via", "backend", "path", "audit", "dsn"}


def _secure_parent(root: Path) -> None:
    os.chmod(root.parent, 0o700)


def _seed(key: str, *, domain_name: str = "projects") -> dict[str, str]:
    return {
        "domain_name": domain_name,
        "domain_class": "project_memory",
        "intent_description": "Keep compact synthetic project context.",
        "idempotency_key": key,
    }


def _assert_public_metadata(metadata: dict[str, object]) -> None:
    assert _PRIVATE_FIELDS.isdisjoint(metadata)
    assert metadata["design_state"] == "seeded"
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
            assert successful(info)["features"]["registry_lifecycle"] is True

            seeded_envelope, failed = await call_envelope(
                session, "aipcs_service_seed", _seed("seed-a")
            )
            assert failed is False
            seeded = successful(seeded_envelope)
            _assert_public_metadata(seeded)
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
            _assert_public_metadata(designed)
            assert designed["service_id"] == service_id
            assert designed["schema_version"] == 1
            assert designed["schema"] == ManifestV2.model_validate(valid_manifest()).model_dump(
                mode="json", by_alias=True
            )

            listed_after, failed = await call_envelope(
                session, "aipcs_service_list", {"limit": 100}
            )
            assert failed is False
            assert successful(listed_after) == {"services": [designed]}
            inspected_after, failed = await call_envelope(
                session, "aipcs_service_inspect", {"service_id": service_id}
            )
            assert failed is False
            assert successful(inspected_after) == designed

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

        async with async_session(sqlite_parameters(root, "test-principal-replay")) as second:
            replay_seed, failed = await call_envelope(second, "aipcs_service_seed", seed)
            assert failed is False
            assert successful(replay_seed) == seeded
            replay_design, failed = await call_envelope(second, "aipcs_service_design", design)
            assert failed is False
            assert successful(replay_design) == designed

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
            assert successful(listed) == {"services": [designed]}
            inspected, failed = await call_envelope(
                second, "aipcs_service_inspect", {"service_id": service_id}
            )
            assert failed is False
            assert successful(inspected) == designed

    anyio.run(exercise)


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
