"""Allowlisted configuration reports."""

from __future__ import annotations

from .models import ResolvedConfiguration


def safe_config_report(config: ResolvedConfiguration) -> dict[str, object]:
    return {
        "config_version": 1,
        "profile": config.profile,
        "available": config.profile == "stateless",
        "runnable": config.profile == "stateless",
        "transport": config.transport,
        "identity": {"principal_configured": config.principal_id is not None},
        "logging": {"level": config.log_level},
        "storage": {
            "sqlite_data_root_configured": config.sqlite_data_root is not None,
            "postgresql_dsn_configured": config.postgres_dsn_env is not None,
        },
        "sources": dict(config.sources),
    }


def safe_validation_report(config: ResolvedConfiguration) -> dict[str, object]:
    return {
        "config_version": 1,
        "valid": True,
        "profile": config.profile,
        "runnable": config.profile == "stateless",
    }
