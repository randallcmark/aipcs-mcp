"""SYNTHETIC_FIXTURE. Test-only closed-registry audit durability inspection."""

from __future__ import annotations

import copy
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import anyio
from fixtures import valid_manifest
from stdio_helpers import async_session, call_envelope, sqlite_parameters, successful


def _secure_parent(root: Path) -> None:
    os.chmod(root.parent, 0o700)


def _seed(key: str, domain_name: str) -> dict[str, str]:
    return {
        "domain_name": domain_name,
        "domain_class": "synthetic",
        "intent_description": "Synthetic audit durability test.",
        "idempotency_key": key,
    }


def _audit_rows(database: Path) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(database)
    try:
        return connection.execute(
            'SELECT "action","outcome","service_id","principal_id","created_via","at" '
            'FROM "aipcs_registry_audit" ORDER BY "audit_id"'
        ).fetchall()
    finally:
        connection.close()


def _mutation_rows(database: Path) -> list[tuple[str, str, str]]:
    connection = sqlite3.connect(database)
    try:
        return connection.execute(
            'SELECT "principal_id","idempotency_key","service_id" '
                'FROM "aipcs_registry_claim" ORDER BY "principal_id","idempotency_key"'
        ).fetchall()
    finally:
        connection.close()


def test_lifecycle_audit_survives_restart_without_public_exposure(tmp_path: Path) -> None:
    root = tmp_path / "audit-root"
    _secure_parent(root)
    database = root / "registry.sqlite"
    principal_one = "audit-principal-one"
    principal_two = "audit-principal-two"
    seed = _seed("shared-key", "audit_projects")
    public_payloads: list[dict[str, object]] = []

    async def exercise() -> None:
        async with async_session(sqlite_parameters(root, principal_one)) as first:
            seeded_envelope, failed = await call_envelope(first, "aipcs_service_seed", seed)
            assert failed is False
            seeded = successful(seeded_envelope)
            public_payloads.append(seeded)
            designed_envelope, failed = await call_envelope(
                first,
                "aipcs_service_design",
                {
                    "service_id": seeded["service_id"],
                    "schema": copy.deepcopy(valid_manifest()),
                    "idempotency_key": "design-key",
                },
            )
            assert failed is False
            designed = successful(designed_envelope)
            public_payloads.append(designed)

        initial_audit = _audit_rows(database)
        assert [(row[0], row[1]) for row in initial_audit] == [
            ("seed", "created"),
            ("design", "accepted"),
        ]
        assert all(row[2] == seeded["service_id"] for row in initial_audit)
        assert all(row[3] == principal_one and row[4] == "mcp" for row in initial_audit)
        for row in initial_audit:
            assert (
                datetime.fromisoformat(str(row[5]).replace("Z", "+00:00")).utcoffset() is not None
            )
        assert _mutation_rows(database) == [
            (principal_one, "design-key", seeded["service_id"]),
            (principal_one, "shared-key", seeded["service_id"]),
        ]

        async with async_session(sqlite_parameters(root, principal_one)) as replay:
            replay_seed, failed = await call_envelope(replay, "aipcs_service_seed", seed)
            assert failed is False
            assert successful(replay_seed) == seeded
            replay_design, failed = await call_envelope(
                replay,
                "aipcs_service_design",
                {
                    "service_id": seeded["service_id"],
                    "schema": copy.deepcopy(valid_manifest()),
                    "idempotency_key": "design-key",
                },
            )
            assert failed is False
            assert successful(replay_design) == designed

        assert _audit_rows(database) == initial_audit
        async with async_session(sqlite_parameters(root, principal_two)) as second_principal:
            second_seed, failed = await call_envelope(
                second_principal, "aipcs_service_seed", _seed("shared-key", "audit_notes")
            )
            assert failed is False
            public_payloads.append(successful(second_seed))

    anyio.run(exercise)
    audit = _audit_rows(database)
    assert [(row[0], row[1], row[3], row[4]) for row in audit] == [
        ("seed", "created", principal_one, "mcp"),
        ("design", "accepted", principal_one, "mcp"),
        ("seed", "created", principal_two, "mcp"),
    ]
    mutations = _mutation_rows(database)
    assert len(mutations) == 3
    assert (principal_two, "shared-key", audit[-1][2]) in mutations
    for payload in public_payloads:
        assert {"audit", "principal_id", "created_via", "provenance"}.isdisjoint(payload)
