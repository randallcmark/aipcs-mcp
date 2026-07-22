"""Allowlisted configuration reports."""

from __future__ import annotations

from .models import ResolvedConfiguration
from .resolver import is_supported_sqlite_platform


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
            "postgresql_dsn_configured": config.postgres_dsn_env is not None,
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
        config.profile == "sqlite" and is_supported_sqlite_platform()
    )
