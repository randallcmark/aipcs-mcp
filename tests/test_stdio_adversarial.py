"""SYNTHETIC_FIXTURE. Real stdio adversarial-boundary coverage."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import anyio
import pytest
from stdio_helpers import (
    assert_no_secrets,
    async_session,
    call_envelope,
    close_process,
    raw_initialize,
    read_json_until_id,
    send_line,
    spawn_raw,
    sqlite_parameters,
)


def _secure_parent(root: Path) -> None:
    os.chmod(root.parent, 0o700)


def _seed() -> dict[str, str]:
    return {
        "domain_name": "adversarial",
        "domain_class": "synthetic",
        "intent_description": "Synthetic boundary test.",
        "idempotency_key": "adversarial-key",
    }


@pytest.mark.parametrize(
    ("name", "arguments", "code"),
    [
        ("aipcs_service_seed", {}, "validation_failed"),
        ("aipcs_service_seed", {**_seed(), "unknown": "payload-sentinel"}, "validation_failed"),
        ("aipcs_service_seed", {"payload": '{"domain_name":"not-flat"}'}, "validation_failed"),
        ("aipcs_service_seed", {**_seed(), "domain_class": "x" * 65}, "validation_failed"),
        ("aipcs_service_seed", {**_seed(), "idempotency_key": "k" * 129}, "validation_failed"),
        ("aipcs_service_list", {"limit": True}, "validation_failed"),
        ("aipcs_service_list", {"limit": "1"}, "validation_failed"),
        (
            "aipcs_service_inspect",
            {"service_id": "00000000-0000-0000-0000-000000000000"},
            "invalid_identifier",
        ),
        ("aipcs_service_inspect", {"service_id": "not-a-canonical-uuid"}, "invalid_identifier"),
        ("aipcs_unknown_operation", {}, "unsupported_operation"),
    ],
)
def test_tool_arguments_safe_envelopes(
    tmp_path: Path, name: str, arguments: dict[str, Any], code: str
) -> None:
    root = tmp_path / "secret-root-component"
    _secure_parent(root)

    async def exercise() -> None:
        async with async_session(sqlite_parameters(root, "secret-principal")) as session:
            envelope, failed = await call_envelope(session, name, arguments)
            assert failed is True
            assert envelope["ok"] is False
            assert envelope["result"] is None
            assert envelope["error"]["code"] == code
            rendered = str(envelope).lower()
            for secret in (
                "secret-principal",
                "secret-root-component",
                "payload-sentinel",
                "registry.sqlite",
                "traceback",
                "sqlite3",
            ):
                assert secret not in rendered

    anyio.run(exercise)


@pytest.mark.parametrize("arguments", ["string-argument-secret", ["list-argument-secret"]])
def test_non_object_arguments_is_sdk_invalid_params(tmp_path: Path, arguments: object) -> None:
    root = tmp_path / "secret-root-component"
    _secure_parent(root)

    async def exercise() -> None:
        raw = await spawn_raw(sqlite_parameters(root, "secret-principal"))
        try:
            await raw_initialize(raw)
            await send_line(
                raw,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "aipcs_server_info", "arguments": arguments},
                },
            )
            invalid = await read_json_until_id(raw, 2)
            assert invalid["error"]["code"] == -32602
            assert "result" not in invalid
            assert_no_secrets(
                invalid,
                "secret-principal",
                "secret-root-component",
                "registry.sqlite",
                "traceback",
                "sqlite3",
                "string-argument-secret",
                "list-argument-secret",
            )
            await send_line(
                raw,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "aipcs_server_info", "arguments": {}},
                },
            )
            valid = await read_json_until_id(raw, 3)
            assert valid["result"]["structuredContent"]["ok"] is True
        finally:
            stdout, stderr = await close_process(raw)
            assert_no_secrets(
                stdout + stderr,
                "secret-principal",
                "secret-root-component",
                "registry.sqlite",
                "traceback",
                "sqlite3",
            )

    anyio.run(exercise)


def test_malformed_line_does_not_poison_next_request_at_debug(tmp_path: Path) -> None:
    root = tmp_path / "secret-root-component"
    _secure_parent(root)
    malformed_secret = "malformed-payload-secret"

    async def exercise() -> None:
        raw = await spawn_raw(sqlite_parameters(root, "secret-principal", debug=True))
        try:
            await raw_initialize(raw)
            await send_line(raw, b'{"broken":"malformed-payload-secret"')
            await send_line(
                raw,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "aipcs_server_info", "arguments": {}},
                },
            )
            valid = await read_json_until_id(raw, 3)
            assert valid["result"]["structuredContent"]["ok"] is True
        finally:
            stdout, stderr = await close_process(raw)
            assert_no_secrets(
                stdout + stderr,
                "secret-principal",
                "secret-root-component",
                malformed_secret,
                "registry.sqlite",
                "traceback",
                "sqlite3",
                "exception",
            )

    anyio.run(exercise)
