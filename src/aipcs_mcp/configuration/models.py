"""Immutable configuration values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

Profile = Literal["stateless", "sqlite", "postgresql"]
Transport = Literal["stdio", "streamable-http"]
Source = Literal["cli", "environment", "file", "default"]
LogLevel = Literal["debug", "info", "warning", "error"]


@dataclass(frozen=True)
class ConfigOverrides:
    profile: str | None = None
    transport: str | None = None
    http_host: str | None = None
    http_port: str | None = None
    http_path: str | None = None
    http_allowed_hosts: str | None = None
    http_allowed_origins: str | None = None
    http_session_idle_timeout_seconds: str | None = None
    http_allow_non_loopback: str | None = None
    principal_id: str | None = field(default=None, repr=False)
    sqlite_data_root: str | None = field(default=None, repr=False)
    sqlite_busy_timeout_ms: str | None = None
    postgres_dsn_env: str | None = field(default=None, repr=False)
    postgres_connect_timeout_seconds: str | None = None
    postgres_lock_timeout_ms: str | None = None
    postgres_statement_timeout_ms: str | None = None
    log_level: str | None = None


@dataclass(frozen=True)
class ResolvedConfiguration:
    profile: Profile
    transport: Transport
    principal_id: str | None = field(repr=False)
    sqlite_data_root: Path | None = field(repr=False)
    postgres_dsn_env: str | None = field(repr=False)
    log_level: LogLevel
    sources: Mapping[str, Source]
    sqlite_busy_timeout_ms: int = 5000
    postgres_connect_timeout_seconds: int = 10
    postgres_lock_timeout_ms: int = 5000
    postgres_statement_timeout_ms: int = 30000
    http_host: str = field(default="127.0.0.1", repr=False)
    http_port: int = 8000
    http_path: str = field(default="/mcp", repr=False)
    http_allowed_hosts: tuple[str, ...] = field(default_factory=tuple, repr=False)
    http_allowed_origins: tuple[str, ...] = field(default_factory=tuple, repr=False)
    http_session_idle_timeout_seconds: int = 1800
    http_allow_non_loopback: bool = False

    def __post_init__(self) -> None:
        """Copy source metadata so direct construction cannot retain mutable state."""

        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))
        object.__setattr__(self, "http_allowed_hosts", tuple(self.http_allowed_hosts))
        object.__setattr__(self, "http_allowed_origins", tuple(self.http_allowed_origins))
