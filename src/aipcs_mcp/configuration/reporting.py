"""Allowlisted configuration reports."""

from __future__ import annotations

from .models import ResolvedConfiguration
from .resolver import is_supported_sqlite_platform, is_supported_sqlite_runtime


def safe_config_report(config: ResolvedConfiguration) -> dict[str, object]:
    runnable = _structurally_runnable(config)
    return {
        "config_version": 1,
        "profile": config.profile,
        "available": runnable,
        "runnable": runnable,
        "transport": config.transport,
        "identity": {"principal_configured": config.principal_id is not None},
        "logging": {"level": config.log_level},
        "storage": {
            "sqlite_data_root_configured": config.sources["sqlite_data_root"] != "default",
            "sqlite_busy_timeout_ms": config.sqlite_busy_timeout_ms,
            "postgresql_dsn_configured": config.postgres_dsn_env is not None,
            "postgresql_connect_timeout_seconds": config.postgres_connect_timeout_seconds,
            "postgresql_lock_timeout_ms": config.postgres_lock_timeout_ms,
            "postgresql_statement_timeout_ms": config.postgres_statement_timeout_ms,
        },
        "streamable_http": {
            "configured": config.transport == "streamable-http",
            "host_configured": config.sources["http_host"] != "default",
            "port_configured": config.sources["http_port"] != "default",
            "path_configured": config.sources["http_path"] != "default",
            "host_policy_configured": config.sources["http_allowed_hosts"] != "default",
            "origin_policy_configured": config.sources["http_allowed_origins"] != "default",
            "session_idle_timeout_seconds": config.http_session_idle_timeout_seconds,
            "non_loopback_explicitly_allowed": config.http_allow_non_loopback,
        },
        "sources": dict(config.sources),
    }


def safe_validation_report(config: ResolvedConfiguration) -> dict[str, object]:
    return {
        "config_version": 1,
        "valid": True,
        "profile": config.profile,
        "runnable": _structurally_runnable(config),
    }


def _structurally_runnable(config: ResolvedConfiguration) -> bool:
    return config.profile == "stateless" or (
        config.profile == "sqlite"
        and is_supported_sqlite_platform()
        and is_supported_sqlite_runtime()
    ) or config.profile == "postgresql"
