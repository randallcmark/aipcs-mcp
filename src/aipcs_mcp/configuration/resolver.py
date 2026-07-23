"""Strict bounded TOML/environment/CLI resolver; no adapter side effects."""

from __future__ import annotations

import os
import re
import stat
import sys
import tomllib
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path
from typing import Any

from aipcs_mcp.errors import ErrorCode

from .errors import ConfigurationError
from .models import ConfigOverrides, ResolvedConfiguration

_FIELDS = {
    "profile": "AIPCS_PROFILE",
    "transport": "AIPCS_TRANSPORT",
    "principal_id": "AIPCS_PRINCIPAL_ID",
    "sqlite_data_root": "AIPCS_SQLITE_DATA_ROOT",
    "sqlite_busy_timeout_ms": "AIPCS_SQLITE_BUSY_TIMEOUT_MS",
    "postgres_dsn_env": "AIPCS_POSTGRES_DSN_ENV",
    "postgres_connect_timeout_seconds": "AIPCS_POSTGRES_CONNECT_TIMEOUT_SECONDS",
    "postgres_lock_timeout_ms": "AIPCS_POSTGRES_LOCK_TIMEOUT_MS",
    "postgres_statement_timeout_ms": "AIPCS_POSTGRES_STATEMENT_TIMEOUT_MS",
    "log_level": "AIPCS_LOG_LEVEL",
}
_DEFAULTS = {
    "profile": "stateless",
    "transport": "stdio",
    "principal_id": None,
    "sqlite_data_root": None,
    "sqlite_busy_timeout_ms": 5000,
    "postgres_dsn_env": None,
    "postgres_connect_timeout_seconds": 10,
    "postgres_lock_timeout_ms": 5000,
    "postgres_statement_timeout_ms": 30000,
    "log_level": "warning",
}
_DSN_ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


def resolve_configuration(
    *, overrides: ConfigOverrides, environ: Mapping[str, str], config_path: Path | None
) -> ResolvedConfiguration:
    file_values = _read_file(config_path)
    values: dict[str, object] = {}
    sources: dict[str, str] = {}
    for field, env_name in _FIELDS.items():
        cli_value = getattr(overrides, field)
        if cli_value is not None:
            values[field], sources[field] = cli_value, "cli"
        elif env_name in environ:
            values[field], sources[field] = environ[env_name], "environment"
        elif field in file_values:
            values[field], sources[field] = file_values[field], "file"
        else:
            values[field], sources[field] = _DEFAULTS[field], "default"
    profile = _choice(values["profile"], {"stateless", "sqlite", "postgresql"}, "profile")
    transport = _choice(values["transport"], {"stdio"}, "transport")
    principal = _text(values["principal_id"], "principal_id", optional=True, maximum=128)
    root = _absolute_path(values["sqlite_data_root"])
    if profile == "sqlite" and root is None:
        root = _default_sqlite_root(environ)
    dsn_ref = _dsn_reference(values["postgres_dsn_env"])
    level = _choice(values["log_level"], {"debug", "info", "warning", "error"}, "log_level")
    busy_timeout_ms = _sqlite_busy_timeout(
        values["sqlite_busy_timeout_ms"], sources["sqlite_busy_timeout_ms"]
    )
    postgres_connect_timeout_seconds = _bounded_timeout(
        values["postgres_connect_timeout_seconds"],
        sources["postgres_connect_timeout_seconds"],
        path="postgresql",
        minimum=1,
        maximum=60,
    )
    postgres_lock_timeout_ms = _bounded_timeout(
        values["postgres_lock_timeout_ms"],
        sources["postgres_lock_timeout_ms"],
        path="postgresql",
        minimum=1,
        maximum=30_000,
    )
    postgres_statement_timeout_ms = _bounded_timeout(
        values["postgres_statement_timeout_ms"],
        sources["postgres_statement_timeout_ms"],
        path="postgresql",
        minimum=1_000,
        maximum=300_000,
    )
    _validate_profile(
        profile,
        principal,
        root,
        dsn_ref,
        file_values,
        sqlite_busy_timeout_explicit=sources["sqlite_busy_timeout_ms"] != "default",
        postgres_timeouts_explicit=any(
            sources[field] != "default"
            for field in (
                "postgres_connect_timeout_seconds",
                "postgres_lock_timeout_ms",
                "postgres_statement_timeout_ms",
            )
        ),
        postgres_lock_timeout_ms=postgres_lock_timeout_ms,
        postgres_statement_timeout_ms=postgres_statement_timeout_ms,
    )
    return ResolvedConfiguration(
        profile=profile,
        transport=transport,
        principal_id=principal,
        sqlite_data_root=root,
        postgres_dsn_env=dsn_ref,
        log_level=level,
        sources=sources,
        sqlite_busy_timeout_ms=busy_timeout_ms,
        postgres_connect_timeout_seconds=postgres_connect_timeout_seconds,
        postgres_lock_timeout_ms=postgres_lock_timeout_ms,
        postgres_statement_timeout_ms=postgres_statement_timeout_ms,
    )


def require_runnable(config: ResolvedConfiguration) -> None:
    if config.profile == "sqlite" and (
        not is_supported_sqlite_platform() or not is_supported_sqlite_runtime()
    ):
        raise ConfigurationError(ErrorCode.UNSUPPORTED_OPERATION, "profile")


def is_supported_sqlite_platform() -> bool:
    """Return only the POSIX platforms certified by the SQLite location policy."""

    return sys.platform == "darwin" or sys.platform.startswith("linux")


def is_supported_sqlite_runtime() -> bool:
    """Return whether the loaded SQLite runtime includes the WAL-reset bug fix."""

    runtime = import_module("sqlite3")
    version = getattr(runtime, "sqlite_version_info", None)
    return (
        type(version) is tuple
        and len(version) >= 3
        and all(type(part) is int for part in version[:3])
        and version[:3] >= (3, 51, 3)
    )


def _read_file(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError
        with path.open("rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, tomllib.TOMLDecodeError):
        raise ConfigurationError(path="configuration") from None
    _bound_shape(parsed)
    version = parsed.get("config_version") if isinstance(parsed, dict) else None
    if not isinstance(parsed, dict) or type(version) is not int or version != 1:
        raise ConfigurationError(path="config_version")
    allowed = {
        "config_version",
        "profile",
        "transport",
        "identity",
        "sqlite",
        "postgresql",
        "logging",
    }
    if set(parsed) - allowed:
        raise ConfigurationError(path="configuration")
    sections = {
        name: parsed.get(name, {}) for name in ("identity", "sqlite", "postgresql", "logging")
    }
    allowed_sections = {
        "identity": {"principal_id"},
        "sqlite": {"data_root", "busy_timeout_ms"},
        "postgresql": {
            "dsn_env",
            "connect_timeout_seconds",
            "lock_timeout_ms",
            "statement_timeout_ms",
        },
        "logging": {"level"},
    }
    for name, section in sections.items():
        if not isinstance(section, dict) or set(section) - allowed_sections[name]:
            raise ConfigurationError(path=name)
    values = {
        "profile": parsed.get("profile"),
        "transport": parsed.get("transport"),
        "principal_id": sections["identity"].get("principal_id"),
        "sqlite_data_root": sections["sqlite"].get("data_root"),
        "sqlite_busy_timeout_ms": sections["sqlite"].get("busy_timeout_ms"),
        "postgres_dsn_env": sections["postgresql"].get("dsn_env"),
        "postgres_connect_timeout_seconds": sections["postgresql"].get(
            "connect_timeout_seconds"
        ),
        "postgres_lock_timeout_ms": sections["postgresql"].get("lock_timeout_ms"),
        "postgres_statement_timeout_ms": sections["postgresql"].get("statement_timeout_ms"),
        "log_level": sections["logging"].get("level"),
    }
    values["__sqlite_present__"] = "sqlite" in parsed
    values["__postgresql_present__"] = "postgresql" in parsed
    return {
        key: value for key, value in values.items() if value is not None or key.startswith("__")
    }


def _bound_shape(value: Any, depth: int = 0, count: list[int] | None = None) -> None:
    count = [0] if count is None else count
    if depth > 8:
        raise ConfigurationError(path="configuration")
    if isinstance(value, dict):
        count[0] += len(value)
        if count[0] > 128:
            raise ConfigurationError(path="configuration")
        for child in value.values():
            _bound_shape(child, depth + 1, count)
    elif isinstance(value, list):
        raise ConfigurationError(path="configuration")


def _choice(value: object, choices: set[str], path: str) -> str:
    if not isinstance(value, str) or not value or value not in choices:
        code = (
            ErrorCode.TRANSPORT_NOT_SUPPORTED
            if path == "transport"
            else ErrorCode.VALIDATION_FAILED
        )
        raise ConfigurationError(code, path)
    return value


def _text(value: object, path: str, *, optional: bool, maximum: int) -> str | None:
    if value is None and optional:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or not value.isprintable()
    ):
        raise ConfigurationError(path=path)
    return value


def _absolute_path(value: object) -> Path | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 4096
        or not value.isprintable()
    ):
        raise ConfigurationError(path="sqlite")
    path = Path(value)
    if not path.is_absolute():
        raise ConfigurationError(path="sqlite")
    return path


def _default_sqlite_root(environ: Mapping[str, str]) -> Path:
    """Select a pure, redacted platform default; never touch the filesystem."""

    if sys.platform == "darwin":
        return _platform_root(environ.get("HOME")) / "Library" / "Application Support" / "aipcs-mcp"
    if sys.platform == "win32":
        value = environ.get("LOCALAPPDATA")
        return _platform_root(value) / "aipcs-mcp"
    xdg = environ.get("XDG_DATA_HOME")
    if xdg is not None:
        return _platform_root(xdg) / "aipcs-mcp"
    return _platform_root(environ.get("HOME")) / ".local" / "share" / "aipcs-mcp"


def _platform_root(value: object) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not value.isprintable()
        or len(value) > 4096
    ):
        raise ConfigurationError(path="sqlite")
    path = Path(value)
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise ConfigurationError(path="sqlite")
    return path


def _dsn_reference(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _DSN_ENV.fullmatch(value):
        raise ConfigurationError(path="postgresql")
    return value


def _sqlite_busy_timeout(value: object, source: str) -> int:
    """Validate the sole SQLite busy-handler policy without coercion."""

    return _bounded_timeout(value, source, path="sqlite", minimum=1, maximum=30_000)


def _bounded_timeout(
    value: object, source: str, *, path: str, minimum: int, maximum: int
) -> int:
    """Validate one explicit timeout without accepting textual coercions."""

    if source in {"cli", "environment"}:
        if (
            not isinstance(value, str)
            or not re.fullmatch(r"(?:[1-9][0-9]*)", value)
            or len(value) > len(str(maximum))
        ):
            raise ConfigurationError(path=path)
        parsed = int(value)
    elif source in {"file", "default"}:
        if type(value) is not int:
            raise ConfigurationError(path=path)
        parsed = value
    else:
        raise ConfigurationError(path=path)
    if not minimum <= parsed <= maximum:
        raise ConfigurationError(path=path)
    return parsed


def _validate_profile(
    profile: str,
    principal: str | None,
    root: Path | None,
    dsn_ref: str | None,
    file_values: Mapping[str, object],
    *,
    sqlite_busy_timeout_explicit: bool,
    postgres_timeouts_explicit: bool,
    postgres_lock_timeout_ms: int,
    postgres_statement_timeout_ms: int,
) -> None:
    if profile != "stateless" and principal is None:
        raise ConfigurationError(path="principal_id")
    sqlite_present = bool(file_values.get("__sqlite_present__"))
    pg_present = bool(file_values.get("__postgresql_present__"))
    if profile == "stateless" and (
        sqlite_present
        or pg_present
        or root is not None
        or dsn_ref is not None
        or sqlite_busy_timeout_explicit
        or postgres_timeouts_explicit
    ):
        raise ConfigurationError(path="storage")
    if profile == "sqlite" and (pg_present or dsn_ref is not None or postgres_timeouts_explicit):
        raise ConfigurationError(path="postgresql")
    if profile == "postgresql" and (
        sqlite_present or root is not None or dsn_ref is None or sqlite_busy_timeout_explicit
    ):
        raise ConfigurationError(path="storage")
    if profile == "postgresql" and postgres_statement_timeout_ms < postgres_lock_timeout_ms:
        raise ConfigurationError(path="postgresql")
