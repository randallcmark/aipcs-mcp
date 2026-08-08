"""SYNTHETIC_FIXTURE. Public seeded AIPCS guide service walkthrough."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import anyio
import pytest
from stdio_helpers import async_session, call_envelope, sqlite_parameters, successful

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "examples/agent-instructions/seeded-guide-service.md"
JSON_FENCE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)
pytestmark = pytest.mark.requires_sqlite


def guide_payloads() -> list[dict[str, object]]:
    return [json.loads(block) for block in JSON_FENCE.findall(GUIDE.read_text())]


def test_seeded_guide_is_discoverable_without_expanding_bootstrap(tmp_path: Path) -> None:
    root = tmp_path / "seeded-guide"
    os.chmod(root.parent, 0o700)
    payloads = guide_payloads()
    seed = next(payload for payload in payloads if set(payload) == {
        "domain_name", "domain_class", "intent_description", "idempotency_key"
    })
    design = next(payload for payload in payloads if set(payload) == {
        "service_id", "idempotency_key", "schema"
    })
    records = [
        payload
        for payload in payloads
        if set(payload) == {"service_id", "entity_name", "record", "idempotency_key"}
    ]
    assert len(records) == 5

    async def exercise() -> None:
        async with async_session(sqlite_parameters(root, "guide-principal")) as session:
            seeded_envelope, failed = await call_envelope(session, "aipcs_service_seed", seed)
            assert failed is False
            service_id = successful(seeded_envelope)["service_id"]

            design["service_id"] = service_id
            designed_envelope, failed = await call_envelope(session, "aipcs_service_design", design)
            assert failed is False
            designed = successful(designed_envelope)

            materialised_envelope, failed = await call_envelope(
                session,
                "aipcs_service_materialise",
                {
                    "service_id": service_id,
                    "expected_service_revision": designed["service_revision"],
                    "expected_schema_version": designed["schema_version"],
                    "idempotency_key": "materialise-aipcs-guide-v1",
                },
            )
            assert failed is False
            assert successful(materialised_envelope)["design_state"] == "materialised"

            for record in records:
                record["service_id"] = service_id
                created_envelope, failed = await call_envelope(
                    session, "aipcs_record_create", record
                )
                assert failed is False
                assert successful(created_envelope)["record"]["entity_name"] == "guidance"

        async with async_session(sqlite_parameters(root, "guide-principal")) as session:
            bootstrap_envelope, failed = await call_envelope(session, "aipcs_bootstrap", {})
            assert failed is False
            cards = successful(bootstrap_envelope)["services"]
            card = next(item for item in cards if item["service_id"] == service_id)
            assert set(card) == {
                "service_id",
                "domain_name",
                "domain_class",
                "intent",
                "state",
                "operational_status",
                "schema_version",
                "recovery_state",
                "entities",
            }
            assert "START HERE" in card["intent"]
            assert "Persist independently retrievable durable units" not in card["intent"]

            summary_envelope, failed = await call_envelope(
                session, "aipcs_service_summary", {"service_id": service_id, "sample": 0}
            )
            assert failed is False
            assert successful(summary_envelope)["affordances"][0]["entity_name"] == "guidance"

            search_envelope, failed = await call_envelope(
                session,
                "aipcs_record_search",
                {
                    "service_id": service_id,
                    "entity_name": "guidance",
                    "filters": {"subject": "start"},
                    "limit": 1,
                },
            )
            assert failed is False
            record = successful(search_envelope)["records"][0]
            assert record["fields"]["detail"].startswith("Call bootstrap.")

    anyio.run(exercise)
