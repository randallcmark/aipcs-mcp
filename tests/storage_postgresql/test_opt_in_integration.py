"""SYNTHETIC_FIXTURE. Synthetic fixture provenance: explicit disposable-container smoke."""

from __future__ import annotations

from importlib import import_module

from .container import PostgresTestTarget


def test_project_owned_postgres_target_is_loopback_only(
    postgres_test_target: PostgresTestTarget,
) -> None:
    """Runs only when the fixture's three explicit project-owned controls are set."""

    assert postgres_test_target.host == "127.0.0.1"
    assert 1 <= postgres_test_target.port <= 65_535
    driver = import_module("psycopg")
    with driver.connect(postgres_test_target.dsn) as connection:
        role = connection.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
            "FROM pg_catalog.pg_roles WHERE rolname = current_user"
        ).fetchone()
        assert role == (False, False, False, False, False)
        assert connection.execute(
            "SELECT has_database_privilege(current_user, current_database(), 'CONNECT'), "
            "has_database_privilege(current_user, current_database(), 'CREATE')"
        ).fetchone() == (True, True)
        assert connection.execute(
            "SELECT has_schema_privilege('public', 'USAGE'), "
            "has_schema_privilege('public', 'CREATE')"
        ).fetchone() == (False, False)
