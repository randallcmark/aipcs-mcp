from __future__ import annotations

import json
from typing import Any

import pytest

from aipcs_mcp import cli
from aipcs_mcp.contracts import LISTENER_ENV_KEYS, TRANSPORT_ENV_KEYS


def _clear_transport_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (*LISTENER_ENV_KEYS, *TRANSPORT_ENV_KEYS):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize(
    ("argv", "environment"),
    [
        (["serve", "--transport", "sse"], {}),
        (["serve"], {"FASTMCP_PORT": "8000"}),
        (["serve"], {"AIPCS_MCP_TRANSPORT": "sse"}),
    ],
)
def test_non_stdio_configuration_returns_one_failure_envelope_before_server_construction(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    environment: dict[str, str],
) -> None:
    _clear_transport_environment(monkeypatch)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    constructed = False

    def unexpected_server(_: object) -> Any:
        nonlocal constructed
        constructed = True
        raise AssertionError("server must not be constructed for rejected configuration")

    monkeypatch.setattr(cli, "compose_server", unexpected_server)

    assert cli.main(argv) == 2
    assert constructed is False
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    assert envelope == {
        "ok": False,
        "result": None,
        "error": {
            "code": "transport_not_supported",
            "message": (
                "Use documented AIPCS transport configuration; legacy listener settings are disabled."
            ),
            "issues": [],
            "retryable": False,
        },
    }


def test_stdio_configuration_constructs_server_only_after_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_transport_environment(monkeypatch)
    calls: list[str] = []

    class StubServer: ...

    async def run_stdio(_: object) -> None:
        calls.append("stdio")

    monkeypatch.setattr(cli, "compose_server", lambda _: StubServer())
    monkeypatch.setattr(cli, "run_stdio", run_stdio)
    monkeypatch.setattr(cli, "_configure_stderr_logging", lambda _: None)

    assert cli.main(["serve", "--transport", "stdio"]) == 0
    assert calls == ["stdio"]


def test_streamable_http_configuration_constructs_server_only_after_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_transport_environment(monkeypatch)
    calls: list[str] = []

    class StubServer: ...

    async def run_streamable_http(_: object, config: object) -> None:
        assert config.transport == "streamable-http"  # type: ignore[attr-defined]
        calls.append("streamable-http")

    monkeypatch.setattr(cli, "compose_server", lambda _: StubServer())
    monkeypatch.setattr(cli, "run_streamable_http", run_streamable_http)
    monkeypatch.setattr(cli, "_configure_stderr_logging", lambda _: None)

    assert cli.main(["serve", "--transport", "streamable-http"]) == 0
    assert calls == ["streamable-http"]
