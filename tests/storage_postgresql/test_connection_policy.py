"""Private PostgreSQL DSN, startup-options, and SQLSTATE coverage."""

from __future__ import annotations

import pytest

from aipcs_mcp.configuration.models import ConfigOverrides
from aipcs_mcp.configuration.resolver import resolve_configuration
from aipcs_mcp.storage.errors import StorageUnavailable
from aipcs_mcp.storage.postgresql.connection import (
    PostgreSQLConnectionPolicy,
    PostgreSQLDsn,
    connect_postgresql,
    resolve_postgresql_dsn_for_serve,
)
from aipcs_mcp.storage.postgresql.result_codes import is_postgresql_busy


def _config():
    return resolve_configuration(
        overrides=ConfigOverrides(
            profile="postgresql",
            principal_id="operator",
            postgres_dsn_env="AIPCS_DSN",
            postgres_connect_timeout_seconds="6",
            postgres_lock_timeout_ms="400",
            postgres_statement_timeout_ms="9000",
        ),
        environ={},
        config_path=None,
    )


def test_dsn_is_read_once_at_serve_boundary_and_is_redacted() -> None:
    config = _config()

    class Environment(dict[str, str]):
        reads = 0

        def get(self, key: str, default=None):  # type: ignore[no-untyped-def]
            self.reads += 1
            return super().get(key, default)

    environment = Environment(AIPCS_DSN="postgresql://secret@private.invalid/aipcs")
    dsn = resolve_postgresql_dsn_for_serve(config, environment)
    assert environment.reads == 1
    assert repr(dsn) == "PostgreSQLDsn(<redacted>)"
    assert "secret" not in repr(dsn)


@pytest.mark.parametrize("value", [None, "", " \t", "postgresql://safe\x00unsafe", "x" * 4097])
def test_dsn_boundary_rejects_invalid_or_unbounded_secrets(value: object) -> None:
    config = _config()
    with pytest.raises(StorageUnavailable):
        resolve_postgresql_dsn_for_serve(config, {"AIPCS_DSN": value})  # type: ignore[dict-item]


def test_policy_uses_only_safe_generated_schema_and_fixed_startup_options() -> None:
    policy = PostgreSQLConnectionPolicy.from_configuration(_config(), schema="svc_" + "a" * 32)
    assert policy.options == (
        "-c search_path=svc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa,pg_catalog "
        "-c TimeZone=UTC -c client_encoding=UTF8 -c lock_timeout=400ms "
        "-c statement_timeout=9000ms"
    )
    with pytest.raises(StorageUnavailable):
        PostgreSQLConnectionPolicy.from_configuration(_config(), schema="public;DROP SCHEMA public")


def test_connect_imports_driver_only_at_explicit_connection_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Driver:
        @staticmethod
        def connect(dsn: str, **kwargs: object) -> object:
            captured["dsn"] = dsn
            captured.update(kwargs)
            return object()

    monkeypatch.setattr(
        "aipcs_mcp.storage.postgresql.connection.import_module", lambda name: Driver
    )
    result = connect_postgresql(
        PostgreSQLDsn("postgresql://secret@private.invalid/aipcs"),
        PostgreSQLConnectionPolicy.from_configuration(_config()),
    )
    assert type(result) is object
    assert captured["connect_timeout"] == 6
    assert captured["options"] == (
        "-c search_path=aipcs_registry,pg_catalog "
        "-c TimeZone=UTC -c client_encoding=UTF8 -c lock_timeout=400ms "
        "-c statement_timeout=9000ms"
    )


@pytest.mark.parametrize("state", ["40001", "40P01", "55P03", "57014"])
def test_retryable_postgresql_sqlstates_are_fixed(state: str) -> None:
    error = RuntimeError()
    error.sqlstate = state  # type: ignore[attr-defined]
    assert is_postgresql_busy(error)


@pytest.mark.parametrize("state", [None, "23505", "08006", "4001", "40001 "])
def test_nonretryable_or_malformed_sqlstates_are_not_busy(state: object) -> None:
    error = RuntimeError()
    error.sqlstate = state  # type: ignore[attr-defined]
    assert not is_postgresql_busy(error)
