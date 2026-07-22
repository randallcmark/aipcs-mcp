"""Supplemental adversarial V1-05 configuration coverage."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from aipcs_mcp import cli, mcp_server
from aipcs_mcp.configuration.errors import ConfigurationError, to_contract_error
from aipcs_mcp.configuration.models import ConfigOverrides, ResolvedConfiguration
from aipcs_mcp.configuration.resolver import require_runnable, resolve_configuration
from aipcs_mcp.contracts import LISTENER_ENV_KEYS, TRANSPORT_ENV_KEYS


def resolve(*, overrides=None, environ=None, path=None):
    return resolve_configuration(
        overrides=overrides or ConfigOverrides(), environ=environ or {}, config_path=path
    )


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_each_field_proves_environment_over_file_and_file_over_default(tmp_path: Path) -> None:
    sqlite = write(
        tmp_path / "sqlite.toml",
        'config_version=1\nprofile="sqlite"\ntransport="stdio"\n[identity]\nprincipal_id="file-p"\n[sqlite]\ndata_root="/file-root"\n[logging]\nlevel="error"\n',
    )
    file = resolve(path=sqlite)
    env = resolve(
        environ={
            "AIPCS_PROFILE": "sqlite",
            "AIPCS_PRINCIPAL_ID": "env-p",
            "AIPCS_SQLITE_DATA_ROOT": "/env-root",
            "AIPCS_TRANSPORT": "stdio",
            "AIPCS_LOG_LEVEL": "info",
        },
        path=sqlite,
    )
    default = resolve()
    assert (file.profile, file.sources["profile"], default.profile) == (
        "sqlite",
        "file",
        "stateless",
    )
    assert (env.profile, env.sources["profile"]) == ("sqlite", "environment")
    assert (file.transport, file.sources["transport"], env.sources["transport"]) == (
        "stdio",
        "file",
        "environment",
    )
    assert (file.principal_id, env.principal_id, default.principal_id) == ("file-p", "env-p", None)
    assert (file.sqlite_data_root, env.sqlite_data_root, default.sqlite_data_root) == (
        Path("/file-root"),
        Path("/env-root"),
        None,
    )
    assert (file.log_level, env.log_level, default.log_level) == ("error", "info", "warning")
    assert env.sources["principal_id"] == "environment"
    assert env.sources["sqlite_data_root"] == "environment"
    assert env.sources["log_level"] == "environment"
    assert file.sources["principal_id"] == "file"
    assert file.sources["sqlite_data_root"] == "file"
    assert file.sources["log_level"] == "file"
    postgres = write(
        tmp_path / "postgres.toml",
        'config_version=1\nprofile="postgresql"\n[identity]\nprincipal_id="p"\n[postgresql]\ndsn_env="FILE_DSN"\n',
    )
    file_postgres = resolve(path=postgres)
    env_postgres = resolve(environ={"AIPCS_POSTGRES_DSN_ENV": "ENV_DSN"}, path=postgres)
    assert (file_postgres.postgres_dsn_env, file_postgres.sources["postgres_dsn_env"]) == (
        "FILE_DSN",
        "file",
    )
    assert (env_postgres.postgres_dsn_env, env_postgres.sources["postgres_dsn_env"]) == (
        "ENV_DSN",
        "environment",
    )
    assert default.postgres_dsn_env is None
    assert default.sources == {
        "profile": "default",
        "transport": "default",
        "principal_id": "default",
        "sqlite_data_root": "default",
        "postgres_dsn_env": "default",
        "log_level": "default",
    }


@pytest.mark.parametrize(
    ("field", "file_text", "environ", "overrides", "expected"),
    [
        (
            "profile",
            'config_version=1\nprofile="sqlite"\n[identity]\nprincipal_id="p"\n',
            {
                "AIPCS_PROFILE": "sqlite",
                "AIPCS_PRINCIPAL_ID": "p",
                "AIPCS_POSTGRES_DSN_ENV": "ENV_DSN",
            },
            ConfigOverrides(profile="postgresql"),
            "postgresql",
        ),
        (
            "transport",
            'config_version=1\ntransport="stdio"\n',
            {"AIPCS_TRANSPORT": "stdio"},
            ConfigOverrides(transport="stdio"),
            "stdio",
        ),
        (
            "principal_id",
            'config_version=1\nprofile="sqlite"\n[identity]\nprincipal_id="file"\n',
            {"AIPCS_PRINCIPAL_ID": "environment", "HOME": "/safe-home"},
            ConfigOverrides(principal_id="cli"),
            "cli",
        ),
        (
            "sqlite_data_root",
            (
                'config_version=1\nprofile="sqlite"\n[identity]\nprincipal_id="p"\n'
                '[sqlite]\ndata_root="/file-root"\n'
            ),
            {"AIPCS_SQLITE_DATA_ROOT": "/environment-root"},
            ConfigOverrides(sqlite_data_root="/cli-root"),
            Path("/cli-root"),
        ),
        (
            "postgres_dsn_env",
            (
                'config_version=1\nprofile="postgresql"\n[identity]\nprincipal_id="p"\n'
                '[postgresql]\ndsn_env="FILE_DSN"\n'
            ),
            {"AIPCS_POSTGRES_DSN_ENV": "ENVIRONMENT_DSN"},
            ConfigOverrides(postgres_dsn_env="CLI_DSN"),
            "CLI_DSN",
        ),
        (
            "log_level",
            'config_version=1\n[logging]\nlevel="error"\n',
            {"AIPCS_LOG_LEVEL": "info"},
            ConfigOverrides(log_level="debug"),
            "debug",
        ),
    ],
)
def test_each_field_proves_cli_over_environment(
    tmp_path: Path,
    field: str,
    file_text: str,
    environ: dict[str, str],
    overrides: ConfigOverrides,
    expected: object,
) -> None:
    config = resolve(
        overrides=overrides,
        environ=environ,
        path=write(tmp_path / "precedence.toml", file_text),
    )
    assert getattr(config, field) == expected
    assert config.sources[field] == "cli"


@pytest.mark.parametrize("value", ["", " \t", " p ", "p\nnext", "p\x1f", "p" * 129])
def test_principal_validation_is_strict(value: str) -> None:
    with pytest.raises(ConfigurationError):
        resolve(overrides=ConfigOverrides(profile="sqlite", principal_id=value))


@pytest.mark.parametrize("value", ["", " ", "relative", "/tmp/\x1funsafe", "/" + "x" * 4096])
def test_sqlite_descriptor_is_strict(value: str) -> None:
    with pytest.raises(ConfigurationError):
        resolve(
            overrides=ConfigOverrides(
                profile="sqlite",
                principal_id="principal",
                sqlite_data_root=value,
            )
        )


@pytest.mark.parametrize("value", ["", "lowercase", "AIPCS_DSN\nNEXT", "AIPCS_" + "X" * 128])
def test_postgresql_reference_is_strict(value: str) -> None:
    with pytest.raises(ConfigurationError):
        resolve(
            overrides=ConfigOverrides(
                profile="postgresql",
                principal_id="principal",
                postgres_dsn_env=value,
            )
        )


@pytest.mark.parametrize(
    "text",
    [
        "not valid toml =\n",
        "config_version=1.0\n",
        "config_version=true\n",
        'config_version="1"\n',
        "config_version=1\nunknown=1\n",
        "config_version=1\nvalues=[1]\n",
        "config_version=1\nidentity=[]\n",
        "config_version=1\n[identity]\nprincipal_id=1\n",
        'config_version=1\n[logging]\nunknown="warning"\n',
        "config_version=1\n[one.two.three.four.five.six.seven.eight.nine]\n",
        "config_version=1\n" + "".join(f"key_{i}=1\n" for i in range(129)),
    ],
)
def test_hostile_toml_is_rejected(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigurationError):
        resolve(path=write(tmp_path / "hostile.toml", text))


def test_missing_non_utf8_oversize_no_discovery_and_no_storage_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ConfigurationError):
        resolve(path=tmp_path / "missing.toml")
    with pytest.raises(ConfigurationError):
        resolve(path=tmp_path)
    raw = tmp_path / "raw.toml"
    raw.write_bytes(b"\xff\xfe")
    with pytest.raises(ConfigurationError):
        resolve(path=raw)
    raw.write_bytes(b"x" * 65537)
    with pytest.raises(ConfigurationError):
        resolve(path=raw)
    write(
        tmp_path / "aipcs.toml",
        'config_version=1\nprofile="sqlite"\n[identity]\nprincipal_id="p"\n',
    )
    monkeypatch.chdir(tmp_path)
    assert resolve().profile == "stateless"
    root = tmp_path / "not-created"
    config = resolve(
        overrides=ConfigOverrides(profile="sqlite", principal_id="p", sqlite_data_root=str(root))
    )
    require_runnable(config)
    assert not root.exists()


def test_undocumented_environment_names_and_config_selector_are_ignored(tmp_path: Path) -> None:
    config_path = write(
        tmp_path / "not-selected.toml",
        'config_version=1\nprofile="sqlite"\n[identity]\nprincipal_id="p"\n',
    )
    config = resolve(
        environ={
            "AIPCS_CONFIG": str(config_path),
            "AIPCS_UNKNOWN_SETTING": "postgresql",
            "AIPCS_DATABASE_URL": "postgresql://synthetic.invalid/db",
        }
    )
    assert config.profile == "stateless"
    assert all(source == "default" for source in config.sources.values())


@pytest.mark.parametrize(
    "text",
    [
        'config_version=1\nprofile="stateless"\n[sqlite]\ndata_root="/state"\n',
        'config_version=1\nprofile="sqlite"\n',
        'config_version=1\nprofile="sqlite"\n[identity]\nprincipal_id="p"\n[postgresql]\ndsn_env="AIPCS_DSN"\n',
        'config_version=1\nprofile="postgresql"\n[identity]\nprincipal_id="p"\n',
        'config_version=1\nprofile="postgresql"\n[identity]\nprincipal_id="p"\n[postgresql]\ndsn="postgresql://secret@host/db"\n',
    ],
)
def test_profile_principal_storage_and_dsn_contradictions_fail(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigurationError):
        resolve(path=write(tmp_path / "bad.toml", text))


def test_direct_construction_copies_and_freezes_sources() -> None:
    sources = {"profile": "cli"}
    config = ResolvedConfiguration("stateless", "stdio", None, None, None, "warning", sources)
    sources["profile"] = "file"
    assert config.sources["profile"] == "cli"
    with pytest.raises(FrozenInstanceError):
        config.profile = "sqlite"  # type: ignore[misc]
    with pytest.raises(TypeError):
        config.sources["profile"] = "file"  # type: ignore[index]


def test_sensitive_configuration_values_are_absent_from_repr_and_errors() -> None:
    principal = "SYNTHETIC-PRINCIPAL-SENTINEL"
    root = "/private/SYNTHETIC-PATH-SENTINEL"
    dsn_ref = "SYNTHETIC_DSN_SENTINEL"
    sqlite = resolve(
        overrides=ConfigOverrides(
            profile="sqlite",
            principal_id=principal,
            sqlite_data_root=root,
        )
    )
    overrides = ConfigOverrides(
        principal_id=principal,
        sqlite_data_root=root,
        postgres_dsn_env=dsn_ref,
    )
    assert principal not in repr(sqlite)
    assert root not in repr(sqlite)
    assert principal not in repr(overrides)
    assert root not in repr(overrides)
    assert dsn_ref not in repr(overrides)

    secret_error = "postgresql://user:SYNTHETIC-SECRET@private.invalid/db"
    public_error = to_contract_error(ValueError(secret_error)).envelope().model_dump_json()
    assert secret_error not in public_error
    assert '"code":"internal_error"' in public_error


def test_cli_redacts_and_validate_serve_stop_before_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    for key in (*LISTENER_ENV_KEYS, *TRANSPORT_ENV_KEYS):
        monkeypatch.delenv(key, raising=False)
    secret = "postgresql://user:secret@private-host/private-db"
    bad = write(tmp_path / "private.toml", f'config_version=1\nunknown="{secret}"\n')
    assert cli.main(["config", "show", "--config", str(bad)]) == 2
    output = capsys.readouterr().err
    assert secret not in output and str(bad) not in output
    unavailable = write(
        tmp_path / "sqlite.toml",
        'config_version=1\nprofile="sqlite"\n[identity]\nprincipal_id="hidden"\n',
    )
    monkeypatch.setattr(
        cli, "compose_server", lambda _: (_ for _ in ()).throw(AssertionError("server started"))
    )
    assert cli.main(["config", "show", "--config", str(unavailable)]) == 0
    assert cli.main(["config", "validate", "--config", str(unavailable)]) == 0
    assert cli.main(["serve", "--config", str(unavailable)]) == 2


def test_cli_uses_one_environment_snapshot_for_preflight_and_resolution(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for key in (*LISTENER_ENV_KEYS, *TRANSPORT_ENV_KEYS):
        monkeypatch.delenv(key, raising=False)
    seen: list[dict[str, str]] = []
    original_preflight = cli.validate_stdio_only
    original_resolver = cli.resolve_configuration

    def preflight(transport=None, environ=None):
        seen.append(environ)
        return original_preflight(transport, environ)

    def resolver(*, overrides, environ, config_path):
        seen.append(environ)
        return original_resolver(
            overrides=overrides,
            environ=environ,
            config_path=config_path,
        )

    monkeypatch.setattr(cli, "validate_stdio_only", preflight)
    monkeypatch.setattr(cli, "resolve_configuration", resolver)
    assert cli.main(["config", "show"]) == 0
    assert len(seen) == 2
    assert seen[0] is seen[1]
    capsys.readouterr()


def test_serve_logging_configuration_is_stderr_only(monkeypatch: pytest.MonkeyPatch) -> None:
    config = resolve(overrides=ConfigOverrides(log_level="debug"))
    root_logger = cli.logging.getLogger()
    previous_level = root_logger.level
    try:
        cli._configure_stderr_logging(config)
        assert root_logger.level > cli.logging.CRITICAL
        logger = cli.logging.getLogger("aipcs_mcp")
        assert logger.propagate is False
        assert len(logger.handlers) == 1
        assert logger.handlers[0].stream is cli.sys.stderr
    finally:
        root_logger.setLevel(previous_level)


@pytest.mark.parametrize("argv", [["config", "show"], ["config", "validate"], ["serve"]])
def test_listener_preflight_precedes_resolution(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    monkeypatch.setenv("FASTMCP_PORT", "8000")
    monkeypatch.setattr(
        cli,
        "resolve_configuration",
        lambda **_: (_ for _ in ()).throw(AssertionError("resolver ran")),
    )
    assert cli.main(argv) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "transport_not_supported"


def test_configuration_uses_low_level_finite_tool_registration() -> None:
    source = Path(mcp_server.__file__).read_text(encoding="utf-8")
    assert "@server.call_tool(validate_input=False)" in source
    assert "@server.list_tools()" in source
    assert '"aipcs_service_seed"' in source
