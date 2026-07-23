"""PostgreSQL configuration remains offline, typed, and redacted."""

from __future__ import annotations

from pathlib import Path

import pytest

from aipcs_mcp.configuration.errors import ConfigurationError
from aipcs_mcp.configuration.models import ConfigOverrides
from aipcs_mcp.configuration.reporting import safe_config_report, safe_validation_report
from aipcs_mcp.configuration.resolver import require_runnable, resolve_configuration

from .helpers import write_config


def _resolve(
    *,
    overrides: ConfigOverrides | None = None,
    environ: dict[str, str] | None = None,
    path: Path | None = None,
):
    return resolve_configuration(
        overrides=overrides or ConfigOverrides(), environ=environ or {}, config_path=path
    )


def test_postgresql_is_structurally_runnable_without_driver_or_dsn_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _resolve(
        overrides=ConfigOverrides(
            profile="postgresql", principal_id="operator", postgres_dsn_env="AIPCS_TEST_DSN"
        ),
        environ={"AIPCS_TEST_DSN": "postgresql://secret@private.invalid/aipcs"},
    )
    monkeypatch.setattr(
        "aipcs_mcp.configuration.resolver.import_module",
        lambda name: (_ for _ in ()).throw(AssertionError(f"unexpected import: {name}")),
    )
    require_runnable(config)
    assert safe_validation_report(config) == {
        "config_version": 1,
        "valid": True,
        "profile": "postgresql",
        "runnable": True,
    }
    assert safe_config_report(config)["storage"] == {
        "sqlite_data_root_configured": False,
        "sqlite_busy_timeout_ms": 5000,
        "postgresql_dsn_configured": True,
        "postgresql_connect_timeout_seconds": 10,
        "postgresql_lock_timeout_ms": 5000,
        "postgresql_statement_timeout_ms": 30000,
    }


def test_postgresql_timeout_defaults_and_precedence(tmp_path: Path) -> None:
    path = write_config(
        tmp_path / "postgresql.toml",
        "config_version=1\nprofile=\"postgresql\"\n[identity]\nprincipal_id=\"operator\"\n"
        "[postgresql]\ndsn_env=\"FILE_DSN\"\nconnect_timeout_seconds=4\n"
        "lock_timeout_ms=400\nstatement_timeout_ms=9000\n",
    )
    environment = {
        "AIPCS_POSTGRES_DSN_ENV": "ENV_DSN",
        "AIPCS_POSTGRES_CONNECT_TIMEOUT_SECONDS": "5",
        "AIPCS_POSTGRES_LOCK_TIMEOUT_MS": "500",
        "AIPCS_POSTGRES_STATEMENT_TIMEOUT_MS": "10000",
    }
    file_config = _resolve(path=path)
    environment_config = _resolve(environ=environment, path=path)
    cli_config = _resolve(
        overrides=ConfigOverrides(
            postgres_connect_timeout_seconds="6",
            postgres_lock_timeout_ms="600",
            postgres_statement_timeout_ms="11000",
        ),
        environ=environment,
        path=path,
    )
    assert (
        file_config.postgres_connect_timeout_seconds,
        file_config.postgres_lock_timeout_ms,
        file_config.postgres_statement_timeout_ms,
    ) == (4, 400, 9000)
    assert (
        environment_config.postgres_connect_timeout_seconds,
        environment_config.postgres_lock_timeout_ms,
        environment_config.postgres_statement_timeout_ms,
    ) == (5, 500, 10000)
    assert (
        cli_config.postgres_connect_timeout_seconds,
        cli_config.postgres_lock_timeout_ms,
        cli_config.postgres_statement_timeout_ms,
    ) == (6, 600, 11000)
    assert all(
        cli_config.sources[field] == "cli"
        for field in (
            "postgres_connect_timeout_seconds",
            "postgres_lock_timeout_ms",
            "postgres_statement_timeout_ms",
        )
    )


@pytest.mark.parametrize(
    "overrides",
    [
        ConfigOverrides(
            profile="postgresql",
            principal_id="operator",
            postgres_dsn_env="AIPCS_DSN",
            postgres_connect_timeout_seconds="0",
        ),
        ConfigOverrides(
            profile="postgresql",
            principal_id="operator",
            postgres_dsn_env="AIPCS_DSN",
            postgres_connect_timeout_seconds="61",
        ),
        ConfigOverrides(
            profile="postgresql",
            principal_id="operator",
            postgres_dsn_env="AIPCS_DSN",
            postgres_lock_timeout_ms="0",
        ),
        ConfigOverrides(
            profile="postgresql",
            principal_id="operator",
            postgres_dsn_env="AIPCS_DSN",
            postgres_statement_timeout_ms="999",
        ),
        ConfigOverrides(
            profile="postgresql",
            principal_id="operator",
            postgres_dsn_env="AIPCS_DSN",
            postgres_lock_timeout_ms="5001",
            postgres_statement_timeout_ms="5000",
        ),
    ],
)
def test_postgresql_timeouts_are_strict_and_ordered(overrides: ConfigOverrides) -> None:
    with pytest.raises(ConfigurationError):
        _resolve(overrides=overrides)


def test_postgresql_timeout_settings_are_rejected_for_other_profiles(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        _resolve(overrides=ConfigOverrides(postgres_lock_timeout_ms="100"))
    with pytest.raises(ConfigurationError):
        _resolve(
            path=write_config(
                tmp_path / "wrong-profile.toml",
                "config_version=1\nprofile=\"sqlite\"\n[identity]\nprincipal_id=\"operator\"\n"
                "[postgresql]\nlock_timeout_ms=100\n",
            )
        )
