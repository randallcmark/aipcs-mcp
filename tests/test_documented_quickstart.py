"""SYNTHETIC_FIXTURE. Synthetic fixture provenance: public quickstart contract."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import anyio
from stdio_helpers import async_session, call_envelope, sqlite_parameters, successful

ROOT = Path(__file__).resolve().parents[1]
JSON_FENCE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


def documented_payload(required_keys: set[str]) -> dict[str, object]:
    blocks = [
        json.loads(block)
        for block in JSON_FENCE.findall((ROOT / "docs/quickstart.md").read_text())
    ]
    return next(block for block in blocks if set(block) == required_keys)


def test_documented_quickstart_persists_a_record_across_restart(tmp_path: Path) -> None:
    root = tmp_path / "documented-quickstart"
    os.chmod(root.parent, 0o700)
    seed = documented_payload(
        {"domain_name", "domain_class", "intent_description", "idempotency_key"}
    )
    design = documented_payload({"service_id", "idempotency_key", "schema"})
    materialise = documented_payload(
        {
            "service_id",
            "expected_service_revision",
            "expected_schema_version",
            "idempotency_key",
        }
    )
    create = documented_payload({"service_id", "entity_name", "record", "idempotency_key"})
    listed = documented_payload({"service_id", "entity_name", "limit"})

    async def exercise() -> None:
        async with async_session(sqlite_parameters(root, "quickstart-principal")) as session:
            seeded_envelope, failed = await call_envelope(
                session, "aipcs_service_seed", seed
            )
            assert failed is False
            service_id = successful(seeded_envelope)["service_id"]

            design["service_id"] = service_id
            designed_envelope, failed = await call_envelope(
                session, "aipcs_service_design", design
            )
            assert failed is False
            designed = successful(designed_envelope)

            materialise["service_id"] = service_id
            materialise["expected_service_revision"] = designed["service_revision"]
            materialise["expected_schema_version"] = designed["schema_version"]
            materialised_envelope, failed = await call_envelope(
                session, "aipcs_service_materialise", materialise
            )
            assert failed is False
            assert successful(materialised_envelope)["design_state"] == "materialised"

            create["service_id"] = service_id
            created_envelope, failed = await call_envelope(
                session, "aipcs_record_create", create
            )
            assert failed is False
            created = successful(created_envelope)["record"]

        async with async_session(sqlite_parameters(root, "quickstart-principal")) as session:
            listed["service_id"] = service_id
            list_envelope, failed = await call_envelope(
                session, "aipcs_record_list", listed
            )
            assert failed is False
            records = successful(list_envelope)["records"]
            assert records == [created]

    anyio.run(exercise)
