"""Private PostgreSQL DSN and connection-policy boundary.

Configuration resolution keeps only a DSN environment-variable reference. The
secret is read exactly once by the future ``serve`` composition path, after
offline ``config show`` and ``config validate`` have completed. This module
does not import psycopg until a caller explicitly opens a connection.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import import_module

from aipcs_mcp.configuration.models import ResolvedConfiguration
from aipcs_mcp.storage.errors import StorageUnavailable

_MAX_DSN_LENGTH = 4096
_REGISTRY_SCHEMA = "aipcs_registry"
_SERVICE_SCHEMA = re.compile(r"^svc_[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True, repr=False)
class PostgreSQLDsn:
    """An in-memory secret which never renders its value."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.value) is not str
            or not self.value.strip()
            or "\x00" in self.value
            or len(self.value) > _MAX_DSN_LENGTH
        ):
            raise StorageUnavailable()

    def __repr__(self) -> str:
        return "PostgreSQLDsn(<redacted>)"


@dataclass(frozen=True, slots=True)
class PostgreSQLConnectionPolicy:
    """One safe startup-policy value for a fixed private schema."""

    schema: str
    connect_timeout_seconds: int
    lock_timeout_ms: int
    statement_timeout_ms: int

    def __post_init__(self) -> None:
        if (
            type(self.schema) is not str
            or not _safe_schema(self.schema)
            or type(self.connect_timeout_seconds) is not int
            or not 1 <= self.connect_timeout_seconds <= 60
            or type(self.lock_timeout_ms) is not int
            or not 1 <= self.lock_timeout_ms <= 30_000
            or type(self.statement_timeout_ms) is not int
            or not 1_000 <= self.statement_timeout_ms <= 300_000
            or self.statement_timeout_ms < self.lock_timeout_ms
        ):
            raise StorageUnavailable()

    @classmethod
    def from_configuration(
        cls, config: ResolvedConfiguration, *, schema: str = _REGISTRY_SCHEMA
    ) -> PostgreSQLConnectionPolicy:
        if type(config) is not ResolvedConfiguration or config.profile != "postgresql":
            raise StorageUnavailable()
        return cls(
            schema=schema,
            connect_timeout_seconds=config.postgres_connect_timeout_seconds,
            lock_timeout_ms=config.postgres_lock_timeout_ms,
            statement_timeout_ms=config.postgres_statement_timeout_ms,
        )

    @property
    def options(self) -> str:
        """Return finite, trusted libpq startup options without DSN interpolation."""

        return (
            f"-c search_path={self.schema},pg_catalog "
            f"-c TimeZone=UTC "
            f"-c client_encoding=UTF8 "
            f"-c lock_timeout={self.lock_timeout_ms}ms "
            f"-c statement_timeout={self.statement_timeout_ms}ms"
        )


def resolve_postgresql_dsn_for_serve(
    config: ResolvedConfiguration, environ: Mapping[str, str]
) -> PostgreSQLDsn:
    """Read the configured secret once, only for future serve composition."""

    if (
        type(config) is not ResolvedConfiguration
        or config.profile != "postgresql"
        or type(config.postgres_dsn_env) is not str
        or not config.postgres_dsn_env
        or not isinstance(environ, Mapping)
    ):
        raise StorageUnavailable()
    value = environ.get(config.postgres_dsn_env)
    return PostgreSQLDsn(value)  # type: ignore[arg-type]


def connect_postgresql(dsn: PostgreSQLDsn, policy: PostgreSQLConnectionPolicy) -> object:
    """Open one synchronous driver connection under the fixed safe policy."""

    if type(dsn) is not PostgreSQLDsn or type(policy) is not PostgreSQLConnectionPolicy:
        raise StorageUnavailable()
    try:
        driver = import_module("psycopg")
        connect = driver.connect
        if not callable(connect):
            raise ValueError
        return connect(
            dsn.value,
            connect_timeout=policy.connect_timeout_seconds,
            options=policy.options,
        )
    except Exception:
        raise StorageUnavailable() from None


def _safe_schema(value: str) -> bool:
    return value == _REGISTRY_SCHEMA or _SERVICE_SCHEMA.fullmatch(value) is not None
