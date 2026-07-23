"""SYNTHETIC_FIXTURE. Synthetic fixture provenance: disposable local PostgreSQL only.

This module is deliberately test-only.  It never accepts a database URL or
falls back to a running server: an integration run must opt in to a pinned,
already-local PostgreSQL image.  The container is addressed only by the exact
identifier returned from ``docker run`` and is removed by that identifier.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from types import MappingProxyType
from uuid import uuid4

import pytest

ACTIVATION_ENV = "AIPCS_POSTGRES_TESTS"
IMAGE_ENV = "AIPCS_POSTGRES_TEST_IMAGE"
MAJOR_ENV = "AIPCS_POSTGRES_TEST_MAJOR"
_ACTIVATION_VALUE = "container"
_SUPPORTED_MAJORS = frozenset({"16", "18"})
PINNED_POSTGRES_IMAGES = MappingProxyType(
    {
        "16": "postgres@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777",
        "18": "postgres@sha256:9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15",
    }
)
_LOOPBACK_PORT = re.compile(r"^127\.0\.0\.1:(?P<port>[1-9][0-9]{0,4})$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
_READY_ATTEMPTS = 40
_READY_DELAY_SECONDS = 0.25


class PostgresFixtureError(RuntimeError):
    """A bounded failure from the project-owned disposable fixture."""


@dataclass(frozen=True)
class PostgresContainerSettings:
    """Validated image choice for an explicit PostgreSQL major-version matrix leg."""

    image: str
    major: str

    @classmethod
    def optional_from_environ(
        cls, environ: Mapping[str, str]
    ) -> PostgresContainerSettings | None:
        """Return no settings unless both project-owned opt-in controls are set.

        Intentionally no DSN, libpq, or generic PostgreSQL environment variable
        is consulted here.  A missing/incomplete opt-in is a clean integration
        skip, not an invitation to find another database.
        """

        if environ.get(ACTIVATION_ENV) != _ACTIVATION_VALUE:
            return None
        image = environ.get(IMAGE_ENV)
        major = environ.get(MAJOR_ENV)
        if image is None or major is None:
            return None
        return cls(image=image, major=major)

    def __post_init__(self) -> None:
        if self.major not in _SUPPORTED_MAJORS:
            raise PostgresFixtureError("unsupported PostgreSQL test matrix entry")
        if self.image != PINNED_POSTGRES_IMAGES[self.major]:
            raise PostgresFixtureError("PostgreSQL test image must be a pinned matching image")


@dataclass(frozen=True)
class PostgresTestTarget:
    """Private connection details with a redacted representation."""

    host: str = field(repr=False)
    port: int = field(repr=False)
    database: str = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)

    @property
    def dsn(self) -> str:
        """Construct the ephemeral test DSN only for a test-owned adapter call."""

        return (
            f"postgresql://{self.username}:{self.password}@{self.host}:"
            f"{self.port}/{self.database}"
        )


Runner = Callable[[Sequence[str], bool], subprocess.CompletedProcess[str]]
Provisioner = Callable[[PostgresTestTarget, str, str], PostgresTestTarget]


def _run_docker(command: Sequence[str], check: bool) -> subprocess.CompletedProcess[str]:
    """Run one no-shell Docker command without surfacing its output to callers."""

    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise PostgresFixtureError("disposable PostgreSQL fixture command failed") from None
    if check and result.returncode:
        raise PostgresFixtureError("disposable PostgreSQL fixture command failed")
    return result


class DisposablePostgres:
    """One labeled, loopback-only container with exact-id cleanup ownership."""

    def __init__(
        self,
        settings: PostgresContainerSettings,
        *,
        runner: Runner = _run_docker,
        sleeper: Callable[[float], None] = time.sleep,
        provisioner: Provisioner | None = None,
    ) -> None:
        self._settings = settings
        self._runner = runner
        self._sleeper = sleeper
        self._provisioner = _provision_runtime_role if provisioner is None else provisioner
        self._container_id: str | None = None
        self._target: PostgresTestTarget | None = None

    @property
    def target(self) -> PostgresTestTarget:
        if self._target is None:
            raise PostgresFixtureError("disposable PostgreSQL fixture was not started")
        return self._target

    def start(self) -> PostgresTestTarget:
        """Start only the pinned image and wait for its private readiness probe."""

        if self._container_id is not None:
            raise PostgresFixtureError("disposable PostgreSQL fixture was already started")
        self._runner(
            ("docker", "image", "inspect", "--format", "{{.Id}}", self._settings.image),
            True,
        )
        run_id = uuid4().hex
        username = f"aipcs_init_{run_id[:12]}"
        database = f"aipcs_{run_id[:12]}"
        password = secrets.token_urlsafe(32)
        command = (
            "docker",
            "run",
            "--detach",
            "--rm",
            "--pull=never",
            "--label",
            "com.aipcs.test.fixture=postgresql",
            "--label",
            f"com.aipcs.test.run_id={run_id}",
            # An omitted host port asks Docker for one random port; the explicit
            # loopback address prevents LAN exposure.  No mount or volume flag
            # is used, and PGDATA avoids the image's normal persistent location.
            "--publish",
            "127.0.0.1::5432",
            "--env",
            f"POSTGRES_USER={username}",
            "--env",
            f"POSTGRES_DB={database}",
            "--env",
            f"POSTGRES_PASSWORD={password}",
            "--env",
            "PGDATA=/tmp/aipcs-postgresql",
            self._settings.image,
        )
        container_id = self._runner(command, True).stdout.strip()
        if _CONTAINER_ID.fullmatch(container_id) is None:
            raise PostgresFixtureError("disposable PostgreSQL fixture returned an invalid container id")
        self._container_id = container_id
        try:
            port_output = self._runner(("docker", "port", container_id, "5432/tcp"), True).stdout
            port = _parse_loopback_port(port_output)
            init_target = PostgresTestTarget(
                host="127.0.0.1",
                port=port,
                database=database,
                username=username,
                password=password,
            )
            self._wait_until_ready(init_target)
            target = self._provisioner(
                init_target,
                f"aipcs_runtime_{run_id[:12]}",
                secrets.token_urlsafe(32),
            )
            self._target = target
            return target
        except Exception:
            self._close(suppress_failure=True)
            raise

    def _wait_until_ready(self, target: PostgresTestTarget) -> None:
        container_id = self._require_container_id()
        command = (
            "docker",
            "exec",
            container_id,
            "pg_isready",
            "--quiet",
            "--host=127.0.0.1",
            f"--username={target.username}",
            f"--dbname={target.database}",
        )
        for attempt in range(_READY_ATTEMPTS):
            if self._runner(command, False).returncode == 0:
                return
            if attempt + 1 < _READY_ATTEMPTS:
                self._sleeper(_READY_DELAY_SECONDS)
        raise PostgresFixtureError("disposable PostgreSQL fixture did not become ready")

    def close(self) -> None:
        """Remove only this fixture's exact Docker id; never target labels or globals."""

        self._close(suppress_failure=False)

    def _close(self, *, suppress_failure: bool) -> None:
        container_id = self._container_id
        self._container_id = None
        self._target = None
        if container_id is None:
            return
        try:
            self._runner(("docker", "container", "rm", "--force", container_id), True)
        except PostgresFixtureError:
            if not suppress_failure:
                raise

    def _require_container_id(self) -> str:
        if self._container_id is None:
            raise PostgresFixtureError("disposable PostgreSQL fixture was not started")
        return self._container_id

    def __enter__(self) -> PostgresTestTarget:
        return self.start()

    def __exit__(self, error_type: object, _value: object, _traceback: object) -> None:
        self._close(suppress_failure=error_type is not None)


def _parse_loopback_port(value: str) -> int:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) != 1:
        raise PostgresFixtureError("disposable PostgreSQL fixture returned an invalid port")
    match = _LOOPBACK_PORT.fullmatch(lines[0])
    if match is None:
        raise PostgresFixtureError("disposable PostgreSQL fixture returned a non-loopback port")
    port = int(match.group("port"))
    if not 1 <= port <= 65_535:
        raise PostgresFixtureError("disposable PostgreSQL fixture returned an invalid port")
    return port


def _provision_runtime_role(
    init_target: PostgresTestTarget, runtime_username: str, runtime_password: str
) -> PostgresTestTarget:
    """Provision the only runtime role through a lazy Psycopg setup connection.

    The image-init user is a PostgreSQL superuser by image design.  It exists
    only for this container-local setup transaction; test adapters receive the
    returned non-superuser role instead.  Credentials never enter a subprocess
    argument, Docker label, or raised diagnostic.
    """

    try:
        driver = import_module("psycopg")
        sql = driver.sql
        connection = driver.connect(init_target.dsn, connect_timeout=10)
        with connection:
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(runtime_username), sql.Literal(runtime_password))
            )
            connection.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(init_target.database)
                )
            )
            connection.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            connection.execute(
                sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO {}").format(
                    sql.Identifier(init_target.database), sql.Identifier(runtime_username)
                )
            )
        connection.close()
    except Exception:
        raise PostgresFixtureError("disposable PostgreSQL runtime role setup failed") from None
    return PostgresTestTarget(
        host=init_target.host,
        port=init_target.port,
        database=init_target.database,
        username=runtime_username,
        password=runtime_password,
    )


@pytest.fixture
def postgres_test_target() -> PostgresTestTarget:
    """Yield an opt-in disposable target or skip before any Docker subprocess call."""

    settings = PostgresContainerSettings.optional_from_environ(os.environ)
    if settings is None:
        pytest.skip(
            "PostgreSQL integration is disabled; set AIPCS_POSTGRES_TESTS=container, "
            "AIPCS_POSTGRES_TEST_MAJOR, and a pinned AIPCS_POSTGRES_TEST_IMAGE."
        )
    fixture = DisposablePostgres(settings)
    try:
        yield fixture.start()
    finally:
        fixture.close()
