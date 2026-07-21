from pathlib import Path

import pytest

from aipcs_mcp.configuration.errors import ConfigurationError
from aipcs_mcp.configuration.models import ConfigOverrides
from aipcs_mcp.configuration.resolver import require_runnable, resolve_configuration

from .helpers import write_config


@pytest.mark.parametrize(
    "text", ["config_version=2", "config_version=1\nunknown=1", "config_version=1\nprofile=1"]
)
def test_strict_toml(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigurationError):
        resolve_configuration(
            overrides=ConfigOverrides(),
            environ={},
            config_path=write_config(tmp_path / "x.toml", text),
        )


def test_persistent_rules_blank_and_runnable() -> None:
    with pytest.raises(ConfigurationError):
        resolve_configuration(
            overrides=ConfigOverrides(profile="sqlite"), environ={}, config_path=None
        )
    with pytest.raises(ConfigurationError):
        resolve_configuration(
            overrides=ConfigOverrides(), environ={"AIPCS_LOG_LEVEL": ""}, config_path=None
        )
    config = resolve_configuration(
        overrides=ConfigOverrides(
            profile="postgresql", principal_id="p", postgres_dsn_env="AIPCS_DSN"
        ),
        environ={},
        config_path=None,
    )
    with pytest.raises(ConfigurationError):
        require_runnable(config)


def test_absolute_sqlite_and_contradiction() -> None:
    with pytest.raises(ConfigurationError):
        resolve_configuration(
            overrides=ConfigOverrides(
                profile="sqlite", principal_id="p", sqlite_data_root="relative"
            ),
            environ={},
            config_path=None,
        )
    with pytest.raises(ConfigurationError):
        resolve_configuration(
            overrides=ConfigOverrides(profile="stateless", sqlite_data_root="/tmp/a"),
            environ={},
            config_path=None,
        )
