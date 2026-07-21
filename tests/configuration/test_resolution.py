from pathlib import Path

from aipcs_mcp.configuration.models import ConfigOverrides
from aipcs_mcp.configuration.resolver import resolve_configuration

from .helpers import write_config


def test_field_precedence_and_immutable_sources(tmp_path: Path) -> None:
    path = write_config(
        tmp_path / "config.toml",
        'config_version=1\nprofile="stateless"\n[logging]\nlevel="error"\n',
    )
    config = resolve_configuration(
        overrides=ConfigOverrides(log_level="debug"),
        environ={"AIPCS_LOG_LEVEL": "info"},
        config_path=path,
    )
    assert config.log_level == "debug" and config.sources["log_level"] == "cli"
    try:
        config.sources["log_level"] = "file"  # type: ignore[index]
    except TypeError:
        pass
    else:
        raise AssertionError("sources must be immutable")


def test_defaults_without_discovery() -> None:
    assert (
        resolve_configuration(overrides=ConfigOverrides(), environ={}, config_path=None).profile
        == "stateless"
    )
