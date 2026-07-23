"""V1-08C SQLite busy-timeout configuration and runtime gate coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

from aipcs_mcp.configuration.errors import ConfigurationError
from aipcs_mcp.configuration.models import ConfigOverrides
from aipcs_mcp.configuration.reporting import safe_config_report
from aipcs_mcp.configuration.resolver import (
    is_supported_sqlite_runtime,
    require_runnable,
    resolve_configuration,
)

from .helpers import write_config


def _resolve(
    *,
    overrides: ConfigOverrides | None = None,
    environ: dict[str, str] | None = None,
    path: Path | None = None,
):
    return resolve_configuration(
        overrides=overrides or ConfigOverrides(),
        environ=environ or {},
        config_path=path,
    )


def test_default_timeout_source_and_safe_report() -> None:
    config = _resolve()
    assert config.sqlite_busy_timeout_ms == 5000
    assert config.sources["sqlite_busy_timeout_ms"] == "default"
    assert safe_config_report(config)["storage"]["sqlite_busy_timeout_ms"] == 5000  # type: ignore[index]


@pytest.mark.parametrize("value", ["1", "30000"])
def test_cli_timeout_accepts_exact_range(value: str) -> None:
    config = _resolve(
        overrides=ConfigOverrides(
            profile="sqlite",
            principal_id="operator",
            sqlite_data_root="/sqlite-root",
            sqlite_busy_timeout_ms=value,
        )
    )
    assert config.sqlite_busy_timeout_ms == int(value)
    assert config.sources["sqlite_busy_timeout_ms"] == "cli"


@pytest.mark.parametrize(
    "value",
    ["", "0", "30001", "-1", "+1", " 1", "1 ", "01", "1.0", "1e3", "1_000", "1\n"],
)
def test_cli_and_environment_timeout_text_is_canonical(value: str) -> None:
    with pytest.raises(ConfigurationError):
        _resolve(
            overrides=ConfigOverrides(
                profile="sqlite", principal_id="operator", sqlite_busy_timeout_ms=value
            )
        )
    with pytest.raises(ConfigurationError):
        _resolve(
            overrides=ConfigOverrides(profile="sqlite", principal_id="operator"),
            environ={"AIPCS_SQLITE_BUSY_TIMEOUT_MS": value},
        )


@pytest.mark.parametrize(
    "toml_value",
    ["0", "30001", "true", '"5000"', "1.0", "1e3"],
)
def test_toml_timeout_requires_a_bounded_real_integer(tmp_path: Path, toml_value: str) -> None:
    with pytest.raises(ConfigurationError):
        _resolve(
            path=write_config(
                tmp_path / "config.toml",
                "config_version=1\nprofile=\"sqlite\"\n[identity]\nprincipal_id=\"operator\"\n"
                f"[sqlite]\nbusy_timeout_ms={toml_value}\n",
            )
        )


def test_timeout_precedence_and_wrong_profile_rejection(tmp_path: Path) -> None:
    path = write_config(
        tmp_path / "config.toml",
        "config_version=1\nprofile=\"sqlite\"\n[identity]\nprincipal_id=\"operator\"\n"
        "[sqlite]\ndata_root=\"/sqlite-root\"\nbusy_timeout_ms=100\n",
    )
    environment = {
        "AIPCS_SQLITE_BUSY_TIMEOUT_MS": "200",
    }
    config = _resolve(
        overrides=ConfigOverrides(sqlite_busy_timeout_ms="300"), environ=environment, path=path
    )
    assert (config.sqlite_busy_timeout_ms, config.sources["sqlite_busy_timeout_ms"]) == (300, "cli")
    environment_config = _resolve(environ=environment, path=path)
    assert (environment_config.sqlite_busy_timeout_ms, environment_config.sources["sqlite_busy_timeout_ms"]) == (
        200,
        "environment",
    )
    file_config = _resolve(path=path)
    assert (file_config.sqlite_busy_timeout_ms, file_config.sources["sqlite_busy_timeout_ms"]) == (
        100,
        "file",
    )
    for wrong_profile in (
        ConfigOverrides(sqlite_busy_timeout_ms="100"),
        ConfigOverrides(
            profile="postgresql",
            principal_id="operator",
            postgres_dsn_env="AIPCS_DSN",
            sqlite_busy_timeout_ms="100",
        ),
    ):
        with pytest.raises(ConfigurationError):
            _resolve(overrides=wrong_profile)


def test_sqlite_runtime_gate_reads_only_runtime_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    class Runtime:
        sqlite_version_info = (3, 51, 2)

    monkeypatch.setattr("aipcs_mcp.configuration.resolver.import_module", lambda _: Runtime)
    assert is_supported_sqlite_runtime() is False
    config = _resolve(
        overrides=ConfigOverrides(
            profile="sqlite", principal_id="operator", sqlite_data_root="/sqlite-root"
        )
    )
    with pytest.raises(ConfigurationError):
        require_runnable(config)

    Runtime.sqlite_version_info = (3, 51, 3)
    assert is_supported_sqlite_runtime() is True
    require_runnable(config)

    for malformed in (None, (), (3, 51), ("3", 51, 3), [3, 51, 3]):
        Runtime.sqlite_version_info = malformed
        assert is_supported_sqlite_runtime() is False
