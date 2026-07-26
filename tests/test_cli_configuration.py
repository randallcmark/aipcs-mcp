from __future__ import annotations

import json
from typing import Any

import pytest

from aipcs_mcp import cli
from aipcs_mcp.errors import AipcsContractError, ErrorCode


def _clear_transport_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIPCS_TRANSPORT", raising=False)
    monkeypatch.delenv("AIPCS_MCP_TRANSPORT", raising=False)
    for key in cli.validate_transport_environment.__globals__["LISTENER_ENV_KEYS"]:
        monkeypatch.delenv(key, raising=False)


def test_config_show_returns_redacted_success_without_constructing_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_transport_environment(monkeypatch)
    class Resolved:
        transport = "stdio"

    resolved = Resolved()
    captured: dict[str, Any] = {}

    def resolve(**kwargs: Any) -> object:
        captured.update(kwargs)
        return resolved

    monkeypatch.setattr(cli, "resolve_configuration", resolve)
    monkeypatch.setattr(
        cli,
        "safe_config_report",
        lambda value: {"profile": "sqlite", "available": False, "runnable": False},
    )
    monkeypatch.setattr(
        cli,
        "compose_server",
        lambda _: (_ for _ in ()).throw(
            AssertionError("config commands must not construct a server")
        ),
    )

    assert (
        cli.main(
            [
                "config",
                "show",
                "--profile",
                "sqlite",
                "--principal-id",
                "hidden-principal",
            ]
        )
        == 0
    )
    assert captured["config_path"] is None
    assert isinstance(captured["environ"], dict)
    assert captured["overrides"].profile == "sqlite"
    assert captured["overrides"].principal_id == "hidden-principal"
    envelope = json.loads(capsys.readouterr().out)
    assert envelope == {
        "ok": True,
        "result": {"profile": "sqlite", "available": False, "runnable": False},
        "error": None,
    }


def test_config_validate_requires_runnable_profile_without_constructing_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_transport_environment(monkeypatch)
    class Resolved:
        transport = "stdio"

    resolved = Resolved()
    calls: list[object] = []
    monkeypatch.setattr(cli, "resolve_configuration", lambda **_: resolved)
    monkeypatch.setattr(cli, "require_runnable", lambda value: calls.append(value))
    monkeypatch.setattr(
        cli,
        "safe_validation_report",
        lambda value: {"profile": "stateless", "available": True, "runnable": True},
    )
    monkeypatch.setattr(
        cli,
        "compose_server",
        lambda _: (_ for _ in ()).throw(
            AssertionError("config commands must not construct a server")
        ),
    )

    assert cli.main(["config", "validate"]) == 0
    assert calls == [resolved]
    assert json.loads(capsys.readouterr().out)["result"]["runnable"] is True


def test_config_show_supports_opt_in_human_projection(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _clear_transport_environment(monkeypatch)
    monkeypatch.setattr(cli, "resolve_configuration", lambda **_: object())
    monkeypatch.setattr(
        cli,
        "safe_config_report",
        lambda _: {
            "profile": "sqlite",
            "available": True,
            "sources": {"profile": "default"},
        },
    )

    assert cli.main(["config", "show", "--format", "human"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "profile: sqlite",
        "available: true",
        "sources.profile: default",
    ]


def test_config_failure_uses_existing_error_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_transport_environment(monkeypatch)
    problem = ValueError("sensitive detail")
    monkeypatch.setattr(cli, "resolve_configuration", lambda **_: (_ for _ in ()).throw(problem))
    monkeypatch.setattr(
        cli,
        "to_contract_error",
        lambda error: AipcsContractError(
            ErrorCode.VALIDATION_FAILED, "Configuration failed public validation."
        ),
    )

    assert cli.main(["config", "validate"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["error"]["code"] == "validation_failed"
    assert "sensitive detail" not in output.err


def test_serve_resolves_and_requires_runnable_before_server_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_transport_environment(monkeypatch)
    class Resolved:
        transport = "stdio"

    resolved = Resolved()
    calls: list[str] = []
    monkeypatch.setattr(cli, "resolve_configuration", lambda **_: resolved)
    monkeypatch.setattr(cli, "require_runnable", lambda value: calls.append("runnable"))
    monkeypatch.setattr(cli, "_configure_stderr_logging", lambda value: calls.append("logging"))

    class StubServer: ...

    async def run_stdio(_: object) -> None:
        calls.append("stdio")

    monkeypatch.setattr(cli, "compose_server", lambda _: StubServer())
    monkeypatch.setattr(cli, "run_stdio", run_stdio)

    assert cli.main(["serve"]) == 0
    assert calls == ["runnable", "logging", "stdio"]
