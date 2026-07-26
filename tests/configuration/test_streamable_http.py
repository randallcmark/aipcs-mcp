"""Configuration and protocol proofs for the supported service transport."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from aipcs_mcp.configuration.errors import ConfigurationError
from aipcs_mcp.configuration.models import ConfigOverrides
from aipcs_mcp.configuration.reporting import safe_config_report
from aipcs_mcp.configuration.resolver import resolve_configuration
from aipcs_mcp.http_server import create_streamable_http_app
from aipcs_mcp.mcp_server import create_server


def _resolve(**values: str) -> object:
    return resolve_configuration(
        overrides=ConfigOverrides(transport="streamable-http", **values),
        environ={},
        config_path=None,
    )


def test_loopback_streamable_http_defaults_are_safe_and_redacted() -> None:
    config = _resolve()
    assert config.http_host == "127.0.0.1"
    assert config.http_port == 8000
    assert config.http_path == "/mcp"
    assert config.http_allowed_hosts == ("127.0.0.1:8000", "localhost:8000")
    assert config.http_allowed_origins == ()
    assert config.http_session_idle_timeout_seconds == 1800
    report = safe_config_report(config)
    assert report["streamable_http"] == {
        "configured": True,
        "host_configured": False,
        "port_configured": False,
        "path_configured": False,
        "host_policy_configured": False,
        "origin_policy_configured": False,
        "session_idle_timeout_seconds": 1800,
        "non_loopback_explicitly_allowed": False,
    }
    assert "127.0.0.1" not in repr(report)
    assert "/mcp" not in repr(report)


def test_non_loopback_requires_explicit_exposure_and_host_policy() -> None:
    with pytest.raises(ConfigurationError):
        _resolve(http_host="0.0.0.0")
    with pytest.raises(ConfigurationError):
        _resolve(http_host="0.0.0.0", http_allow_non_loopback="true")
    config = _resolve(
        http_host="0.0.0.0",
        http_allow_non_loopback="true",
        http_allowed_hosts="aipcs.internal:443",
        http_allowed_origins="https://aipcs.internal",
    )
    assert config.http_allow_non_loopback is True
    assert config.http_allowed_hosts == ("aipcs.internal:443",)
    assert config.http_allowed_origins == ("https://aipcs.internal",)


@pytest.mark.parametrize(
    "overrides",
    [
        {"http_host": "localhost"},
        {"http_port": "08000"},
        {"http_path": "mcp"},
        {"http_path": "/mcp?x=1"},
        {"http_allowed_hosts": "https://aipcs.internal"},
        {"http_allowed_hosts": "aipcs.internal:65536"},
        {"http_allowed_origins": "https://aipcs.internal/mcp"},
        {"http_allowed_origins": "https://aipcs.internal:65536"},
        {"http_allow_non_loopback": "yes"},
    ],
)
def test_streamable_http_configuration_fails_closed(overrides: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError):
        _resolve(**overrides)


def test_streamable_http_toml_lists_are_supported_but_strict(tmp_path: Path) -> None:
    path = tmp_path / "service.toml"
    path.write_text(
        """config_version = 1
transport = "streamable-http"

[streamable_http]
host = "127.0.0.1"
port = 9000
path = "/memory"
allowed_hosts = ["127.0.0.1:9000", "localhost:9000"]
allowed_origins = []
session_idle_timeout_seconds = 900
allow_non_loopback = false
""",
        encoding="utf-8",
    )
    config = resolve_configuration(overrides=ConfigOverrides(), environ={}, config_path=path)
    assert config.http_path == "/memory"
    assert config.http_allowed_hosts == ("127.0.0.1:9000", "localhost:9000")
    assert config.http_allowed_origins == ()
    assert config.http_session_idle_timeout_seconds == 900


def test_streamable_http_fields_follow_cli_environment_file_default_precedence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "service.toml"
    path.write_text(
        """config_version = 1
transport = "streamable-http"

[streamable_http]
host = "127.0.0.1"
port = 9000
path = "/file"
allowed_hosts = ["127.0.0.1:9000"]
allowed_origins = ["https://file.example.test"]
session_idle_timeout_seconds = 900
allow_non_loopback = false
""",
        encoding="utf-8",
    )
    config = resolve_configuration(
        overrides=ConfigOverrides(http_port="9002"),
        environ={
            "AIPCS_HTTP_PORT": "9001",
            "AIPCS_HTTP_PATH": "/environment",
            "AIPCS_HTTP_ALLOWED_HOSTS": "127.0.0.1:9001,localhost:9001",
            "AIPCS_HTTP_ALLOWED_ORIGINS": "https://environment.example.test",
            "AIPCS_HTTP_SESSION_IDLE_TIMEOUT_SECONDS": "901",
        },
        config_path=path,
    )
    assert (config.http_port, config.sources["http_port"]) == (9002, "cli")
    assert (config.http_path, config.sources["http_path"]) == ("/environment", "environment")
    assert config.http_allowed_hosts == ("127.0.0.1:9001", "localhost:9001")
    assert config.sources["http_allowed_hosts"] == "environment"
    assert config.http_allowed_origins == ("https://environment.example.test",)
    assert config.sources["http_allowed_origins"] == "environment"
    assert (config.http_session_idle_timeout_seconds, config.sources["http_session_idle_timeout_seconds"]) == (
        901,
        "environment",
    )
    assert config.sources["http_host"] == "file"


async def _http_smoke() -> None:
    config = _resolve()
    app = create_streamable_http_app(create_server(), config)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client,
            streamable_http_client("http://127.0.0.1:8000/mcp", http_client=client) as (
                read_stream,
                write_stream,
                _,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            result = await session.call_tool("aipcs_server_info", {})
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is True
    assert payload["result"]["transports"] == ["stdio", "streamable-http"]


def test_streamable_http_real_client_smoke() -> None:
    anyio.run(_http_smoke)


async def _security_smoke() -> tuple[int, int]:
    config = _resolve()
    app = create_streamable_http_app(create_server(), config)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8000"
        ) as valid_client:
            origin = await valid_client.post(
                "/mcp",
                headers={"content-type": "application/json", "origin": "https://attacker.test"},
                content="{}",
            )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://attacker.test:8000"
        ) as invalid_host_client:
            host = await invalid_host_client.post(
                "/mcp", headers={"content-type": "application/json"}, content="{}"
            )
    return origin.status_code, host.status_code


def test_streamable_http_rejects_unapproved_origin_and_host() -> None:
    assert anyio.run(_security_smoke) == (403, 421)
