"""Immutable configuration values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

Profile = Literal["stateless", "sqlite", "postgresql"]
Source = Literal["cli", "environment", "file", "default"]
LogLevel = Literal["debug", "info", "warning", "error"]


@dataclass(frozen=True)
class ConfigOverrides:
    profile: str | None = None
    transport: str | None = None
    principal_id: str | None = field(default=None, repr=False)
    sqlite_data_root: str | None = field(default=None, repr=False)
    sqlite_busy_timeout_ms: str | None = None
    postgres_dsn_env: str | None = field(default=None, repr=False)
    log_level: str | None = None


@dataclass(frozen=True)
class ResolvedConfiguration:
    profile: Profile
    transport: Literal["stdio"]
    principal_id: str | None = field(repr=False)
    sqlite_data_root: Path | None = field(repr=False)
    postgres_dsn_env: str | None = field(repr=False)
    log_level: LogLevel
    sources: Mapping[str, Source]
    sqlite_busy_timeout_ms: int = 5000

    def __post_init__(self) -> None:
        """Copy source metadata so direct construction cannot retain mutable state."""

        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))
