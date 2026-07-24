#!/usr/bin/env python3
"""Rehearse the AIPCS release boundary without touching the checkout.

This intentionally has no project imports.  It verifies the distributions actually
built from the checkout, then repeats source gates from a staged non-linked copy.
It is designed for a trusted developer machine with a primed, locked uv cache.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tarfile import open as open_tar
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
MAX_OUTPUT_CHARS = 4_000
DEFAULT_TIMEOUT_SECONDS = 300
SMOKE_TIMEOUT_SECONDS = 60
POSTGRES_SMOKE_TIMEOUT_SECONDS = 90
POSTGRES_RELEASE_DSN_ENV = "AIPCS_RELEASE_POSTGRES_DSN"
_POSTGRES_RELEASE_IMAGES = {
    "16": "postgres@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777",
    "18": "postgres@sha256:9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15",
}
_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
_LOOPBACK_PORT = re.compile(r"^127\.0\.0\.1:(?P<port>[1-9][0-9]{0,4})$")
_FROZEN_R1_MIGRATION_ID = "registry-0001-initial"
_FROZEN_R1_CHECKSUM = "d40691d8ae8e09b10767b262ac716bc1689c52f4887770d9f43cd84679d291bc"
_FROZEN_R2_MIGRATION_ID = "registry-0002-durable-intent"
_FROZEN_R2_CHECKSUM = "b6190247d4f709728bab59cb4eb5fd149d4b7424472615f377b83f5191e0d8ea"
_FROZEN_R2_DDL = ('CREATE TABLE "aipcs_registry_service" (\n    "service_id" TEXT PRIMARY KEY NOT NULL CHECK (\n        length("service_id") = 36 AND "service_id" = lower("service_id")\n        AND substr("service_id", 9, 1) = \'-\' AND substr("service_id", 14, 1) = \'-\'\n        AND substr("service_id", 19, 1) = \'-\' AND substr("service_id", 24, 1) = \'-\'\n        AND replace("service_id", \'-\', \'\') NOT GLOB \'*[^0-9a-f]*\'\n        AND length(replace("service_id", \'-\', \'\')) = 32\n        AND replace("service_id", \'-\', \'\') != \'00000000000000000000000000000000\'\n    ),\n    "principal_id" TEXT NOT NULL CHECK (length("principal_id") BETWEEN 1 AND 128),\n    "domain_name" TEXT NOT NULL CHECK (length("domain_name") BETWEEN 1 AND 63),\n    "domain_class" TEXT NOT NULL CHECK (length("domain_class") BETWEEN 1 AND 64),\n    "intent_description" TEXT NOT NULL CHECK (length("intent_description") BETWEEN 1 AND 1000),\n    "created_at" TEXT NOT NULL CHECK (length("created_at") = 27),\n    "updated_at" TEXT NOT NULL CHECK (length("updated_at") = 27),\n    "last_activity_at" TEXT NOT NULL CHECK (length("last_activity_at") = 27),\n    "manifest_json" TEXT,\n    "schema_version" INTEGER CHECK ("schema_version" IS NULL OR "schema_version" BETWEEN 1 AND 65),\n    "design_state" TEXT NOT NULL CHECK ("design_state" IN (\'seeded\', \'materialised\')),\n    "operational_status" TEXT NOT NULL CHECK (\n        "operational_status" IN (\'active\', \'suspended\', \'archived\')\n    ),\n    "service_revision" INTEGER NOT NULL CHECK (\n        "service_revision" BETWEEN 1 AND 9223372036854775807\n    ),\n    "materialised_at" TEXT CHECK (\n        "materialised_at" IS NULL OR length("materialised_at") = 27\n    ),\n    "storage_backend" TEXT CHECK (\n        "storage_backend" IS NULL OR "storage_backend" IN (\'sqlite\', \'postgresql\')\n    ),\n    "storage_namespace" TEXT CHECK (\n        "storage_namespace" IS NULL OR (\n            length("storage_namespace") = 36\n            AND substr("storage_namespace", 1, 4) = \'svc_\'\n            AND substr("storage_namespace", 5) NOT GLOB \'*[^0-9a-f]*\'\n            AND substr("storage_namespace", 5) != \'00000000000000000000000000000000\'\n        )\n    ),\n    CHECK (\n        ("manifest_json" IS NULL AND "schema_version" IS NULL)\n        OR ("manifest_json" IS NOT NULL AND "schema_version" IS NOT NULL)\n    ),\n    CHECK (\n        ("design_state" = \'seeded\' AND "materialised_at" IS NULL\n            AND "storage_backend" IS NULL AND "storage_namespace" IS NULL)\n        OR ("design_state" = \'materialised\' AND "manifest_json" IS NOT NULL\n            AND "materialised_at" IS NOT NULL AND "storage_backend" IS NOT NULL\n            AND "storage_namespace" IS NOT NULL\n            AND substr("storage_namespace", 5) = replace("service_id", \'-\', \'\'))\n    ),\n    UNIQUE ("service_id", "principal_id"),\n    UNIQUE ("principal_id", "domain_name")\n) STRICT', 'CREATE TABLE "aipcs_registry_mutation" (\n    "principal_id" TEXT NOT NULL CHECK (length("principal_id") BETWEEN 1 AND 128),\n    "idempotency_key" TEXT NOT NULL CHECK (length("idempotency_key") BETWEEN 1 AND 128),\n    "fingerprint" TEXT NOT NULL CHECK (\n        length("fingerprint") = 64 AND "fingerprint" = lower("fingerprint")\n        AND "fingerprint" NOT GLOB \'*[^0-9a-f]*\'\n    ),\n    "service_id" TEXT NOT NULL,\n    "operation_kind" TEXT NOT NULL CHECK (\n        "operation_kind" IN (\'legacy\', \'seed\', \'design\', \'materialise\', \'evolve\')\n    ),\n    "phase" TEXT NOT NULL CHECK (\n        "phase" IN (\'prepared\', \'completed\', \'recovery_required\')\n    ),\n    "created_via" TEXT CHECK (\n        "created_via" IS NULL OR length("created_via") BETWEEN 1 AND 64\n    ),\n    "expected_service_revision" INTEGER CHECK (\n        "expected_service_revision" IS NULL\n        OR "expected_service_revision" BETWEEN 1 AND 9223372036854775807\n    ),\n    "expected_schema_version" INTEGER CHECK (\n        "expected_schema_version" IS NULL OR "expected_schema_version" BETWEEN 1 AND 65\n    ),\n    "target_manifest_json" TEXT,\n    "result_json" TEXT,\n    "recovery_category" TEXT CHECK (\n        "recovery_category" IS NULL OR "recovery_category" = \'recovery_required\'\n    ),\n    PRIMARY KEY ("principal_id", "idempotency_key"),\n    FOREIGN KEY ("service_id", "principal_id")\n        REFERENCES "aipcs_registry_service" ("service_id", "principal_id")\n        ON UPDATE RESTRICT ON DELETE RESTRICT,\n    CHECK (\n        (\n            "operation_kind" IN (\'legacy\', \'seed\', \'design\')\n            AND "phase" = \'completed\' AND "created_via" IS NULL\n            AND "expected_service_revision" IS NULL\n            AND "expected_schema_version" IS NULL\n            AND "target_manifest_json" IS NULL\n            AND "result_json" IS NOT NULL AND "recovery_category" IS NULL\n        )\n        OR (\n            "operation_kind" = \'materialise\' AND "created_via" IS NOT NULL\n            AND "expected_service_revision" IS NOT NULL\n            AND "expected_schema_version" IS NOT NULL\n            AND "expected_schema_version" = 1\n            AND "target_manifest_json" IS NOT NULL\n            AND (\n                ("phase" = \'prepared\' AND "result_json" IS NULL\n                    AND "recovery_category" IS NULL)\n                OR ("phase" = \'completed\' AND "result_json" IS NOT NULL\n                    AND "recovery_category" IS NULL)\n                OR ("phase" = \'recovery_required\' AND "result_json" IS NULL\n                    AND "recovery_category" IS NOT NULL\n                    AND "recovery_category" = \'recovery_required\')\n            )\n        )\n        OR (\n            "operation_kind" = \'evolve\' AND "created_via" IS NOT NULL\n            AND "expected_service_revision" IS NOT NULL\n            AND "expected_schema_version" IS NOT NULL\n            AND "expected_schema_version" BETWEEN 1 AND 64\n            AND "target_manifest_json" IS NOT NULL\n            AND (\n                ("phase" = \'prepared\' AND "result_json" IS NULL\n                    AND "recovery_category" IS NULL)\n                OR ("phase" = \'completed\' AND "result_json" IS NOT NULL\n                    AND "recovery_category" IS NULL)\n                OR ("phase" = \'recovery_required\' AND "result_json" IS NULL\n                    AND "recovery_category" IS NOT NULL\n                    AND "recovery_category" = \'recovery_required\')\n            )\n        )\n    )\n) STRICT', 'CREATE TABLE "aipcs_registry_audit" (\n    "audit_id" INTEGER PRIMARY KEY,\n    "action" TEXT NOT NULL CHECK (\n        "action" IN (\'seed\', \'design\', \'materialise\', \'evolve\')\n    ),\n    "outcome" TEXT NOT NULL CHECK (\n        "outcome" IN (\'created\', \'duplicate\', \'accepted\', \'completed\', \'recovery_required\')\n    ),\n    "service_id" TEXT NOT NULL,\n    "principal_id" TEXT NOT NULL CHECK (length("principal_id") BETWEEN 1 AND 128),\n    "created_via" TEXT NOT NULL CHECK (length("created_via") BETWEEN 1 AND 64),\n    "at" TEXT NOT NULL CHECK (length("at") = 27),\n    CHECK (\n        ("action" = \'seed\' AND "outcome" IN (\'created\', \'duplicate\'))\n        OR ("action" = \'design\' AND "outcome" = \'accepted\')\n        OR ("action" IN (\'materialise\', \'evolve\')\n            AND "outcome" IN (\'completed\', \'recovery_required\'))\n    ),\n    FOREIGN KEY ("service_id", "principal_id")\n        REFERENCES "aipcs_registry_service" ("service_id", "principal_id")\n        ON UPDATE RESTRICT ON DELETE RESTRICT\n) STRICT', 'CREATE INDEX "aipcs_registry_service_list"\n    ON "aipcs_registry_service" ("principal_id", "created_at" ASC, "service_id" ASC)', 'CREATE UNIQUE INDEX "aipcs_registry_mutation_active_lifecycle"\n    ON "aipcs_registry_mutation" ("principal_id", "service_id")\n    WHERE "operation_kind" IN (\'materialise\', \'evolve\')\n      AND "phase" IN (\'prepared\', \'recovery_required\')', 'CREATE INDEX "aipcs_registry_audit_principal"\n    ON "aipcs_registry_audit" ("principal_id", "audit_id" DESC)')

_FROZEN_R1_DDL = (
    """CREATE TABLE "aipcs_registry_meta" (
    "singleton" INTEGER PRIMARY KEY NOT NULL CHECK ("singleton" = 1),
    "adapter_id" TEXT NOT NULL CHECK ("adapter_id" = 'aipcs.sqlite.registry'),
    "component" TEXT NOT NULL CHECK ("component" = 'registry'),
    "applied_revision" INTEGER NOT NULL CHECK ("applied_revision" >= 0),
    "dirty" INTEGER NOT NULL CHECK ("dirty" IN (0, 1))
) STRICT""",
    """CREATE TABLE "aipcs_registry_migration" (
    "component" TEXT NOT NULL CHECK ("component" = 'registry'),
    "revision" INTEGER NOT NULL CHECK ("revision" > 0),
    "migration_id" TEXT NOT NULL CHECK (length("migration_id") BETWEEN 1 AND 96),
    "checksum" TEXT NOT NULL CHECK (
        length("checksum") = 64 AND "checksum" = lower("checksum")
        AND "checksum" NOT GLOB '*[^0-9a-f]*'
    ),
    "applied_at" TEXT NOT NULL CHECK (
        length("applied_at") = 27 AND substr("applied_at", 11, 1) = 'T'
        AND substr("applied_at", 20, 1) = '.' AND substr("applied_at", 27, 1) = 'Z'
    ),
    PRIMARY KEY ("component", "revision"),
    UNIQUE ("component", "migration_id")
) STRICT""",
    """CREATE TABLE "aipcs_registry_service" (
    "service_id" TEXT PRIMARY KEY NOT NULL CHECK (
        length("service_id") = 36 AND "service_id" = lower("service_id")
        AND substr("service_id", 9, 1) = '-' AND substr("service_id", 14, 1) = '-'
        AND substr("service_id", 19, 1) = '-' AND substr("service_id", 24, 1) = '-'
        AND replace("service_id", '-', '') NOT GLOB '*[^0-9a-f]*'
        AND length(replace("service_id", '-', '')) = 32
        AND replace("service_id", '-', '') != '00000000000000000000000000000000'
    ),
    "principal_id" TEXT NOT NULL CHECK (length("principal_id") BETWEEN 1 AND 128),
    "domain_name" TEXT NOT NULL CHECK (length("domain_name") BETWEEN 1 AND 63),
    "domain_class" TEXT NOT NULL CHECK (length("domain_class") BETWEEN 1 AND 64),
    "intent_description" TEXT NOT NULL CHECK (length("intent_description") BETWEEN 1 AND 1000),
    "created_at" TEXT NOT NULL CHECK (length("created_at") = 27),
    "updated_at" TEXT NOT NULL CHECK (length("updated_at") = 27),
    "last_activity_at" TEXT NOT NULL CHECK (length("last_activity_at") = 27),
    "manifest_json" TEXT,
    "schema_version" INTEGER CHECK ("schema_version" IS NULL OR "schema_version" >= 1),
    "design_state" TEXT NOT NULL CHECK ("design_state" IN ('seeded', 'materialised')),
    "operational_status" TEXT NOT NULL CHECK (
        "operational_status" IN ('active', 'suspended', 'archived')
    ),
    CHECK (
        ("manifest_json" IS NULL AND "schema_version" IS NULL)
        OR ("manifest_json" IS NOT NULL AND "schema_version" IS NOT NULL)
    ),
    UNIQUE ("service_id", "principal_id"),
    UNIQUE ("principal_id", "domain_name")
) STRICT""",
    """CREATE TABLE "aipcs_registry_mutation" (
    "principal_id" TEXT NOT NULL CHECK (length("principal_id") BETWEEN 1 AND 128),
    "idempotency_key" TEXT NOT NULL CHECK (length("idempotency_key") BETWEEN 1 AND 128),
    "fingerprint" TEXT NOT NULL CHECK (
        length("fingerprint") = 64 AND "fingerprint" = lower("fingerprint")
        AND "fingerprint" NOT GLOB '*[^0-9a-f]*'
    ),
    "service_id" TEXT NOT NULL,
    "result_json" TEXT NOT NULL,
    PRIMARY KEY ("principal_id", "idempotency_key"),
    FOREIGN KEY ("service_id", "principal_id")
        REFERENCES "aipcs_registry_service" ("service_id", "principal_id")
        ON UPDATE RESTRICT ON DELETE RESTRICT
) STRICT""",
    """CREATE TABLE "aipcs_registry_audit" (
    "audit_id" INTEGER PRIMARY KEY,
    "action" TEXT NOT NULL CHECK ("action" IN ('seed', 'design')),
    "outcome" TEXT NOT NULL CHECK ("outcome" IN ('created', 'duplicate', 'accepted')),
    "service_id" TEXT NOT NULL,
    "principal_id" TEXT NOT NULL CHECK (length("principal_id") BETWEEN 1 AND 128),
    "created_via" TEXT NOT NULL CHECK (length("created_via") BETWEEN 1 AND 64),
    "at" TEXT NOT NULL CHECK (length("at") = 27),
    CHECK (
        ("action" = 'seed' AND "outcome" IN ('created', 'duplicate'))
        OR ("action" = 'design' AND "outcome" = 'accepted')
    ),
    FOREIGN KEY ("service_id", "principal_id")
        REFERENCES "aipcs_registry_service" ("service_id", "principal_id")
        ON UPDATE RESTRICT ON DELETE RESTRICT
) STRICT""",
    """CREATE INDEX "aipcs_registry_service_list"
    ON "aipcs_registry_service" ("principal_id", "created_at" ASC, "service_id" ASC)""",
)
_SCRUBBED_ENVIRONMENT_KEYS = frozenset(
    {
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    }
)
_GENERATED_DIRECTORY_NAMES = frozenset(
    {
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
        "htmlcov",
    }
)
_GENERATED_FILE_SUFFIXES = frozenset(
    {
        ".bak",
        ".db",
        ".db3",
        ".orig",
        ".pyc",
        ".pyo",
        ".rej",
        ".sqlite",
        ".sqlite3",
        ".swn",
        ".swo",
        ".swp",
        ".temp",
        ".tmp",
    }
)
_GENERATED_FILE_NAMES = frozenset({".coverage", ".ds_store", "coverage.xml"})
_GENERATED_FILE_ENDINGS = ("-journal", "-wal", "-shm")
_PRIVATE_DIRECTORY_NAMES = frozenset(
    {
        ".agents",
        ".claude",
        ".codex",
        ".cursor",
        ".vscode",
        "archives",
        "captures",
        "exec-plans",
        "private-data",
        "research",
        "transcripts",
    }
)
_PRIVATE_FILE_NAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        "agents.md",
        "claude.md",
        "credentials.json",
        "credentials.yaml",
        "credentials.yml",
        "destination-sha256.tsv",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
        "source-sha256.tsv",
    }
)
_PRIVATE_FILE_SUFFIXES = frozenset(
    {".cer", ".crt", ".key", ".p12", ".pem", ".pfx"}
)
_PRIVATE_TRANSCRIPT = re.compile(
    r"(?:^|/)(?:20\d{2}-\d{2}-\d{2}-.+|[^/]*(?:transcript|session)[^/]*)"
    r"\.(?:txt|md|json)$",
    re.I,
)


class ReleaseVerificationError(RuntimeError):
    """A safe, locally actionable release-verification failure."""


@dataclass(frozen=True)
class ArtifactSet:
    """The one wheel and one source distribution from a single build."""

    wheel: Path
    sdist: Path


@dataclass(frozen=True)
class StageResult:
    """A successful named external command, retained for the final summary."""

    label: str


@dataclass(frozen=True)
class CleanCopy:
    """A fully regular-file staged repository snapshot."""

    root: Path
    source_files: tuple[Path, ...]
    copied_files: tuple[Path, ...]


@dataclass(frozen=True, repr=False)
class PostgreSQLReleaseTarget:
    """Ephemeral verifier-owned runtime credentials; never render their values."""

    host: str
    port: int
    database: str
    username: str
    password: str

    @property
    def dsn(self) -> str:
        return f"postgresql://{self.username}:{self.password}@{self.host}:{self.port}/{self.database}"


def postgresql_release_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Discard every ambient database selector before the verifier creates its own."""

    environment = scrubbed_environment(base)
    for key in tuple(environment):
        upper = key.upper()
        if (
            upper.startswith("PG")
            or upper.startswith("POSTGRES")
            or upper.startswith("AIPCS_POSTGRES")
            or upper == POSTGRES_RELEASE_DSN_ENV
            or upper in {"DATABASE_URL", "DB_URL", "DB_DSN"}
        ):
            environment.pop(key, None)
    return environment


def _container_command(
    command: Sequence[str], *, input_text: str | None = None
) -> str:
    """Run one verifier-owned Docker command without retaining its diagnostics."""

    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ReleaseVerificationError("PostgreSQL release fixture command failed.") from None
    if completed.returncode:
        raise ReleaseVerificationError("PostgreSQL release fixture command failed.")
    return completed.stdout


def _release_loopback_port(value: str) -> int:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ReleaseVerificationError("PostgreSQL release fixture returned an invalid port.")
    matched = _LOOPBACK_PORT.fullmatch(lines[0])
    if matched is None:
        raise ReleaseVerificationError("PostgreSQL release fixture returned a non-loopback port.")
    port = int(matched.group("port"))
    if not 1 <= port <= 65_535:
        raise ReleaseVerificationError("PostgreSQL release fixture returned an invalid port.")
    return port


class DisposablePostgreSQLRelease:
    """Create and remove only one exact-id, pinned, loopback PostgreSQL container."""

    def __init__(self, major: str, *, runner: object = _container_command) -> None:
        image = _POSTGRES_RELEASE_IMAGES.get(major)
        if image is None:
            raise ReleaseVerificationError("Unsupported PostgreSQL release matrix entry.")
        if not callable(runner):
            raise ReleaseVerificationError("PostgreSQL release fixture is unavailable.")
        self._image = image
        self._runner = runner
        self._container_id: str | None = None

    def __enter__(self) -> PostgreSQLReleaseTarget:
        return self.start()

    def __exit__(self, error_type: object, _value: object, _traceback: object) -> None:
        self.close(suppress_failure=error_type is not None)

    def start(self) -> PostgreSQLReleaseTarget:
        if self._container_id is not None:
            raise ReleaseVerificationError("PostgreSQL release fixture was already started.")
        self._run(("docker", "image", "inspect", "--format", "{{.Id}}", self._image))
        run_id = secrets.token_hex(16)
        username = f"aipcs_init_{run_id[:12]}"
        database = f"aipcs_{run_id[:12]}"
        password = secrets.token_urlsafe(32)
        output = self._run(
            (
                "docker",
                "run",
                "--detach",
                "--rm",
                "--pull=never",
                "--label",
                "com.aipcs.release.fixture=postgresql",
                "--label",
                f"com.aipcs.release.run_id={run_id}",
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
                self._image,
            )
        )
        container_id = output.strip()
        if _CONTAINER_ID.fullmatch(container_id) is None:
            raise ReleaseVerificationError("PostgreSQL release fixture returned an invalid container id.")
        self._container_id = container_id
        try:
            port = _release_loopback_port(self._run(("docker", "port", container_id, "5432/tcp")))
            initial = PostgreSQLReleaseTarget("127.0.0.1", port, database, username, password)
            self._wait_ready(initial)
            return self._provision_runtime_role(initial, run_id)
        except Exception:
            self.close(suppress_failure=True)
            raise

    def close(self, *, suppress_failure: bool = False) -> None:
        container_id, self._container_id = self._container_id, None
        if container_id is None:
            return
        try:
            self._run(("docker", "container", "rm", "--force", container_id))
        except ReleaseVerificationError:
            if not suppress_failure:
                raise

    def _run(self, command: Sequence[str], *, input_text: str | None = None) -> str:
        return self._runner(command, input_text=input_text)  # type: ignore[operator]

    def _wait_ready(self, target: PostgreSQLReleaseTarget) -> None:
        container_id = self._container_id
        if container_id is None:
            raise ReleaseVerificationError("PostgreSQL release fixture was not started.")
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
        for attempt in range(40):
            try:
                self._run(command)
                return
            except ReleaseVerificationError:
                if attempt == 39:
                    raise ReleaseVerificationError("PostgreSQL release fixture did not become ready.") from None
                time.sleep(0.25)

    def _provision_runtime_role(
        self, initial: PostgreSQLReleaseTarget, run_id: str
    ) -> PostgreSQLReleaseTarget:
        container_id = self._container_id
        if container_id is None:
            raise ReleaseVerificationError("PostgreSQL release fixture was not started.")
        username = f"aipcs_runtime_{run_id[:12]}"
        password = secrets.token_urlsafe(32)
        sql = (
            f"CREATE ROLE {username} LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOREPLICATION NOBYPASSRLS;\n"
            f"REVOKE ALL ON DATABASE {initial.database} FROM PUBLIC;\n"
            "REVOKE ALL ON SCHEMA public FROM PUBLIC;\n"
            f"GRANT CONNECT, CREATE ON DATABASE {initial.database} TO {username};\n"
        )
        self._run(
            (
                "docker",
                "exec",
                "--interactive",
                "--env",
                f"PGPASSWORD={initial.password}",
                container_id,
                "psql",
                "--no-psqlrc",
                "--set",
                "ON_ERROR_STOP=1",
                f"--username={initial.username}",
                f"--dbname={initial.database}",
            ),
            input_text=sql,
        )
        return PostgreSQLReleaseTarget(
            initial.host, initial.port, initial.database, username, password
        )


def scrubbed_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Remove interpreter/venv contamination while retaining normal OS settings."""

    environment = dict(os.environ if base is None else base)
    for key in _SCRUBBED_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    environment["UV_OFFLINE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def stage_environment(environment: Mapping[str, str], project_environment: Path) -> dict[str, str]:
    """Give a uv stage an external environment so the checkout remains untouched."""

    staged = dict(environment)
    staged["UV_PROJECT_ENVIRONMENT"] = str(project_environment)
    return staged


def _bounded(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return f"{text[:MAX_OUTPUT_CHARS]}\n[output truncated]"


def redact_text(text: str, roots: Iterable[Path]) -> str:
    """Bound diagnostics without retaining a checkout or temporary-workspace path."""

    redacted = text
    for root in sorted({str(path.resolve()) for path in roots}, key=len, reverse=True):
        redacted = redacted.replace(root, "<redacted-path>")
    return _bounded(redacted)


def run_stage(
    label: str,
    args: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    redaction_roots: Iterable[Path] = (),
) -> StageResult:
    """Run one bounded no-shell command and turn failures into a safe local error."""

    command = [os.fspath(argument) for argument in args]
    roots = (cwd, *redaction_roots)
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=dict(environment),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise ReleaseVerificationError(f"{label}: required executable is unavailable.") from error
    except subprocess.TimeoutExpired as error:
        output = "".join(part for part in (error.stdout, error.stderr) if isinstance(part, str))
        detail = redact_text(output, roots)
        suffix = f"\n{detail}" if detail else ""
        raise ReleaseVerificationError(
            f"{label}: timed out after {timeout} seconds.{suffix}"
        ) from None
    if completed.returncode:
        detail = redact_text(f"{completed.stdout}{completed.stderr}", roots)
        suffix = f"\n{detail}" if detail else ""
        raise ReleaseVerificationError(f"{label}: failed (exit {completed.returncode}).{suffix}")
    return StageResult(label)


def require_local_preconditions(root: Path, uv: str | None = None) -> str:
    """Fail before work if this is not a suitable locked, local release checkout."""

    if tuple(sys.version_info[:2]) < (3, 12):
        raise ReleaseVerificationError("Python 3.12 or newer is required.")
    for relative in ("pyproject.toml", "uv.lock", ".git", "scripts/check_wheel_contents.py"):
        if not (root / relative).exists():
            raise ReleaseVerificationError(
                "release verifier must run from a complete repository checkout."
            )
    resolved_uv = uv or shutil.which("uv")
    if resolved_uv is None:
        raise ReleaseVerificationError(
            "uv is required; install it and prime the locked offline cache."
        )
    return resolved_uv


def generated_checkout_artifacts(root: Path) -> tuple[Path, ...]:
    """Find denied private/generated material without entering .git or the development venv."""

    artifacts: list[Path] = []
    for current, directories, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        if current_path == root:
            directories[:] = [name for name in directories if name not in {".git", ".venv"}]
        for directory in tuple(directories):
            folded = directory.casefold()
            if (
                folded in _GENERATED_DIRECTORY_NAMES
                or folded in _PRIVATE_DIRECTORY_NAMES
                or folded.startswith(".data")
                or folded.endswith(".egg-info")
            ):
                artifacts.append(current_path / directory)
                directories.remove(directory)
        for name in names:
            path = current_path / name
            folded = name.casefold()
            relative = path.relative_to(root).as_posix()
            if (
                folded in _GENERATED_FILE_NAMES
                or folded in _PRIVATE_FILE_NAMES
                or folded.startswith(".data")
                or (folded.startswith(".env") and folded != ".env.example")
                or path.suffix.casefold() in _GENERATED_FILE_SUFFIXES
                or path.suffix.casefold() in _PRIVATE_FILE_SUFFIXES
                or folded.endswith(_GENERATED_FILE_ENDINGS)
                or _PRIVATE_TRANSCRIPT.search(relative)
                or "private-artifact" in folded
                or "preservation-manifest" in folded
                or name.endswith("~")
                or name.startswith(".#")
                or (len(name) > 2 and name.startswith("#") and name.endswith("#"))
            ):
                artifacts.append(path)
    return tuple(sorted(artifacts))


def require_clean_checkout_artifacts(root: Path) -> None:
    if generated_checkout_artifacts(root):
        raise ReleaseVerificationError(
            "source checkout contains generated, database, or private artifacts."
        )


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseVerificationError("git returned an unsafe path while staging the clean copy.")
    return path


def intended_source_paths(root: Path, *, environment: Mapping[str, str]) -> tuple[Path, ...]:
    """Return tracked and non-ignored untracked regular files, including dirty content."""

    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            env=dict(environment),
            check=True,
            capture_output=True,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ReleaseVerificationError(
            "could not determine the intended Git source file set."
        ) from error
    paths: list[Path] = []
    for raw in listed.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            relative = _safe_relative_path(raw.decode("utf-8", "strict"))
        except UnicodeDecodeError as error:
            raise ReleaseVerificationError(
                "git returned a non-UTF-8 path while staging the clean copy."
            ) from error
        candidate = root.joinpath(*relative.parts)
        _require_regular_file(candidate, root)
        paths.append(candidate)
    if not paths:
        raise ReleaseVerificationError("the intended Git source file set is unexpectedly empty.")
    return tuple(sorted(set(paths)))


def _require_regular_file(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
        root_mode = root.lstat().st_mode
        if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
            raise ReleaseVerificationError("clean-copy staging requires a regular directory root.")
        current = root
        for part in relative.parts[:-1]:
            current /= part
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ReleaseVerificationError(
                    "clean-copy staging rejects symbolic-link or non-directory ancestors."
                )
        mode = path.lstat().st_mode
    except (OSError, ValueError) as error:
        raise ReleaseVerificationError(
            "clean-copy staging found an unsafe source entry."
        ) from error
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ReleaseVerificationError("clean-copy staging accepts regular source files only.")


def regular_files_under(root: Path) -> tuple[Path, ...]:
    """Walk a directory without following, tolerating, or silently skipping links."""

    files: list[Path] = []
    for current, directories, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for directory in tuple(directories):
            candidate = current_path / directory
            if stat.S_ISLNK(candidate.lstat().st_mode):
                raise ReleaseVerificationError("clean-copy staging rejects symbolic links.")
        for name in names:
            candidate = current_path / name
            _require_regular_file(candidate, root)
            files.append(candidate)
    return tuple(sorted(files))


def _copy_one(source: Path, source_root: Path, destination_root: Path) -> Path:
    relative = source.relative_to(source_root)
    destination = destination_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination, follow_symlinks=False)
    _require_regular_file(destination, destination_root)
    return destination


def make_clean_copy(
    source_root: Path, workspace: Path, *, environment: Mapping[str, str]
) -> CleanCopy:
    """Copy only intended files plus a fully independent .git directory using copy2."""

    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = workspace / "clean-copy"
    destination.mkdir(mode=0o700)
    source_files = list(intended_source_paths(source_root, environment=environment))
    git_root = source_root / ".git"
    if not git_root.is_dir() or git_root.is_symlink():
        raise ReleaseVerificationError("clean-copy staging requires a real .git directory.")
    if (git_root / "objects" / "info" / "alternates").exists():
        raise ReleaseVerificationError(
            "clean-copy staging rejects Git alternates and shared object stores."
        )
    source_files.extend(regular_files_under(git_root))
    if len(set(source_files)) != len(source_files):
        raise ReleaseVerificationError("clean-copy staging found duplicate source entries.")
    copied = tuple(_copy_one(path, source_root, destination) for path in source_files)
    expected = {path.relative_to(source_root) for path in source_files}
    actual = {path.relative_to(destination) for path in regular_files_under(destination)}
    if actual != expected:
        raise ReleaseVerificationError("clean-copy staging produced an unexpected file set.")
    clean_copy = CleanCopy(destination, tuple(source_files), copied)
    assert_distinct_inodes(clean_copy)
    return clean_copy


def _inode_pairs(paths: Iterable[Path]) -> set[tuple[int, int]]:
    return {(path.stat().st_dev, path.stat().st_ino) for path in paths}


def assert_distinct_inodes(copy: CleanCopy) -> None:
    """Reject every source/copy hard link, including links under a different name."""

    if _inode_pairs(copy.source_files) & _inode_pairs(copy.copied_files):
        raise ReleaseVerificationError("clean-copy staging produced a hard-linked source file.")


def discover_artifacts(directory: Path) -> ArtifactSet:
    """Require exactly one wheel and one gzip source distribution and nothing else."""

    entries = tuple(sorted(directory.iterdir())) if directory.is_dir() else ()
    controls = tuple(entry for entry in entries if entry.name == ".gitignore")
    if any(not entry.is_file() or entry.is_symlink() for entry in controls):
        raise ReleaseVerificationError("build output contains an unsafe control-file entry.")
    artifacts = tuple(entry for entry in entries if entry.name != ".gitignore")
    wheels = [entry for entry in artifacts if entry.is_file() and entry.suffix == ".whl"]
    sdists = [entry for entry in artifacts if entry.is_file() and entry.name.endswith(".tar.gz")]
    if len(artifacts) != 2 or len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseVerificationError(
            "build must produce exactly one wheel and one .tar.gz source distribution."
        )
    return ArtifactSet(wheels[0], sdists[0])


def build_and_guard(
    root: Path,
    output_directory: Path,
    *,
    uv: str,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> ArtifactSet:
    """Build outside the checkout and apply the archive-content denial guard."""

    output_directory.mkdir(mode=0o700)
    run_stage(
        "build",
        [uv, "build", "--offline", "--out-dir", output_directory],
        cwd=root,
        environment=environment,
        redaction_roots=redaction_roots,
    )
    artifacts = discover_artifacts(output_directory)
    for artifact in (artifacts.wheel, artifacts.sdist):
        run_stage(
            "artifact content guard",
            [sys.executable, root / "scripts" / "check_wheel_contents.py", artifact],
            cwd=root,
            environment=environment,
            redaction_roots=redaction_roots,
        )
    return artifacts


def run_source_gates(
    root: Path,
    *,
    uv: str,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> tuple[StageResult, ...]:
    """Run direct source gates.  This function never invokes this verifier."""

    source_environment = dict(environment)
    source_environment["PYTHONPATH"] = str(root / "src")
    commands = (
        ("git diff --check", ["git", "diff", "--check"]),
        ("git diff --cached --check", ["git", "diff", "--cached", "--check"]),
        ("git fsck", ["git", "fsck", "--no-dangling"]),
        ("public hygiene", [sys.executable, "scripts/check_public_hygiene.py", "--history"]),
        (
            "source environment",
            [
                uv,
                "sync",
                "--locked",
                "--offline",
                "--extra",
                "dev",
                "--no-install-project",
            ],
        ),
        (
            "application boundaries",
            [
                uv,
                "run",
                "--no-sync",
                "python",
                "scripts/check_application_boundaries.py",
            ],
        ),
        (
            "pytest",
            [
                uv,
                "run",
                "--no-sync",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
            ],
        ),
        (
            "ruff",
            [
                uv,
                "run",
                "--no-sync",
                "ruff",
                "check",
                ".",
                "--no-cache",
            ],
        ),
    )
    return tuple(
        run_stage(
            label,
            command,
            cwd=root,
            environment=source_environment,
            redaction_roots=redaction_roots,
        )
        for label, command in commands
    )


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def install_distribution(
    artifact: Path,
    workspace: Path,
    name: str,
    *,
    uv: str,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> Path:
    """Create a fresh external venv and install a locked candidate offline."""

    venv = workspace / f"{name}-venv"
    empty_cwd = workspace / f"{name}-cwd"
    empty_cwd.mkdir(mode=0o700)
    run_stage(
        f"{name} venv",
        [uv, "venv", "--offline", "--python", sys.executable, venv],
        cwd=empty_cwd,
        environment=environment,
        redaction_roots=redaction_roots,
    )
    python = _venv_python(venv)
    run_stage(
        f"{name} install",
        [uv, "pip", "install", "--offline", "--python", python, artifact],
        cwd=empty_cwd,
        environment=environment,
        redaction_roots=redaction_roots,
    )
    return python


def require_postgresql_extra(artifacts: ArtifactSet) -> None:
    """Prove both built artifacts declare the optional Psycopg runtime dependency."""

    try:
        with ZipFile(artifacts.wheel) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise ValueError
            wheel_metadata = archive.read(metadata_names[0]).decode("utf-8")
        with open_tar(artifacts.sdist, "r:gz") as archive:
            members = [member for member in archive.getmembers() if member.name.endswith("/pyproject.toml")]
            if len(members) != 1:
                raise ValueError
            stream = archive.extractfile(members[0])
            if stream is None:
                raise ValueError
            sdist_project = stream.read().decode("utf-8")
    except Exception:
        raise ReleaseVerificationError("PostgreSQL optional-dependency artifact inspection failed.") from None
    if (
        "Provides-Extra: postgresql" not in wheel_metadata
        or "Requires-Dist: psycopg[binary]" not in wheel_metadata
        or "postgresql" not in wheel_metadata
        or "[project.optional-dependencies]" not in sdist_project
        or "postgresql" not in sdist_project
        or "psycopg[binary]" not in sdist_project
    ):
        raise ReleaseVerificationError("PostgreSQL optional dependency is missing from a release artifact.")


def install_postgresql_distribution(
    artifact: Path,
    workspace: Path,
    name: str,
    *,
    uv: str,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> Path:
    """Install one artifact plus its PostgreSQL extra into an independent venv."""

    venv = workspace / f"postgres-{name}-venv"
    empty_cwd = workspace / f"postgres-{name}-cwd"
    empty_cwd.mkdir(mode=0o700)
    run_stage(
        f"postgres {name} venv",
        [uv, "venv", "--offline", "--python", sys.executable, venv],
        cwd=empty_cwd,
        environment=environment,
        redaction_roots=redaction_roots,
    )
    python = _venv_python(venv)
    run_stage(
        f"postgres {name} install",
        [uv, "pip", "install", "--offline", "--python", python, f"{artifact}[postgresql]"],
        cwd=empty_cwd,
        environment=environment,
        redaction_roots=redaction_roots,
    )
    return python


def _postgresql_config_probe_program() -> str:
    """Return an installed-only offline configuration redaction assertion."""

    return r"""
import os
import subprocess
import sys

executable, principal = sys.argv[1:]
secret = os.environ["AIPCS_RELEASE_POSTGRES_DSN"]
base = [
    executable, "--profile", "postgresql", "--principal-id", principal,
    "--postgres-dsn-env", "AIPCS_RELEASE_POSTGRES_DSN",
]
for command in ("show", "validate"):
    completed = subprocess.run(
        [*base[:1], "config", command, *base[1:]],
        check=False, capture_output=True, text=True, env=os.environ.copy(), timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    assert secret not in completed.stdout
    assert secret not in completed.stderr
    assert "AIPCS_RELEASE_POSTGRES_DSN" not in completed.stdout
"""


def run_postgresql_config_redaction(
    python: Path,
    workspace: Path,
    executable: Path,
    principal: str,
    target: PostgreSQLReleaseTarget,
    *,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> None:
    """Check installed offline PostgreSQL config commands without a config file."""

    probe = workspace / "postgres-config-probe.py"
    probe.write_text(_postgresql_config_probe_program(), encoding="utf-8")
    child_environment = dict(environment)
    child_environment[POSTGRES_RELEASE_DSN_ENV] = target.dsn
    try:
        run_stage(
            "installed PostgreSQL config redaction",
            [python, "-I", probe, executable, principal],
            cwd=workspace,
            environment=child_environment,
            redaction_roots=redaction_roots,
        )
    except ReleaseVerificationError:
        raise ReleaseVerificationError("Installed PostgreSQL config redaction failed.") from None


def _postgresql_stdio_probe_program() -> str:
    """Return the installed real-stdio/restart/isolation capability proof."""

    return r"""
import json
import os
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

executable, principal, other_principal = sys.argv[1:]
expected = [
    "aipcs_server_info", "aipcs_service_seed", "aipcs_service_list",
    "aipcs_service_inspect", "aipcs_service_design", "aipcs_service_materialise",
    "aipcs_service_evolve", "aipcs_record_create", "aipcs_record_get",
    "aipcs_record_list", "aipcs_record_search", "aipcs_record_update",
    "aipcs_record_delete", "aipcs_record_history", "aipcs_bootstrap",
    "aipcs_service_summary", "aipcs_branch_create", "aipcs_branch_list",
    "aipcs_branch_update", "aipcs_branch_assign_records", "aipcs_maintenance_scan",
]
state = {}
managed = [
    {"name": "id", "type": "uuid", "required": True, "primary_key": True},
    {"name": "owner_id", "type": "string", "required": True},
    {"name": "created_at", "type": "datetime", "required": True},
    {"name": "updated_at", "type": "datetime", "required": True},
    {"name": "created_via", "type": "string", "required": True},
    {"name": "record_version", "type": "integer", "required": True},
]
manifest = {
    "manifest_version": 2, "schema_version": 1,
    "entities": [{"name": "note", "attributes": managed + [
        {"name": "title", "type": "string", "required": True},
        {"name": "category", "type": "string", "required": True},
        {"name": "tags", "type": "string_list", "retrieval_mode": "membership"},
        {"name": "status", "type": "string"},
    ]}],
    "relationships": [],
    "indices": [{"name": "note_owner_category_idx", "entity": "note", "fields": ["owner_id", "category"]}],
    "query_patterns": ["Find synthetic release notes."],
    "discovery_facets": [{"entity": "note", "field": "category"}, {"entity": "note", "field": "tags"}],
    "retrieval_guidance": "Use exact category and membership tag filters.", "migration_history": [],
}
evolved = json.loads(json.dumps(manifest))
evolved["schema_version"] = 2
evolved["migration_history"] = [{"from_schema_version": 1, "to_schema_version": 2, "operations": ["add optional summary"]}]
evolved["entities"][0]["attributes"].append({"name": "summary", "type": "string"})

def session_for(value):
    params = StdioServerParameters(
        command=executable,
        args=["serve", "--profile", "postgresql", "--principal-id", value,
              "--postgres-dsn-env", "AIPCS_RELEASE_POSTGRES_DSN"],
        env=os.environ.copy(),
    )
    return stdio_client(params)

async def check(value, *, mode):
    async with session_for(value) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert [tool.name for tool in tools.tools] == expected
            info = await session.call_tool("aipcs_server_info", {})
            result = info.structuredContent["result"]
            assert all(result["features"][name] for name in (
                "registry_lifecycle", "materialisation_lifecycle", "record_runtime", "discovery_topology"
            ))
            listed = await session.call_tool("aipcs_service_list", {})
            listed_value = listed.structuredContent
            if mode == "initial":
                assert listed_value["result"]["services"] == []
                bootstrap = await session.call_tool("aipcs_bootstrap", {})
                assert bootstrap.structuredContent["result"]["services"] == []
                seeded = await session.call_tool("aipcs_service_seed", {
                    "domain_name": "release_postgres", "domain_class": "release",
                    "intent_description": "installed PostgreSQL verifier", "idempotency_key": "release-seed",
                })
                assert seeded.structuredContent["ok"] is True
                service_id = seeded.structuredContent["result"]["service_id"]
                designed = await session.call_tool("aipcs_service_design", {
                    "service_id": service_id, "schema": manifest, "idempotency_key": "release-design",
                })
                assert designed.structuredContent["ok"] is True
                materialised = await session.call_tool("aipcs_service_materialise", {
                    "service_id": service_id,
                    "expected_service_revision": designed.structuredContent["result"]["service_revision"],
                    "expected_schema_version": 1, "idempotency_key": "release-materialise",
                })
                assert materialised.structuredContent["ok"] is True
                primary = await session.call_tool("aipcs_record_create", {
                    "service_id": service_id, "entity_name": "note",
                    "record": {"title": "Primary release note", "category": "release", "tags": ["alpha", "shared"], "status": "active"},
                    "idempotency_key": "release-record-primary",
                })
                assert primary.structuredContent["ok"] is True
                record = primary.structuredContent["result"]["record"]
                record_id = record["id"]
                loose = await session.call_tool("aipcs_record_create", {
                    "service_id": service_id, "entity_name": "note",
                    "record": {"title": "Loose release note", "category": "release", "tags": ["shared", "loose"], "status": "stale"},
                    "idempotency_key": "release-record-loose",
                })
                assert loose.structuredContent["ok"] is True
                loose_id = loose.structuredContent["result"]["record"]["id"]
                fetched = await session.call_tool("aipcs_record_get", {"service_id": service_id, "entity_name": "note", "record_id": record_id})
                assert fetched.structuredContent["result"]["id"] == record_id
                listed_records = await session.call_tool("aipcs_record_list", {"service_id": service_id, "entity_name": "note", "limit": 10})
                assert {item["id"] for item in listed_records.structuredContent["result"]["records"]} == {record_id, loose_id}
                searched = await session.call_tool("aipcs_record_search", {"service_id": service_id, "entity_name": "note", "filters": {"tags": "alpha"}})
                assert [item["id"] for item in searched.structuredContent["result"]["records"]] == [record_id]
                updated = await session.call_tool("aipcs_record_update", {
                    "service_id": service_id, "entity_name": "note", "record_id": record_id,
                    "updates": {"title": "Primary release note updated"}, "expected_record_version": record["record_version"],
                    "idempotency_key": "release-record-update",
                })
                assert updated.structuredContent["ok"] is True
                branch = await session.call_tool("aipcs_branch_create", {
                    "service_id": service_id, "slug": "release-root", "title": "Release root", "intent": "Bounded release retrieval.",
                    "branch_type": "release", "idempotency_key": "release-branch-create",
                })
                assert branch.structuredContent["ok"] is True
                branch_value = branch.structuredContent["result"]["branch"]
                branch_id = branch_value["id"]
                changed_branch = await session.call_tool("aipcs_branch_update", {
                    "service_id": service_id, "branch_id": branch_id, "updates": {"title": "Release root updated"},
                    "expected_branch_revision": branch_value["branch_revision"], "idempotency_key": "release-branch-update",
                })
                assert changed_branch.structuredContent["ok"] is True
                branches = await session.call_tool("aipcs_branch_list", {"service_id": service_id, "limit": 10})
                assert [item["id"] for item in branches.structuredContent["result"]["branches"]] == [branch_id]
                assigned = await session.call_tool("aipcs_branch_assign_records", {
                    "service_id": service_id, "branch_id": branch_id, "role": "primary", "operation": "assign",
                    "records": [{"entity_name": "note", "record_id": record_id,
                                 "expected_record_version": updated.structuredContent["result"]["record"]["record_version"]}],
                    "idempotency_key": "release-branch-assign",
                })
                assert assigned.structuredContent["ok"] is True
                history = await session.call_tool("aipcs_record_history", {"service_id": service_id, "entity_name": "note", "record_id": record_id})
                assert len(history.structuredContent["result"]["events"]) >= 3
                summary = await session.call_tool("aipcs_service_summary", {"service_id": service_id, "sample": 1})
                assert summary.structuredContent["result"]["data_status"] == "ready"
                assert summary.structuredContent["result"]["facets"] and summary.structuredContent["result"]["samples"]
                maintenance = await session.call_tool("aipcs_maintenance_scan", {
                    "service_id": service_id, "entity_name": "note", "scan_types": ["unbranched"], "limit": 10,
                })
                assert any(item["record"]["id"] == loose_id for item in maintenance.structuredContent["result"]["candidates"])
                evolved_result = await session.call_tool("aipcs_service_evolve", {
                    "service_id": service_id,
                    "expected_service_revision": materialised.structuredContent["result"]["service_revision"],
                    "expected_schema_version": 1, "schema": evolved, "idempotency_key": "release-evolve",
                })
                assert evolved_result.structuredContent["result"]["schema_version"] == 2
                state.update(service_id=service_id, record_id=record_id)
            elif mode == "restart":
                assert len(listed_value["result"]["services"]) == 1
                fetched = await session.call_tool("aipcs_record_get", {
                    "service_id": state["service_id"], "entity_name": "note", "record_id": state["record_id"],
                })
                assert fetched.structuredContent["result"]["fields"]["title"] == "Primary release note updated"
                summary = await session.call_tool("aipcs_service_summary", {"service_id": state["service_id"], "sample": 1})
                assert summary.structuredContent["result"]["data_status"] == "ready"
            else:
                assert listed_value["result"]["services"] == []
                bootstrap = await session.call_tool("aipcs_bootstrap", {})
                assert bootstrap.structuredContent["result"]["services"] == []

async def main():
    await check(principal, mode="initial")
    await check(principal, mode="restart")
    await check(other_principal, mode="isolation")

anyio.run(main)
"""


def run_postgresql_stdio_probe(
    python: Path,
    workspace: Path,
    executable: Path,
    principal: str,
    target: PostgreSQLReleaseTarget,
    *,
    name: str,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> None:
    """Run the real installed PostgreSQL stdio capability/restart/isolation proof."""

    probe = workspace / f"postgres-{name}-stdio-probe.py"
    probe.write_text(_postgresql_stdio_probe_program(), encoding="utf-8")
    child_environment = dict(environment)
    child_environment[POSTGRES_RELEASE_DSN_ENV] = target.dsn
    try:
        run_stage(
            f"installed PostgreSQL {name} stdio",
            [python, "-I", probe, executable, principal, f"{principal}-isolation"],
            cwd=workspace,
            environment=child_environment,
            timeout=POSTGRES_SMOKE_TIMEOUT_SECONDS,
            redaction_roots=redaction_roots,
        )
    except ReleaseVerificationError:
        raise ReleaseVerificationError("Installed PostgreSQL stdio verification failed.") from None


def _postgresql_admin_cli_smoke_program() -> str:
    """Return the installed PostgreSQL and cross-backend CLI workflow."""

    return r"""
import json
import os
import subprocess
import sys
from pathlib import Path

from aipcs_mcp.application.models import ApplicationContext, SeedCommand
from aipcs_mcp.application.portable import decode_bundle_content
from aipcs_mcp.application.portable_store import validate_portable_members
from aipcs_mcp.application.registry_authority import StorageBackend
from aipcs_mcp.application.services import ServiceApplication
from aipcs_mcp.portable_io import validate_and_spool_bundle
from aipcs_mcp.records import compile_record_specification
from aipcs_mcp.runtime import SystemUtcClock, Uuid4ServiceIds
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter
from aipcs_mcp.storage.sqlite.portable import SQLitePortableServiceStore

executable = Path(sys.argv[1])
principal = sys.argv[2]
workspace = Path(sys.argv[3])
secret = os.environ["AIPCS_RELEASE_POSTGRES_DSN"]
sqlite_principal = f"{principal}-sqlite"
roundtrip_principal = f"{principal}-roundtrip"
sqlite_root = workspace / "sqlite-destination"
postgres_bundle = workspace / "postgres-export.jsonl"
archive_bundle = workspace / "postgres-archive.jsonl"
sqlite_bundle = workspace / "sqlite-export.jsonl"


def invoke(arguments, expected=0, retry_uncertain=False):
    for attempt in range(2 if retry_uncertain else 1):
        completed = subprocess.run(
            [str(executable), *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=dict(os.environ),
            timeout=45,
        )
        assert secret not in completed.stdout
        assert secret not in completed.stderr
        if completed.returncode == expected:
            stream = completed.stdout if expected == 0 else completed.stderr
            return json.loads(stream)
        try:
            failure = json.loads(completed.stderr)
            code = failure["error"]["code"]
        except Exception:
            code = "unparseable"
        if retry_uncertain and attempt == 0 and code == "operation_uncertain":
            print("POSTGRES_ADMIN_RETRY_OPERATION_UNCERTAIN", flush=True)
            continue
        safe_code = "".join(character for character in code.upper() if character.isalpha() or character == "_")
        print(f"POSTGRES_ADMIN_ERROR_{safe_code or 'UNKNOWN'}", flush=True)
        assert completed.returncode == expected, (
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )
    raise AssertionError("The retryable operation did not converge.")


def postgresql_config(value):
    return [
        "--profile", "postgresql",
        "--principal-id", value,
        "--postgres-dsn-env", "AIPCS_RELEASE_POSTGRES_DSN",
    ]


def sqlite_config():
    return [
        "--profile", "sqlite",
        "--principal-id", sqlite_principal,
        "--sqlite-data-root", str(sqlite_root),
    ]


source_config = postgresql_config(principal)
print("POSTGRES_ADMIN_START", flush=True)
assert invoke(["status", *source_config])["result"]["overall"] == "ready"
print("POSTGRES_ADMIN_STATUS", flush=True)
listed = invoke(["service", "list", *source_config])["result"]["services"]
assert len(listed) == 1
service = listed[0]
service_id = service["service_id"]
revision = service["service_revision"]
assert service["operational_status"] == "active"
print("POSTGRES_ADMIN_LIST", flush=True)
assert invoke(["service", "inspect", service_id, *source_config])["result"]["service_id"] == service_id
print("POSTGRES_ADMIN_INSPECT", flush=True)
assert invoke(["storage", "status", "--service", service_id, *source_config])["result"]["status"] == "ready"
print("POSTGRES_ADMIN_STORAGE", flush=True)
assert invoke(["doctor", "--service", service_id, *source_config])["result"]["overall"] in {
    "pass",
    "warning",
}
print("POSTGRES_ADMIN_DOCTOR", flush=True)
assert "candidates" in invoke(
    ["maintenance", "scan", "--service", service_id, *source_config]
)["result"]
print("POSTGRES_ADMIN_READ", flush=True)

suspended = invoke([
    "service", "suspend", service_id,
    "--expected-revision", str(revision),
    "--operation-id", "81111111-1111-4111-8111-111111111111",
    *source_config,
])
suspended_revision = suspended["result"]["service"]["service_revision"]
assert suspended["result"]["service"]["operational_status"] == "suspended"
print("POSTGRES_ADMIN_SUSPENDED", flush=True)

exported = invoke([
    "export", service_id,
    "--output", str(postgres_bundle),
    "--expected-revision", str(suspended_revision),
    "--operation-id", "82222222-2222-4222-8222-222222222222",
    *source_config,
])
assert exported["result"]["summary"]["source_backend"] == "postgresql"
assert postgres_bundle.is_file()
assert postgres_bundle.stat().st_mode & 0o777 == 0o600
print("POSTGRES_ADMIN_POSTGRES_EXPORTED", flush=True)

diagnostic_root = workspace / "sqlite-stage-diagnostic"
with postgres_bundle.open("rb") as diagnostic_source:
    with validate_and_spool_bundle(diagnostic_source) as diagnostic_bundle:
        diagnostic_content = decode_bundle_content(
            diagnostic_bundle.iter_lines(),
            principal_id=sqlite_principal,
            destination_backend=StorageBackend.SQLITE,
        )
checked_members = validate_portable_members(
    diagnostic_content.members,
    compile_record_specification(diagnostic_content.service.manifest),
)
print("POSTGRES_ADMIN_MEMBERS_VALID", flush=True)
diagnostic_store = SQLitePortableServiceStore(
    SQLiteLocationPolicy.from_resolved(diagnostic_root, "cli")
)
diagnostic_stage = diagnostic_store.stage(
    diagnostic_content.service,
    checked_members,
    diagnostic_content.export_fence,
)
assert diagnostic_stage.status == "staged"
assert diagnostic_store.observe(
    diagnostic_content.service,
    diagnostic_content.export_fence,
    checked_members,
)
print("POSTGRES_ADMIN_SQLITE_STAGE_VALID", flush=True)

dry = invoke([
    "import", "--input", str(postgres_bundle), "--dry-run", *sqlite_config()
])
assert dry["result"]["service"]["service_id"] == service_id
assert not sqlite_root.exists()
print("POSTGRES_ADMIN_SQLITE_DRY_RUN", flush=True)
sqlite_registry = SQLiteRegistryAdapter(
    SQLiteLocationPolicy.from_resolved(sqlite_root, "cli")
)
assert sqlite_registry.migrate().status == "ready"
print("POSTGRES_ADMIN_SQLITE_READY", flush=True)
imported = invoke([
    "import", "--input", str(postgres_bundle),
    "--operation-id", "83333333-3333-4333-8333-333333333333",
    "--yes",
    *sqlite_config(),
], retry_uncertain=True)
assert imported["result"]["service"]["service_id"] == service_id
assert imported["result"]["service"]["operational_status"] == "suspended"
print("POSTGRES_ADMIN_POSTGRES_TO_SQLITE", flush=True)
assert invoke(["status", *source_config])["result"]["registry"]["status"] == "ready"
print("POSTGRES_ADMIN_SOURCE_STILL_READY", flush=True)

archived = invoke([
    "service", "archive", service_id,
    "--expected-revision", str(suspended_revision),
    "--operation-id", "84444444-4444-4444-8444-444444444444",
    "--yes",
    *source_config,
])
archived_revision = archived["result"]["service"]["service_revision"]
assert archived["result"]["service"]["operational_status"] == "archived"
print("POSTGRES_ADMIN_ARCHIVED", flush=True)
archived_export = invoke([
    "export", service_id,
    "--output", str(archive_bundle),
    "--expected-revision", str(archived_revision),
    "--operation-id", "85555555-5555-4555-8555-555555555555",
    *source_config,
])
receipt_id = archived_export["result"]["receipt"]["receipt_id"]
print("POSTGRES_ADMIN_ARCHIVE_EXPORTED", flush=True)
assert invoke(["status", *source_config])["result"]["registry"]["status"] == "ready"
print("POSTGRES_ADMIN_PRE_PURGE_READY", flush=True)
purge_command = [
    "service", "purge", service_id,
    "--expected-revision", str(archived_revision),
    "--operation-id", "86666666-6666-4666-8666-666666666666",
    "--receipt", receipt_id,
    "--confirm-service-id", service_id,
    "--yes",
    *source_config,
]
purged = invoke(purge_command)
assert purged["result"]["tombstone"]["service_id"] == service_id
print("POSTGRES_ADMIN_PURGED_ONCE", flush=True)
assert invoke(purge_command)["result"] == purged["result"]
print("POSTGRES_ADMIN_PURGED", flush=True)

sqlite_service = ServiceApplication(
    sqlite_registry.open_uow,
    SystemUtcClock(),
    Uuid4ServiceIds(),
).seed(
    ApplicationContext(sqlite_principal, "cli"),
    SeedCommand(
        "sqlite_roundtrip",
        "release",
        "Installed SQLite to PostgreSQL CLI proof.",
        "sqlite-roundtrip-seed",
    ),
)
sqlite_service_id = str(sqlite_service.service_id)
sqlite_suspended = invoke([
    "service", "suspend", sqlite_service_id,
    "--expected-revision", "1",
    "--operation-id", "87000000-0000-4000-8000-000000000001",
    *sqlite_config(),
])
assert sqlite_suspended["result"]["service"]["operational_status"] == "suspended"
sqlite_export = invoke([
    "export", sqlite_service_id,
    "--output", str(sqlite_bundle),
    "--expected-revision", "2",
    "--operation-id", "87777777-7777-4777-8777-777777777777",
    *sqlite_config(),
])
assert sqlite_export["result"]["summary"]["source_backend"] == "sqlite"
print("POSTGRES_ADMIN_SQLITE_EXPORTED", flush=True)
roundtrip = invoke([
    "import", "--input", str(sqlite_bundle),
    "--operation-id", "88888888-8888-4888-8888-888888888888",
    "--yes",
    *postgresql_config(roundtrip_principal),
], retry_uncertain=True)
assert roundtrip["result"]["service"]["service_id"] == sqlite_service_id
assert roundtrip["result"]["service"]["operational_status"] == "suspended"
assert len(
    invoke(["service", "list", *postgresql_config(roundtrip_principal)])["result"]["services"]
) == 1

print("POSTGRES_ADMIN_SQLITE_TO_POSTGRES", flush=True)
print("POSTGRES_ADMIN_CLI_OK", flush=True)
"""


def run_postgresql_admin_cli_smoke(
    python: Path,
    workspace: Path,
    executable: Path,
    principal: str,
    target: PostgreSQLReleaseTarget,
    *,
    name: str,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> None:
    """Exercise installed V1-11 CLI and both transfer directions on PostgreSQL."""

    probe = workspace / f"postgres-{name}-admin-cli-smoke.py"
    probe.write_text(_postgresql_admin_cli_smoke_program(), encoding="utf-8")
    root = workspace / f"postgres-{name}-admin-cli"
    root.mkdir(mode=0o700)
    child_environment = dict(environment)
    child_environment[POSTGRES_RELEASE_DSN_ENV] = target.dsn
    try:
        run_stage(
            f"installed PostgreSQL {name} administration CLI",
            [python, "-I", probe, executable, principal, root],
            cwd=root,
            environment=child_environment,
            timeout=POSTGRES_SMOKE_TIMEOUT_SECONDS,
            redaction_roots=redaction_roots,
        )
    except ReleaseVerificationError as error:
        checkpoints = re.findall(r"POSTGRES_ADMIN_[A-Z_]+", str(error))
        phases = [
            value
            for value in checkpoints
            if not value.startswith(
                ("POSTGRES_ADMIN_ERROR_", "POSTGRES_ADMIN_RETRY_")
            )
        ]
        errors = [
            value.removeprefix("POSTGRES_ADMIN_ERROR_")
            for value in checkpoints
            if value.startswith("POSTGRES_ADMIN_ERROR_")
        ]
        checkpoint = phases[-1] if phases else "POSTGRES_ADMIN_START"
        suffix = f" with {errors[-1]}" if errors else ""
        raise ReleaseVerificationError(
            "Installed PostgreSQL administration CLI verification failed "
            f"after {checkpoint}{suffix}."
        ) from None


def _origin_probe(source_root: Path, copy_root: Path) -> str:
    return (
        "import pathlib,site,sys,aipcs_mcp;"
        "origin=pathlib.Path(aipcs_mcp.__file__).resolve();"
        "sites=[pathlib.Path(p).resolve() for p in site.getsitepackages()];"
        "blocked=[pathlib.Path(p).resolve() for p in "
        f"{json.dumps([str(source_root), str(copy_root)])}];"
        "assert any(origin.is_relative_to(p) for p in sites), origin;"
        "assert not any(origin.is_relative_to(p) for p in blocked), origin;"
        "assert not any(pathlib.Path(p or '.').resolve().is_relative_to(b) "
        "for p in sys.path for b in blocked), sys.path"
    )


def origin_is_isolated(
    origin: Path,
    site_packages: Iterable[Path],
    blocked_roots: Iterable[Path],
    sys_path: Iterable[str],
) -> bool:
    """Pure counterpart of the installed-interpreter origin probe for fast tests."""

    resolved_origin = origin.resolve()
    allowed = tuple(path.resolve() for path in site_packages)
    blocked = tuple(path.resolve() for path in blocked_roots)
    return (
        any(resolved_origin.is_relative_to(path) for path in allowed)
        and not any(resolved_origin.is_relative_to(path) for path in blocked)
        and not any(
            Path(entry or ".").resolve().is_relative_to(root)
            for entry in sys_path
            for root in blocked
        )
    )


def prove_site_packages_import(
    python: Path,
    workspace: Path,
    source_root: Path,
    copy_root: Path,
    *,
    name: str,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> None:
    """Prove import origin is installed site-packages, not either checkout."""

    empty_cwd = workspace / f"{name}-origin-probe"
    empty_cwd.mkdir(mode=0o700)
    run_stage(
        "installed import origin",
        [python, "-I", "-c", _origin_probe(source_root, copy_root)],
        cwd=empty_cwd,
        environment=environment,
        redaction_roots=redaction_roots,
    )


def _smoke_client_program() -> str:
    """Standalone installed public 1.2 lifecycle, data, restart, and isolation proof."""

    return r"""
import json
import sqlite3
import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

exe, root_text, principal, mode = sys.argv[1:]
root = Path(root_text)
state_path = root.parent / f"{root.name}-release-data-state.json"
managed = [
    {"name": "id", "type": "uuid", "required": True, "primary_key": True},
    {"name": "owner_id", "type": "string", "required": True},
    {"name": "created_at", "type": "datetime", "required": True},
    {"name": "updated_at", "type": "datetime", "required": True},
    {"name": "created_via", "type": "string", "required": True},
    {"name": "record_version", "type": "integer", "required": True},
]
manifest = {
    "manifest_version": 2,
    "schema_version": 1,
    "entities": [{
        "name": "note",
        "attributes": managed + [
            {"name": "title", "type": "string", "required": True},
            {"name": "category", "type": "string", "required": True},
            {"name": "tags", "type": "string_list", "retrieval_mode": "membership"},
            {"name": "status", "type": "string"},
            {"name": "confidence", "type": "number"},
        ],
    }],
    "relationships": [],
    "indices": [{
        "name": "note_owner_category_idx",
        "entity": "note",
        "fields": ["owner_id", "category"],
    }],
    "query_patterns": ["Find release notes by category or tag."],
    "discovery_facets": [
        {"entity": "note", "field": "category"},
        {"entity": "note", "field": "tags"},
        {"entity": "note", "field": "status"},
    ],
    "retrieval_guidance": "Use exact category filters and membership tag filters.",
    "migration_history": [],
}
evolved_manifest = json.loads(json.dumps(manifest))
evolved_manifest["schema_version"] = 2
evolved_manifest["migration_history"] = [{
    "from_schema_version": 1,
    "to_schema_version": 2,
    "operations": ["add optional note summary"],
}]
evolved_manifest["entities"][0]["attributes"].append(
    {"name": "summary", "type": "string"}
)
metadata_fields = {
    "service_id", "domain_name", "domain_class", "intent_description",
    "design_state", "operational_status", "schema", "schema_version",
    "service_revision", "recovery_state", "created_at", "updated_at",
    "last_activity_at", "materialised_at", "storage",
}
tool_names = [
    "aipcs_server_info",
    "aipcs_service_seed",
    "aipcs_service_list",
    "aipcs_service_inspect",
    "aipcs_service_design",
    "aipcs_service_materialise",
    "aipcs_service_evolve",
    "aipcs_record_create",
    "aipcs_record_get",
    "aipcs_record_list",
    "aipcs_record_search",
    "aipcs_record_update",
    "aipcs_record_delete",
    "aipcs_record_history",
    "aipcs_bootstrap",
    "aipcs_service_summary",
    "aipcs_branch_create",
    "aipcs_branch_list",
    "aipcs_branch_update",
    "aipcs_branch_assign_records",
    "aipcs_maintenance_scan",
]
secrets = (
    principal,
    "release-seed",
    "release-design",
    "release-materialise",
    "release-evolve",
    "release-record-create",
    "release-record-update",
    "release-loose-create",
    "release-root-create",
    "release-child-create",
    "release-root-update",
    "release-primary-assign",
    "release-primary-move",
    "release-related-assign",
    "release-related-unassign",
    "release-recovery-seed",
    "release-recovery-design",
    "release-recovery-materialise",
    str(root.resolve()),
    "registry.sqlite",
    "service-stores",
)


def assert_redacted(value):
    if isinstance(value, dict):
        for nested in value.values():
            assert_redacted(nested)
        return
    if isinstance(value, list):
        for nested in value:
            assert_redacted(nested)
        return
    if isinstance(value, str):
        for secret in secrets:
            assert secret not in value


async def call(session, name, arguments):
    with anyio.fail_after(10):
        result = await session.call_tool(name, arguments)
    assert result.structuredContent is not None
    payload = result.structuredContent
    assert json.loads(result.content[0].text) == payload
    assert_redacted(payload)
    return payload


def assert_failure(value, code, retryable=False):
    assert value["ok"] is False
    assert value["result"] is None
    assert value["error"]["code"] == code
    assert value["error"]["retryable"] is retryable


def assert_metadata(value, *, state, schema_version, revision, storage=False, recovery_state="clear"):
    assert set(value) == metadata_fields
    assert value["design_state"] == state
    assert value["operational_status"] == "active"
    assert value["service_revision"] == revision
    assert value["recovery_state"] == recovery_state
    if schema_version is None:
        assert value["schema"] is None and value["schema_version"] is None
    else:
        assert value["schema"]["manifest_version"] == 2
        assert value["schema"]["schema_version"] == schema_version
        assert value["schema"]["entities"][0]["name"] == "note"
        assert value["schema_version"] == schema_version
    if storage:
        assert isinstance(value["materialised_at"], str)
        assert value["storage"] == {
            "backend": "sqlite",
            "namespace": f"svc_{value['service_id'].replace('-', '')}",
        }
    else:
        assert value["materialised_at"] is None and value["storage"] is None


def record_arguments(service_id, record, key):
    return {
        "service_id": service_id,
        "entity_name": "note",
        "record": record,
        "idempotency_key": key,
    }


def downgrade_exact_r3_to_r2(database):
    with sqlite3.connect(database) as connection:
        for table in (
            "__aipcs_record_branch",
            "__aipcs_memory_branch",
            "__aipcs_record_history",
            "__aipcs_record_mutation",
        ):
            connection.execute(f'DROP TABLE "{table}"')
        connection.execute(
            'DELETE FROM "__aipcs_service_store_migration" WHERE "revision"=3'
        )
        connection.execute(
            'UPDATE "__aipcs_service_store_meta" '
            'SET "applied_revision"=2,"dirty"=0 WHERE "singleton"=1'
        )
        connection.commit()


def r3_objects(database):
    with sqlite3.connect(database) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE "
                "name IN ('__aipcs_record_mutation','__aipcs_record_history',"
                "'__aipcs_memory_branch','__aipcs_record_branch')"
            )
        }


async def prove_data_runtime(session, service_id, database, restarting):
    primary_document = {
        "title": "Primary release note",
        "category": "release",
        "tags": ["alpha", "shared"],
        "status": "active",
        "confidence": 0.9,
    }
    primary_create = record_arguments(
        service_id, primary_document, "release-record-create"
    )
    if not restarting:
        downgrade_exact_r3_to_r2(database)
        assert r3_objects(database) == set()
        before = database.read_bytes()
        migration_summary = await call(
            session, "aipcs_service_summary", {"service_id": service_id, "sample": 1}
        )
        assert migration_summary["ok"] is True
        assert migration_summary["result"]["data_status"] == "migration_required"
        assert_failure(
            await call(
                session,
                "aipcs_record_list",
                {"service_id": service_id, "entity_name": "note"},
            ),
            "storage_migration_required",
        )
        assert database.read_bytes() == before
        assert r3_objects(database) == set()

    created = await call(session, "aipcs_record_create", primary_create)
    assert created["ok"] is True
    assert created["result"]["replayed"] is restarting
    created_record = created["result"]["record"]
    assert created_record["record_version"] == 1
    assert created_record["created_via"] == "mcp"
    assert created_record["fields"]["title"] == primary_document["title"]
    record_id = created_record["id"]
    if not restarting:
        assert r3_objects(database) == {
            "__aipcs_record_mutation",
            "__aipcs_record_history",
            "__aipcs_memory_branch",
            "__aipcs_record_branch",
        }
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                'SELECT "applied_revision","dirty" '
                'FROM "__aipcs_service_store_meta"'
            ).fetchone() == (3, 0)

    replay = await call(session, "aipcs_record_create", primary_create)
    assert replay["ok"] is True and replay["result"]["replayed"] is True
    assert replay["result"]["record"] == created_record
    changed = record_arguments(
        service_id,
        {**primary_document, "title": "Changed request"},
        "release-record-create",
    )
    assert_failure(
        await call(session, "aipcs_record_create", changed),
        "changed_fingerprint",
    )

    loose_create = record_arguments(
        service_id,
        {
            "title": "Loose release note",
            "category": "release",
            "tags": ["shared", "loose"],
            "status": "stale",
            "confidence": 0.2,
        },
        "release-loose-create",
    )
    loose = await call(session, "aipcs_record_create", loose_create)
    assert loose["ok"] is True
    assert loose["result"]["replayed"] is restarting
    loose_id = loose["result"]["record"]["id"]

    fetched = await call(
        session,
        "aipcs_record_get",
        {"service_id": service_id, "entity_name": "note", "record_id": record_id},
    )
    assert fetched["ok"] is True
    assert fetched["result"]["id"] == record_id
    listed = await call(
        session,
        "aipcs_record_list",
        {"service_id": service_id, "entity_name": "note", "limit": 10},
    )
    assert listed["ok"] is True
    assert {record["id"] for record in listed["result"]["records"]} == {
        record_id,
        loose_id,
    }
    exact = await call(
        session,
        "aipcs_record_search",
        {
            "service_id": service_id,
            "entity_name": "note",
            "filters": {"category": "release"},
        },
    )
    assert exact["ok"] is True and len(exact["result"]["records"]) == 2
    membership = await call(
        session,
        "aipcs_record_search",
        {
            "service_id": service_id,
            "entity_name": "note",
            "filters": {"tags": "alpha"},
        },
    )
    assert membership["ok"] is True
    assert [record["id"] for record in membership["result"]["records"]] == [record_id]

    update = {
        "service_id": service_id,
        "entity_name": "note",
        "record_id": record_id,
        "updates": {"title": "Primary release note updated"},
        "expected_record_version": 1,
        "idempotency_key": "release-record-update",
    }
    updated = await call(session, "aipcs_record_update", update)
    assert updated["ok"] is True
    assert updated["result"]["replayed"] is restarting
    assert updated["result"]["record"]["record_version"] == 2
    assert_failure(
        await call(
            session,
            "aipcs_record_update",
            {**update, "idempotency_key": "release-record-stale"},
        ),
        "stale_record_version",
    )

    root_create = {
        "service_id": service_id,
        "slug": "release-root",
        "title": "Release root",
        "intent": "Primary release retrieval.",
        "branch_type": "release",
        "idempotency_key": "release-root-create",
    }
    root_branch = await call(session, "aipcs_branch_create", root_create)
    assert root_branch["ok"] is True
    assert root_branch["result"]["replayed"] is restarting
    root_id = root_branch["result"]["branch"]["id"]
    root_replay = await call(session, "aipcs_branch_create", root_create)
    assert root_replay["ok"] is True and root_replay["result"]["replayed"] is True
    child_create = {
        "service_id": service_id,
        "slug": "release-child",
        "title": "Release child",
        "intent": "Focused release retrieval.",
        "parent_branch_id": root_id,
        "idempotency_key": "release-child-create",
    }
    child_branch = await call(session, "aipcs_branch_create", child_create)
    assert child_branch["ok"] is True
    assert child_branch["result"]["replayed"] is restarting
    child_id = child_branch["result"]["branch"]["id"]
    root_update = {
        "service_id": service_id,
        "branch_id": root_id,
        "updates": {"title": "Release root updated"},
        "expected_branch_revision": 1,
        "idempotency_key": "release-root-update",
    }
    root_updated = await call(session, "aipcs_branch_update", root_update)
    assert root_updated["ok"] is True
    assert root_updated["result"]["replayed"] is restarting
    assert root_updated["result"]["branch"]["branch_revision"] == 2
    branches = await call(
        session, "aipcs_branch_list", {"service_id": service_id, "limit": 10}
    )
    assert branches["ok"] is True
    assert {branch["slug"] for branch in branches["result"]["branches"]} == {
        "release-root",
        "release-child",
    }

    async def assign(branch_id, role, operation, version, key):
        value = await call(
            session,
            "aipcs_branch_assign_records",
            {
                "service_id": service_id,
                "branch_id": branch_id,
                "role": role,
                "operation": operation,
                "records": [{
                    "entity_name": "note",
                    "record_id": record_id,
                    "expected_record_version": version,
                }],
                "idempotency_key": key,
            },
        )
        assert value["ok"] is True
        assert value["result"]["replayed"] is restarting
        return value["result"]["records"][0]["record_version"]

    assert await assign(
        root_id, "primary", "assign", 2, "release-primary-assign"
    ) == 3
    assert await assign(
        child_id, "primary", "assign", 3, "release-primary-move"
    ) == 4
    assert await assign(
        root_id, "related", "assign", 4, "release-related-assign"
    ) == 5
    assert await assign(
        root_id, "related", "unassign", 5, "release-related-unassign"
    ) == 6

    history = await call(
        session,
        "aipcs_record_history",
        {"service_id": service_id, "entity_name": "note", "record_id": record_id},
    )
    assert history["ok"] is True
    assert [
        (event["record_version"], event["kind"])
        for event in history["result"]["events"]
    ] == [
        (1, "create"),
        (2, "update"),
        (3, "primary_assign"),
        (4, "primary_move"),
        (5, "related_assign"),
        (6, "related_unassign"),
    ]
    current = await call(
        session,
        "aipcs_record_get",
        {"service_id": service_id, "entity_name": "note", "record_id": record_id},
    )
    assert current["ok"] is True
    assert current["result"]["record_version"] == 6
    assert current["result"]["fields"]["title"] == "Primary release note updated"

    summary = await call(
        session, "aipcs_service_summary", {"service_id": service_id, "sample": 1}
    )
    assert summary["ok"] is True
    result = summary["result"]
    assert result["data_status"] == "ready"
    assert result["record_counts"] == [{"entity_name": "note", "count": 2}]
    assert result["unbranched_record_count"] == 1
    facets = {(item["entity_name"], item["field_name"]): item["values"] for item in result["facets"]}
    assert facets[("note", "category")] == [{"value": "release", "count": 2}]
    assert {item["value"] for item in facets[("note", "tags")]} == {
        "alpha", "shared", "loose"
    }
    cards = {card["branch"]["slug"]: card for card in result["branches"]}
    assert cards["release-child"]["primary_record_count"] == 1
    assert cards["release-root"]["primary_record_count"] == 0
    assert cards["release-root"]["related_record_count"] == 0
    assert result["samples"][0]["entity_name"] == "note"
    assert len(result["samples"][0]["records"]) == 1

    maintenance = await call(
        session,
        "aipcs_maintenance_scan",
        {
            "service_id": service_id,
            "entity_name": "note",
            "scan_types": ["unbranched"],
            "limit": 10,
        },
    )
    assert maintenance["ok"] is True
    assert [
        candidate["record"]["id"]
        for candidate in maintenance["result"]["candidates"]
        if candidate["scan_type"] == "unbranched"
    ] == [loose_id]
    return {
        "service_id": service_id,
        "record_id": record_id,
        "loose_id": loose_id,
        "root_id": root_id,
        "child_id": child_id,
    }


async def main():
    args = (
        ["serve"]
        if mode == "stateless"
        else [
            "serve",
            "--profile",
            "sqlite",
            "--sqlite-data-root",
            str(root),
            "--principal-id",
            principal,
        ]
    )
    params = StdioServerParameters(
        command=exe,
        args=args,
        cwd=str(root.parent),
        env={"PYTHONNOUSERSITE": "1"},
    )
    with anyio.fail_after(55):
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            with anyio.fail_after(10):
                await session.initialize()
            with anyio.fail_after(10):
                tools = await session.list_tools()
            names = [tool.name for tool in tools.tools]
            if mode == "stateless":
                assert names == ["aipcs_server_info"]
                info = await call(session, "aipcs_server_info", {})
                assert info["ok"] is True
                assert info["result"]["aipcs_mcp_contract"] == "1.2.0"
                assert info["result"]["features"]["registry_lifecycle"] is False
                assert info["result"]["features"]["materialisation_lifecycle"] is False
                assert info["result"]["features"]["record_runtime"] is False
                assert info["result"]["features"]["discovery_topology"] is False
                return
            assert names == tool_names and len(names) == 21
            info = await call(session, "aipcs_server_info", {})
            assert info["ok"] is True
            assert info["result"]["aipcs_mcp_contract"] == "1.2.0"
            assert info["result"]["features"]["registry_lifecycle"] is True
            assert info["result"]["features"]["materialisation_lifecycle"] is True
            assert info["result"]["features"]["record_runtime"] is True
            assert info["result"]["features"]["discovery_topology"] is True
            listed = await call(session, "aipcs_service_list", {})

            if mode == "principal_b":
                assert listed["ok"] is True and listed["result"]["services"] == []
                bootstrap = await call(session, "aipcs_bootstrap", {})
                assert bootstrap["ok"] is True and bootstrap["result"]["services"] == []
                state = json.loads(state_path.read_text(encoding="utf-8"))
                assert_failure(
                    await call(
                        session,
                        "aipcs_record_get",
                        {
                            "service_id": state["service_id"],
                            "entity_name": "note",
                            "record_id": state["record_id"],
                        },
                    ),
                    "not_found",
                )
                assert_failure(
                    await call(
                        session,
                        "aipcs_service_summary",
                        {"service_id": state["service_id"], "sample": 1},
                    ),
                    "not_found",
                )
                assert_failure(
                    await call(
                        session,
                        "aipcs_service_inspect",
                        {"service_id": state["service_id"]},
                    ),
                    "not_found",
                )
                seed = {
                    "domain_name": "release_smoke",
                    "domain_class": "release",
                    "intent_description": "isolated installed distribution smoke",
                    "idempotency_key": "release-principal-b-seed",
                }
                created = await call(session, "aipcs_service_seed", seed)
                assert created["ok"] is True
                assert_metadata(
                    created["result"],
                    state="seeded",
                    schema_version=None,
                    revision=1,
                )
                return

            if mode == "recovery":
                recovery_seed = {
                    "domain_name": "release_recovery",
                    "domain_class": "release",
                    "intent_description": "installed public recovery smoke",
                    "idempotency_key": "release-recovery-seed",
                }
                created = await call(session, "aipcs_service_seed", recovery_seed)
                assert created["ok"] is True
                service_id = created["result"]["service_id"]
                designed = await call(
                    session,
                    "aipcs_service_design",
                    {
                        "service_id": service_id,
                        "schema": manifest,
                        "idempotency_key": "release-recovery-design",
                    },
                )
                assert designed["ok"] is True
                store_root = root / "service-stores"
                store_root.mkdir(mode=0o700, exist_ok=True)
                store_root.chmod(0o700)
                database = store_root / f"svc_{service_id.replace('-', '')}.sqlite"
                with sqlite3.connect(database) as connection:
                    connection.execute(
                        'CREATE TABLE "unexpected" ("id" INTEGER PRIMARY KEY)'
                    )
                database.chmod(0o600)
                materialise = {
                    "service_id": service_id,
                    "expected_service_revision": 2,
                    "expected_schema_version": 1,
                    "idempotency_key": "release-recovery-materialise",
                }
                recovery = await call(
                    session, "aipcs_service_materialise", materialise
                )
                assert_failure(recovery, "recovery_required")
                assert await call(
                    session, "aipcs_service_materialise", materialise
                ) == recovery
                inspected = await call(
                    session, "aipcs_service_inspect", {"service_id": service_id}
                )
                assert_metadata(
                    inspected["result"],
                    state="seeded",
                    schema_version=1,
                    revision=2,
                    recovery_state="recovery_required",
                )
                return

            assert listed["ok"] is True
            restarting = len(listed["result"]["services"]) == 1
            seed = {
                "domain_name": "release_smoke",
                "domain_class": "release",
                "intent_description": "installed distribution smoke",
                "idempotency_key": "release-seed",
            }
            seeded = await call(session, "aipcs_service_seed", seed)
            assert_metadata(
                seeded["result"],
                state="seeded",
                schema_version=None,
                revision=1,
            )
            service_id = seeded["result"]["service_id"]
            design = {
                "service_id": service_id,
                "schema": manifest,
                "idempotency_key": "release-design",
            }
            designed = await call(session, "aipcs_service_design", design)
            assert_metadata(
                designed["result"],
                state="seeded",
                schema_version=1,
                revision=2,
            )
            assert await call(session, "aipcs_service_design", design) == designed
            materialise = {
                "service_id": service_id,
                "expected_service_revision": 2,
                "expected_schema_version": 1,
                "idempotency_key": "release-materialise",
            }
            materialised = await call(
                session, "aipcs_service_materialise", materialise
            )
            assert_metadata(
                materialised["result"],
                state="materialised",
                schema_version=1,
                revision=3,
                storage=True,
            )
            assert await call(
                session, "aipcs_service_materialise", materialise
            ) == materialised
            assert_failure(
                await call(
                    session,
                    "aipcs_service_materialise",
                    {**materialise, "expected_service_revision": 3},
                ),
                "changed_fingerprint",
            )
            stale = {
                "service_id": service_id,
                "expected_service_revision": 2,
                "expected_schema_version": 1,
                "idempotency_key": "release-stale",
                "schema": evolved_manifest,
            }
            assert_failure(
                await call(session, "aipcs_service_evolve", stale),
                "stale_revision",
            )
            evolve = {
                "service_id": service_id,
                "expected_service_revision": 3,
                "expected_schema_version": 1,
                "idempotency_key": "release-evolve",
                "schema": evolved_manifest,
            }
            evolved = await call(session, "aipcs_service_evolve", evolve)
            assert_metadata(
                evolved["result"],
                state="materialised",
                schema_version=2,
                revision=4,
                storage=True,
            )
            assert await call(session, "aipcs_service_evolve", evolve) == evolved
            database = (
                root
                / "service-stores"
                / f"{evolved['result']['storage']['namespace']}.sqlite"
            )
            state = await prove_data_runtime(
                session, service_id, database, restarting
            )
            if restarting:
                assert json.loads(state_path.read_text(encoding="utf-8")) == state
            else:
                state_path.write_text(
                    json.dumps(state, sort_keys=True), encoding="utf-8"
                )
            inspected = await call(
                session, "aipcs_service_inspect", {"service_id": service_id}
            )
            assert inspected["ok"] is True and inspected["result"] == evolved["result"]
            final_list = await call(session, "aipcs_service_list", {})
            assert final_list["ok"] is True
            assert len(final_list["result"]["services"]) == 1


anyio.run(main)
"""


def _run_smoke_client(
    python: Path,
    workspace: Path,
    executable: Path,
    sqlite_root: Path,
    principal: str,
    mode: str,
    *,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> None:
    client = workspace / "installed_smoke_client.py"
    client.write_text(_smoke_client_program(), encoding="utf-8")
    run_stage(
        f"installed {mode} smoke",
        [python, "-I", client, executable, sqlite_root, principal, mode],
        cwd=workspace,
        environment=environment,
        timeout=SMOKE_TIMEOUT_SECONDS,
        redaction_roots=redaction_roots,
    )


def _catalog_smoke_program() -> str:
    """Standalone installed private-catalog proof with no checkout imports."""

    return r"""
import shutil
import site
import sqlite3
import sys
from pathlib import Path
from uuid import UUID

import aipcs_mcp.storage.sqlite.service_store as service_store_module
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteServiceStoreCatalog

root, mode = Path(sys.argv[1]), sys.argv[2]
origin = Path(service_store_module.__file__).resolve()
sites = tuple(Path(value).resolve() for value in site.getsitepackages())
assert any(origin.is_relative_to(value) for value in sites), origin

def catalog():
    return SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root))

def database(locator):
    return root / "service-stores" / f"{locator.namespace}.sqlite"

def migration_row(locator):
    uri = "file:" + str(database(locator)) + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        return connection.execute(
            'SELECT component,revision,migration_id,checksum,applied_at '
            'FROM "__aipcs_service_store_migration"'
        ).fetchone()

first = catalog()
locator_a = first.allocate(UUID(int=1))
assert locator_a.namespace == "svc_00000000000000000000000000000001"
if mode == "restart":
    assert root.is_dir()
    assert first.inspect_migration(locator_a).status == "ready"
    assert migration_row(locator_a) is not None
    raise SystemExit(0)
if mode not in {"baseline", "heavy"}:
    raise AssertionError("unknown installed catalog smoke mode")

assert not root.exists()
missing = first.inspect_migration(locator_a)
assert (missing.component, missing.applied_revision, missing.target_revision, missing.status) == (
    "service_store", 0, 3, "uninitialised"
)
assert not root.exists()

ready = first.migrate(locator_a)
assert (ready.component, ready.applied_revision, ready.target_revision, ready.status) == (
    "service_store", 3, 3, "ready"
)
db_a = database(locator_a)
assert db_a.is_file()
with sqlite3.connect(f"file:{db_a}?mode=ro", uri=True) as connection:
    objects = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE substr(name,1,7) <> 'sqlite_'"
        )
    }
assert objects == {
    "__aipcs_service_store_meta",
    "__aipcs_service_store_migration",
    "__aipcs_service_store_policy",
    "__aipcs_record_mutation",
    "__aipcs_record_history",
    "__aipcs_memory_branch",
    "__aipcs_record_branch",
    "__aipcs_record_history_record_version",
    "__aipcs_record_history_entity_sequence",
    "__aipcs_memory_branch_principal_status_updated",
    "__aipcs_record_branch_primary",
    "__aipcs_record_branch_branch_lookup",
}
with sqlite3.connect(f"file:{db_a}?mode=ro", uri=True) as connection:
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
ledger = migration_row(locator_a)
assert first.migrate(locator_a) == ready
assert migration_row(locator_a) == ledger
assert catalog().inspect_migration(locator_a) == ready

if mode == "heavy":
    second = catalog()
    locator_b = second.allocate(UUID(int=2))
    assert second.inspect_migration(locator_b).status == "uninitialised"
    assert db_a.is_file() and not database(locator_b).exists()
    assert second.migrate(locator_b).status == "ready"
    assert catalog().inspect_migration(locator_a) == ready
    assert catalog().inspect_migration(locator_b).status == "ready"

    locator_c = second.allocate(UUID(int=3))
    db_c = database(locator_c)
    shutil.copyfile(db_a, db_c)
    db_c.chmod(0o600)
    substituted = catalog().inspect_migration(locator_c)
    assert substituted.status == "incompatible"
"""


def run_installed_catalog_smoke(
    python: Path,
    workspace: Path,
    name: str,
    *,
    heavy: bool,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> None:
    """Exercise the same catalog baseline under one installed distribution."""

    client = workspace / f"{name}-catalog-smoke.py"
    client.write_text(_catalog_smoke_program(), encoding="utf-8")
    cwd = workspace / f"{name}-catalog-cwd"
    cwd.mkdir(mode=0o700)
    root = workspace / f"{name}-catalog-root"
    run_stage(
        f"installed {name} catalog smoke",
        [python, "-I", client, root, "heavy" if heavy else "baseline"],
        cwd=cwd,
        environment=environment,
        timeout=SMOKE_TIMEOUT_SECONDS,
        redaction_roots=redaction_roots,
    )
    run_stage(
        f"installed {name} catalog restart",
        [python, "-I", client, root, "restart"],
        cwd=cwd,
        environment=environment,
        timeout=SMOKE_TIMEOUT_SECONDS,
        redaction_roots=redaction_roots,
    )


def _domain_schema_smoke_program() -> str:
    """Standalone installed V1-07B/C/D domain-schema proof with no checkout imports."""

    program = r'''
import site
import sqlite3
import sys
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import aipcs_mcp.storage.sqlite.domain_schema as domain_schema_module
import aipcs_mcp.storage.sqlite.domain_schema_layout as domain_schema_layout_module
import aipcs_mcp.storage.sqlite.service_store as service_store_module
import aipcs_mcp.storage.sqlite.service_store_inspection as service_store_inspection_module
import aipcs_mcp.storage.sqlite.sql_tokens as sql_tokens_module
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.relational import classify_transition, compile_manifest
from aipcs_mcp.storage import DomainSchemaState
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteServiceStoreCatalog
from aipcs_mcp.storage.sqlite.domain_schema import SQLiteDomainSchemaStore

root = Path(sys.argv[1])
mode = sys.argv[2]
assert mode in {"initial", "restart"}
sites = tuple(Path(value).resolve() for value in site.getsitepackages())
for module in (
    domain_schema_module,
    domain_schema_layout_module,
    service_store_module,
    service_store_inspection_module,
    sql_tokens_module,
):
    origin = Path(module.__file__).resolve()
    assert any(origin.is_relative_to(value) for value in sites), origin

managed = [
    {"name": "id", "type": "uuid", "required": True, "primary_key": True},
    {"name": "owner_id", "type": "string", "required": True},
    {"name": "created_at", "type": "datetime", "required": True},
    {"name": "updated_at", "type": "datetime", "required": True},
    {"name": "created_via", "type": "string", "required": True},
    {"name": "record_version", "type": "integer", "required": True},
]

def entity(name, attributes):
    return {"name": name, "attributes": deepcopy(managed) + attributes}

source_document = {
    "manifest_version": 2,
    "schema_version": 1,
    "entities": [
        entity("account", [{"name": "label", "type": "string", "required": True}]),
        entity("project", [
            {"name": "title", "type": "string", "required": True},
            {"name": "quantity", "type": "integer"},
            {"name": "ratio", "type": "number"},
            {"name": "enabled", "type": "boolean"},
            {"name": "recorded_at", "type": "datetime"},
            {"name": "account_id", "type": "uuid"},
            {"name": "parent_id", "type": "uuid"},
            {"name": "labels", "type": "string_list"},
        ]),
    ],
    "relationships": [
        {
            "name": "project_account_fk",
            "from": {"entity": "project", "field": "account_id"},
            "to": {"entity": "account", "field": "id"},
            "on_delete": "restrict",
        },
        {
            "name": "project_parent_fk",
            "from": {"entity": "project", "field": "parent_id"},
            "to": {"entity": "project", "field": "id"},
            "on_delete": "restrict",
        },
    ],
    "indices": [
        {
            "name": "project_account_title_unique_idx",
            "entity": "project",
            "fields": ["account_id", "title"],
            "unique": True,
        },
        {
            "name": "project_title_quantity_idx",
            "entity": "project",
            "fields": ["title", "quantity"],
        },
    ],
    "query_patterns": [],
    "discovery_facets": [],
    "migration_history": [],
}
target_document = deepcopy(source_document)
target_document["schema_version"] = 2
target_document["migration_history"] = [{
    "from_schema_version": 1,
    "to_schema_version": 2,
    "operations": ["add project summary and task"],
}]
target_document["entities"][1]["attributes"].append({"name": "summary", "type": "string"})
target_document["entities"].append(entity("task", [
    {"name": "project_id", "type": "uuid", "required": True},
    {"name": "label", "type": "string", "required": True},
]))
target_document["relationships"].append({
    "name": "task_project_fk",
    "from": {"entity": "task", "field": "project_id"},
    "to": {"entity": "project", "field": "id"},
    "on_delete": "restrict",
})
target_document["indices"].extend([
    {
        "name": "project_summary_title_idx",
        "entity": "project",
        "fields": ["summary", "title"],
    },
    {
        "name": "task_project_owner_unique_idx",
        "entity": "task",
        "fields": ["project_id", "owner_id"],
        "unique": True,
    },
])
source_manifest = ManifestV2.model_validate(source_document)
target_manifest = ManifestV2.model_validate(target_document)
source_specification = compile_manifest(source_manifest)
target_specification = compile_manifest(target_manifest)
transition = classify_transition(source_manifest, target_manifest)
policy = SQLiteLocationPolicy(root)
catalog = SQLiteServiceStoreCatalog(policy)
locator = catalog.allocate(UUID(int=41))
store = SQLiteDomainSchemaStore(policy)
database = root / "service-stores" / f"{locator.namespace}.sqlite"

def schema_snapshot():
    with sqlite3.connect(database) as connection:
        return tuple(connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE sql IS NOT NULL ORDER BY type, name"
        ))

def assert_target_state(*, prove_token_tolerance):
    assert store.inspect(locator, target_specification) == DomainSchemaState("ready")
    assert store.inspect(locator, source_specification) == DomainSchemaState("incompatible")
    with sqlite3.connect(database) as connection:
        project = tuple(
            (row[1], row[2], row[3])
            for row in connection.execute('PRAGMA table_xinfo("project")')
        )
        assert project[-9:] == (
            ("title", "TEXT", 1),
            ("quantity", "INTEGER", 0),
            ("ratio", "REAL", 0),
            ("enabled", "INTEGER", 0),
            ("recorded_at", "TEXT", 0),
            ("account_id", "TEXT", 0),
            ("parent_id", "TEXT", 0),
            ("labels", "TEXT", 0),
            ("summary", "TEXT", 0),
        )
        assert connection.execute(
            'SELECT "title","quantity","account_id","summary" FROM "project" WHERE "id"=?',
            ("project-1",),
        ).fetchone() == ("Project", 7, "account-1", None)
        foreign_keys = tuple(connection.execute('PRAGMA foreign_key_list("project")'))
        assert foreign_keys == (
            (0, 0, "project", "parent_id", "id", "RESTRICT", "RESTRICT", "NONE"),
            (1, 0, "account", "account_id", "id", "RESTRICT", "RESTRICT", "NONE"),
        )
        project_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='project'"
        ).fetchone()[0]
        assert "DEFERRABLE" not in project_sql
        ordered = tuple(
            row[2]
            for row in connection.execute('PRAGMA index_xinfo("project_title_quantity_idx")')
            if row[5]
        )
        assert ordered == ("title", "quantity")
        unique_ordered = tuple(
            row[2]
            for row in connection.execute('PRAGMA index_xinfo("project_account_title_unique_idx")')
            if row[5]
        )
        assert unique_ordered == ("account_id", "title")
        assert tuple(connection.execute('PRAGMA foreign_key_list("task")')) == (
            (0, 0, "project", "project_id", "id", "RESTRICT", "RESTRICT", "NONE"),
        )
        assert tuple(
            row[2]
            for row in connection.execute('PRAGMA index_xinfo("project_summary_title_idx")')
            if row[5]
        ) == ("summary", "title")
        assert tuple(
            row[2]
            for row in connection.execute('PRAGMA index_xinfo("task_project_owner_unique_idx")')
            if row[5]
        ) == ("project_id", "owner_id")
        index_flags = {
            row[1]: row[2]
            for row in connection.execute('PRAGMA index_list("project")')
        }
        assert index_flags["project_title_quantity_idx"] == 0
        assert index_flags["project_account_title_unique_idx"] == 1
        if prove_token_tolerance:
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute(
                "UPDATE sqlite_schema SET sql=replace(sql, 'CREATE TABLE', 'CREATE\tTABLE') "
                "WHERE type='table' AND name='project'"
            )
            connection.execute("PRAGMA writable_schema=OFF")
    assert store.inspect(locator, target_specification) == DomainSchemaState("ready")

if mode == "initial":
    assert catalog.migrate(locator).status == "ready"
    assert store.inspect(locator, source_specification) == DomainSchemaState("unmaterialised")
    assert store.materialise(locator, source_specification) == DomainSchemaState("ready")
    assert store.inspect(locator, source_specification) == DomainSchemaState("ready")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            'INSERT INTO "account" ("id","owner_id","created_at","updated_at","created_via",'
            '"record_version","label") VALUES (?,?,?,?,?,?,?)',
            ("account-1", "owner", "t", "t", "smoke", 1, "Account"),
        )
        connection.execute(
            'INSERT INTO "project" ("id","owner_id","created_at","updated_at","created_via",'
            '"record_version","title","quantity","account_id") VALUES (?,?,?,?,?,?,?,?,?)',
            ("project-1", "owner", "t", "t", "smoke", 1, "Project", 7, "account-1"),
        )
    assert store.evolve(locator, transition) == DomainSchemaState("ready")
    assert_target_state(prove_token_tolerance=True)
else:
    assert catalog.inspect_migration(locator).status == "ready"
    assert_target_state(prove_token_tolerance=False)

before_retry = schema_snapshot()
assert store.evolve(locator, transition) == DomainSchemaState("ready")
assert schema_snapshot() == before_retry
assert_target_state(prove_token_tolerance=False)
'''
    return program


def run_installed_domain_schema_smoke(
    python: Path,
    workspace: Path,
    name: str,
    *,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> None:
    """Exercise installed V1-07B/C/D schema convergence across a restart."""

    client = workspace / f"{name}-domain-schema-smoke.py"
    client.write_text(_domain_schema_smoke_program(), encoding="utf-8")
    cwd = workspace / f"{name}-domain-schema-cwd"
    cwd.mkdir(mode=0o700)
    root = workspace / f"{name}-domain-schema-root"
    for mode in ("initial", "restart"):
        run_stage(
            f"installed {name} domain schema {mode}",
            [python, "-I", client, root, mode],
            cwd=cwd,
            environment=environment,
            timeout=SMOKE_TIMEOUT_SECONDS,
            redaction_roots=redaction_roots,
        )


def _relational_contract_smoke_program() -> str:
    """Standalone installed proof for the pure V1-07A relational contract."""

    return r'''
from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
from inspect import Parameter, signature
from pathlib import Path
import site
import sys
from typing import get_type_hints

import aipcs_mcp.manifest_v2 as manifest_module
import aipcs_mcp.relational as relational_module
import aipcs_mcp.storage.contracts as contracts_module
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.relational import (
    RelationalContractError,
    RelationalSpecification,
    RelationalTransition,
    classify_transition,
    compile_manifest,
)
from aipcs_mcp.storage.contracts import (
    DomainSchemaState,
    DomainSchemaStore,
    ServiceStoreLocator,
)

sites = tuple(Path(value).resolve() for value in site.getsitepackages())
for module in (manifest_module, relational_module, contracts_module):
    origin = Path(module.__file__).resolve()
    assert any(origin.is_relative_to(value) for value in sites), origin

managed = [
    {"name": "id", "type": "uuid", "required": True, "primary_key": True},
    {"name": "owner_id", "type": "string", "required": True},
    {"name": "created_at", "type": "datetime", "required": True},
    {"name": "updated_at", "type": "datetime", "required": True},
    {"name": "created_via", "type": "string", "required": True},
    {"name": "record_version", "type": "integer", "required": True},
]

def entity(name, extra=(), **values):
    result = {"name": name, "attributes": deepcopy(managed) + list(extra)}
    result.update(values)
    return result

def document(entities=None, relationships=None, indices=None, *, version=1, history=None, **values):
    result = {
        "manifest_version": 2,
        "schema_version": version,
        "entities": entities or [entity("project", [{"name": "title", "type": "string"}])],
        "relationships": relationships or [],
        "indices": indices or [{"name": "project_title_idx", "entity": "project", "fields": ["title"]}],
        "query_patterns": [],
        "discovery_facets": [],
        "migration_history": history or [],
    }
    result.update(values)
    return result

def validated(value):
    return ManifestV2.model_validate(value)

def rejected(value):
    try:
        validated(value)
    except Exception:
        return
    raise AssertionError("manifest should have been rejected")

# Positive breakable self/cyclic graph, distinct relationship sources, and ordered index fields.
graph = document(
    entities=[
        entity("beta", [{"name": "alpha_id", "type": "uuid"}]),
        entity("alpha", [{"name": "parent_id", "type": "uuid"}, {"name": "beta_id", "type": "uuid"}]),
    ],
    relationships=[
        {"name": "beta_alpha_fk", "from": {"entity": "beta", "field": "alpha_id"}, "to": {"entity": "alpha", "field": "id"}, "on_delete": "restrict"},
        {"name": "alpha_parent_fk", "from": {"entity": "alpha", "field": "parent_id"}, "to": {"entity": "alpha", "field": "id"}, "on_delete": "restrict"},
        {"name": "alpha_beta_fk", "from": {"entity": "alpha", "field": "beta_id"}, "to": {"entity": "beta", "field": "id"}, "on_delete": "restrict"},
    ],
    indices=[
        {"name": "beta_alpha_idx", "entity": "beta", "fields": ["alpha_id", "owner_id"]},
        {"name": "alpha_beta_idx", "entity": "alpha", "fields": ["beta_id", "parent_id"]},
    ],
)
source = validated(graph)
specification = compile_manifest(source)
assert [value.name for value in specification.entities] == ["alpha", "beta"]
assert [value.name for value in specification.relationships] == ["alpha_beta_fk", "alpha_parent_fk", "beta_alpha_fk"]
assert specification.indices[0].fields == ("beta_id", "parent_id")
assert {(value.on_delete, value.on_update, value.constraint_timing) for value in specification.relationships} == {("restrict", "restrict", "immediate")}
source.entities[0].attributes[0].name = "mutated"
assert specification.entities[0].fields[0].name == "id"
assert specification.entities[1].fields[0].name == "id"
try:
    specification.entities[0].fields[0].name = "mutated"
except FrozenInstanceError:
    pass
else:
    raise AssertionError("compiled nested field is mutable")
try:
    specification.entities += ()
except FrozenInstanceError:
    pass
else:
    raise AssertionError("compiled specification is mutable")

# Every V1-07A manifest fail-closed category is checked without storage I/O.
bad = document(); bad["entities"][0]["name"] = "sqlite_project"; rejected(bad)
bad = document(indices=[{"name": "sqlite_project_idx", "entity": "project", "fields": ["title"]}]); rejected(bad)
bad = document(indices=[{"name": "project", "entity": "project", "fields": ["title"]}]); rejected(bad)
bad = document(entities=[entity("project", [{"name": "score", "type": "number", "allowed_values": [float("nan")]}])]); rejected(bad)
bad = document(entities=[entity("project", [{"name": "label", "type": "string", "allowed_values": ["bad\x00value"]}])]); rejected(bad)
bad = document(entities=[entity("project", [{"name": "labels", "type": "string_list", "allowed_values": ["bad\x00value"]}])]); rejected(bad)
bad = document(version=66); rejected(bad)
bad = document(version=2, history=[]); rejected(bad)
bad = document(version=2, history=[{"from_schema_version": 1, "to_schema_version": 2, "operations": []}]); rejected(bad)
bad = document(version=2, history=[{"from_schema_version": 1, "to_schema_version": 2, "operations": ["x" * 241]}]); rejected(bad)
bad = document(version=3, history=[{"from_schema_version": 1, "to_schema_version": 3, "operations": ["skip"]}, {"from_schema_version": 2, "to_schema_version": 3, "operations": ["next"]}]); rejected(bad)
bad = deepcopy(graph); bad["relationships"] = [{"name": "id_source_fk", "from": {"entity": "alpha", "field": "id"}, "to": {"entity": "beta", "field": "id"}, "on_delete": "restrict"}]; rejected(bad)
bad = deepcopy(graph); bad["relationships"] = [
    {"name": "one_fk", "from": {"entity": "alpha", "field": "beta_id"}, "to": {"entity": "beta", "field": "id"}, "on_delete": "restrict"},
    {"name": "two_fk", "from": {"entity": "alpha", "field": "beta_id"}, "to": {"entity": "alpha", "field": "id"}, "on_delete": "restrict"},
]; rejected(bad)
required_cycle = document(entities=[entity("left", [{"name": "right_id", "type": "uuid", "required": True}]), entity("right", [{"name": "left_id", "type": "uuid", "required": True}])], relationships=[
    {"name": "left_right_fk", "from": {"entity": "left", "field": "right_id"}, "to": {"entity": "right", "field": "id"}, "on_delete": "restrict"},
    {"name": "right_left_fk", "from": {"entity": "right", "field": "left_id"}, "to": {"entity": "left", "field": "id"}, "on_delete": "restrict"},
]); rejected(required_cycle)
required_self_loop = document(entities=[entity("node", [{"name": "parent_id", "type": "uuid", "required": True}])], relationships=[
    {"name": "node_parent_fk", "from": {"entity": "node", "field": "parent_id"}, "to": {"entity": "node", "field": "id"}, "on_delete": "restrict"},
]); rejected(required_self_loop)
larger_cycle = deepcopy(graph)
for item in larger_cycle["entities"]:
    for field in item["attributes"]:
        if field["name"] in {"alpha_id", "beta_id"}:
            field["required"] = True
rejected(larger_cycle)

class ManifestSubclass(ManifestV2):
    pass

try:
    compile_manifest(ManifestSubclass.model_validate(document()))
except RelationalContractError:
    pass
else:
    raise AssertionError("ManifestV2 subclass bypassed compiler")
try:
    compile_manifest(ManifestV2.model_construct(manifest_version=2, schema_version="bad"))
except RelationalContractError:
    pass
else:
    raise AssertionError("forged manifest bypassed compiler")
mutated = validated(document())
mutated.schema_version = 0
try:
    compile_manifest(mutated)
except RelationalContractError:
    pass
else:
    raise AssertionError("mutated manifest bypassed compiler")

# Additive transition and its rejected rebuild/retrofit/narrowing counterparts.
current_payload = document(entities=[entity("project", [{"name": "title", "type": "string"}, {"name": "state", "type": "string", "allowed_values": ["open", "closed"]}])])
target_payload = deepcopy(current_payload)
target_payload["schema_version"] = 2
target_payload["migration_history"] = [{"from_schema_version": 1, "to_schema_version": 2, "operations": ["add optional project summary and task"]}]
target_payload["entities"][0]["attributes"].append({"name": "summary", "type": "string"})
target_payload["entities"][0]["attributes"][7]["allowed_values"] = ["closed", "open", "paused"]
target_payload["entities"].append(entity("task", [{"name": "project_id", "type": "uuid", "required": True}]))
target_payload["relationships"] = [{"name": "task_project_fk", "from": {"entity": "task", "field": "project_id"}, "to": {"entity": "project", "field": "id"}, "on_delete": "restrict"}]
target_payload["indices"].append({"name": "task_project_idx", "entity": "task", "fields": ["project_id", "owner_id"], "unique": True})
transition = classify_transition(validated(current_payload), validated(target_payload))
assert [value.name for value in transition.additions.entities] == ["task"]
assert [(value.entity, value.field.name) for value in transition.additions.fields] == [("project", "summary")]
assert [value.name for value in transition.additions.relationships] == ["task_project_fk"]
assert [value.name for value in transition.additions.indices] == ["task_project_idx"]
assert transition.current != transition.target
metadata_only = deepcopy(current_payload)
metadata_only["schema_version"] = 2
metadata_only["migration_history"] = [{"from_schema_version": 1, "to_schema_version": 2, "operations": ["describe project"]}]
metadata_only["entities"][0]["description"] = "changed"
assert compile_manifest(validated(current_payload)).same_structure_as(compile_manifest(validated(metadata_only)))
for change in ("required", "reorder", "retrofit", "narrow"):
    candidate = deepcopy(target_payload)
    if change == "required":
        candidate["entities"][0]["attributes"].append({"name": "must_have", "type": "string", "required": True})
    elif change == "reorder":
        candidate["entities"][0]["attributes"][6:8] = reversed(candidate["entities"][0]["attributes"][6:8])
    elif change == "retrofit":
        candidate["entities"][0]["attributes"].append({"name": "task_id", "type": "uuid"})
        candidate["relationships"].append({"name": "project_task_fk", "from": {"entity": "project", "field": "task_id"}, "to": {"entity": "task", "field": "id"}, "on_delete": "restrict"})
    else:
        candidate["entities"][0]["attributes"][7]["allowed_values"] = ["open"]
    try:
        classify_transition(validated(current_payload), validated(candidate))
    except RelationalContractError:
        pass
    else:
        raise AssertionError(f"{change} transition bypassed classifier")

port_signatures = (
    ("inspect", ["self", "locator", "specification"], {"locator": ServiceStoreLocator, "specification": RelationalSpecification, "return": DomainSchemaState}),
    ("materialise", ["self", "locator", "specification"], {"locator": ServiceStoreLocator, "specification": RelationalSpecification, "return": DomainSchemaState}),
    ("evolve", ["self", "locator", "transition"], {"locator": ServiceStoreLocator, "transition": RelationalTransition, "return": DomainSchemaState}),
)
for name, expected, hints in port_signatures:
    method = getattr(DomainSchemaStore, name)
    parameters = list(signature(method).parameters.values())
    assert [parameter.name for parameter in parameters] == expected
    assert all(parameter.kind is Parameter.POSITIONAL_OR_KEYWORD and parameter.default is Parameter.empty for parameter in parameters)
    assert get_type_hints(method) == hints
assert ServiceStoreLocator.for_service("sqlite", __import__("uuid").UUID(int=1)).namespace.endswith("1".zfill(32))
'''


def run_installed_relational_contract_smoke(
    python: Path,
    workspace: Path,
    name: str,
    *,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> None:
    """Run the V1-07A pure-contract proof from an external cwd in isolated mode."""

    client = workspace / f"{name}-relational-contract-smoke.py"
    client.write_text(_relational_contract_smoke_program(), encoding="utf-8")
    cwd = workspace / f"{name}-relational-contract-cwd"
    cwd.mkdir(mode=0o700)
    run_stage(
        f"installed {name} relational contract smoke",
        [python, "-I", client],
        cwd=cwd,
        environment=environment,
        timeout=SMOKE_TIMEOUT_SECONDS,
        redaction_roots=redaction_roots,
    )


def _lifecycle_contract_smoke_program() -> str:
    """Standalone installed proof for the pure V1-08A lifecycle contract."""

    return r'''
from __future__ import annotations

from copy import deepcopy
from itertools import product
from pathlib import Path
import site
import sqlite3
from uuid import UUID

cwd = Path.cwd()
before = tuple(cwd.iterdir())
assert before == (), before

import aipcs_mcp.lifecycle as lifecycle_module
import aipcs_mcp.manifest_v2 as manifest_module
from aipcs_mcp.lifecycle import (
    DeferredResult,
    DomainObservation,
    EvolveCommand,
    EvolveRecoveryObservation,
    FoundationObservation,
    LifecycleAdmission,
    LifecycleAdmissionStatus,
    LifecycleClaim,
    LifecycleClaimStatus,
    LifecycleContractError,
    LifecyclePhase,
    LifecycleResultCategory,
    MaterialiseCommand,
    MaterialiseRecoveryObservation,
    RecoveryAction,
    canonical_lifecycle_json,
    lifecycle_fingerprint,
    lifecycle_result_retryable,
    plan_recovery,
    prepare_intent,
)
from aipcs_mcp.manifest_v2 import ManifestV2

sites = tuple(Path(value).resolve() for value in site.getsitepackages())
for module in (lifecycle_module, manifest_module):
    origin = Path(module.__file__).resolve()
    assert any(origin.is_relative_to(value) for value in sites), origin

def blocked_connect(*_args, **_kwargs):
    raise AssertionError("pure lifecycle contract must not open SQLite")

sqlite3.connect = blocked_connect

managed = [
    {"name": "id", "type": "uuid", "required": True, "primary_key": True},
    {"name": "owner_id", "type": "string", "required": True},
    {"name": "created_at", "type": "datetime", "required": True},
    {"name": "updated_at", "type": "datetime", "required": True},
    {"name": "created_via", "type": "string", "required": True},
    {"name": "record_version", "type": "integer", "required": True},
]

def manifest(version=1):
    payload = {
        "manifest_version": 2,
        "schema_version": version,
        "entities": [{"name": "project", "attributes": deepcopy(managed) + [
            {"name": "title", "type": "string", "required": True},
        ]}],
        "relationships": [],
        "indices": [{"name": "project_owner_idx", "entity": "project", "fields": ["owner_id"]}],
        "query_patterns": ["Find projects by owner."],
        "discovery_facets": [{"entity": "project", "field": "owner_id"}],
        "retrieval_guidance": "Use exact owner filters before listing.",
        "migration_history": [
            {
                "from_schema_version": prior,
                "to_schema_version": prior + 1,
                "operations": [f"advance schema to version {prior + 1}"],
            }
            for prior in range(1, version)
        ],
    }
    return ManifestV2.model_validate(payload)

service_id = UUID("5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23")
materialise = MaterialiseCommand(
    principal_id="principal-a",
    created_via="mcp",
    service_id=service_id,
    expected_service_revision=7,
    expected_schema_version=1,
    idempotency_key="materialise-1",
)
target = manifest(2)
evolve = EvolveCommand(
    principal_id="principal-a",
    created_via="mcp",
    service_id=service_id,
    expected_service_revision=8,
    expected_schema_version=1,
    idempotency_key="evolve-1",
    target_manifest=target,
)

known_json = (
    '{"created_via":"mcp","expected_schema_version":1,'
    '"expected_service_revision":7,"kind":"materialise",'
    '"principal":"principal-a","service_id":"5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23"}'
)
assert canonical_lifecycle_json(materialise) == known_json
assert lifecycle_fingerprint(materialise) == "49f31d4f8ae992243644fc2aa7bcb8eadabeb03c74040dae4d1b7b96bd679839"
known_evolve_json = (
    '{"created_via":"mcp","expected_schema_version":1,'
    '"expected_service_revision":8,"kind":"evolve",'
    '"principal":"principal-a","service_id":"5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23",'
    '"target_manifest":{"discovery_facets":[{"entity":"project","field":"owner_id"}],'
    '"entities":[{"attributes":['
    '{"allowed_values":null,"description":null,"name":"id","primary_key":true,'
    '"required":true,"retrieval_mode":null,"type":"uuid"},'
    '{"allowed_values":null,"description":null,"name":"owner_id","primary_key":false,'
    '"required":true,"retrieval_mode":null,"type":"string"},'
    '{"allowed_values":null,"description":null,"name":"created_at","primary_key":false,'
    '"required":true,"retrieval_mode":null,"type":"datetime"},'
    '{"allowed_values":null,"description":null,"name":"updated_at","primary_key":false,'
    '"required":true,"retrieval_mode":null,"type":"datetime"},'
    '{"allowed_values":null,"description":null,"name":"created_via","primary_key":false,'
    '"required":true,"retrieval_mode":null,"type":"string"},'
    '{"allowed_values":null,"description":null,"name":"record_version","primary_key":false,'
    '"required":true,"retrieval_mode":null,"type":"integer"},'
    '{"allowed_values":null,"description":null,"name":"title","primary_key":false,'
    '"required":true,"retrieval_mode":null,"type":"string"}],'
    '"description":null,"name":"project"}],"indices":[{"entity":"project",'
    '"fields":["owner_id"],"name":"project_owner_idx","unique":false}],"manifest_version":2,'
    '"migration_history":[{"from_schema_version":1,"operations":['
    '"advance schema to version 2"],"to_schema_version":2}],"query_patterns":['
    '"Find projects by owner."],"relationships":[],'
    '"retrieval_guidance":"Use exact owner filters before listing.","schema_version":2}}'
)
assert canonical_lifecycle_json(evolve) == known_evolve_json
assert lifecycle_fingerprint(evolve) == "b22745fa5b9825dba2cca535c649931cda60da46f160d468814269d7ad9bad56"

original_fingerprint = lifecycle_fingerprint(evolve)
object.__setattr__(target, "schema_version", 1)
target.entities[0].attributes[-1].name = "mutated_title"
assert evolve.target_manifest.schema_version == 2
assert evolve.target_manifest.entities[0].attributes[-1].name == "title"
exposed_target = evolve.target_manifest
exposed_target.entities[0].attributes[-1].name = "forged_title"
assert evolve.target_manifest.entities[0].attributes[-1].name == "title"
assert lifecycle_fingerprint(evolve) == original_fingerprint

for forged in (
    ManifestV2.model_construct(manifest_version=2, schema_version="bad"),
    target,
):
    try:
        EvolveCommand(
            principal_id="principal-a",
            created_via="mcp",
            service_id=service_id,
            expected_service_revision=8,
            expected_schema_version=1,
            idempotency_key="forged",
            target_manifest=forged,
        )
    except LifecycleContractError:
        pass
    else:
        raise AssertionError("forged or mutable target manifest was accepted")

initial = manifest(1)
materialise_intent = prepare_intent(materialise, initial)
evolve_intent = prepare_intent(evolve, manifest(2))
completed = materialise_intent.with_phase(LifecyclePhase.COMPLETED)
recovery_required = materialise_intent.with_phase(LifecyclePhase.RECOVERY_REQUIRED)
evolve_completed = evolve_intent.with_phase(LifecyclePhase.COMPLETED)
evolve_recovery_required = evolve_intent.with_phase(LifecyclePhase.RECOVERY_REQUIRED)

assert LifecycleClaim(LifecycleClaimStatus.NEW).intent is None
assert LifecycleClaim(LifecycleClaimStatus.CONFLICT).intent is None
assert LifecycleClaim(LifecycleClaimStatus.REPLAY_COMPLETE, completed).intent is completed
assert LifecycleClaim(LifecycleClaimStatus.RESUME_PREPARED, materialise_intent).intent is materialise_intent
assert LifecycleClaim(LifecycleClaimStatus.RECOVERY_REQUIRED, recovery_required).intent is recovery_required
assert LifecycleAdmission(LifecycleAdmissionStatus.PREPARED, materialise_intent).intent is materialise_intent
assert LifecycleAdmission(LifecycleAdmissionStatus.OPERATION_IN_PROGRESS, materialise_intent).intent is materialise_intent
assert LifecycleAdmission(LifecycleAdmissionStatus.RECOVERY_REQUIRED, recovery_required).intent is recovery_required

retryability = {
    LifecycleResultCategory.MALFORMED_INPUT: False,
    LifecycleResultCategory.UNSUPPORTED_TRANSITION: False,
    LifecycleResultCategory.STALE_REVISION: False,
    LifecycleResultCategory.CHANGED_FINGERPRINT: False,
    LifecycleResultCategory.OPERATION_IN_PROGRESS: True,
    LifecycleResultCategory.RECOVERY_REQUIRED: False,
    LifecycleResultCategory.STORAGE_BUSY: True,
    LifecycleResultCategory.OPERATION_UNCERTAIN: True,
    LifecycleResultCategory.STORAGE_UNAVAILABLE: False,
    LifecycleResultCategory.INTERNAL_FAILURE: False,
}
assert set(retryability) == set(LifecycleResultCategory)
for category, retryable in retryability.items():
    assert lifecycle_result_retryable(category) is retryable
try:
    lifecycle_result_retryable("storage_busy")
except LifecycleContractError:
    pass
else:
    raise AssertionError("untyped lifecycle result category was accepted")

def assert_plan(intent, observation, action, deferred=None):
    result = plan_recovery(intent, observation)
    assert result.action is action
    assert result.deferred_result is deferred
    if action is RecoveryAction.RECOVERY_REQUIRED:
        category = LifecycleResultCategory.RECOVERY_REQUIRED
    elif action is RecoveryAction.DEFER_OBSERVATION:
        category = {
            DeferredResult.STORAGE_BUSY: LifecycleResultCategory.STORAGE_BUSY,
            DeferredResult.OPERATION_UNCERTAIN: LifecycleResultCategory.OPERATION_UNCERTAIN,
            DeferredResult.STORAGE_UNAVAILABLE: LifecycleResultCategory.STORAGE_UNAVAILABLE,
        }[deferred]
    else:
        category = None
    assert result.result_category is category
    assert result.retryable is (category in {
        LifecycleResultCategory.STORAGE_BUSY,
        LifecycleResultCategory.OPERATION_UNCERTAIN,
    })

def deferred_expected(value):
    return {
        FoundationObservation.BUSY: DeferredResult.STORAGE_BUSY,
        FoundationObservation.UNCERTAIN: DeferredResult.OPERATION_UNCERTAIN,
        FoundationObservation.UNAVAILABLE: DeferredResult.STORAGE_UNAVAILABLE,
        DomainObservation.BUSY: DeferredResult.STORAGE_BUSY,
        DomainObservation.UNCERTAIN: DeferredResult.OPERATION_UNCERTAIN,
        DomainObservation.UNAVAILABLE: DeferredResult.STORAGE_UNAVAILABLE,
    }.get(value)

def materialise_valid(phase, foundation, target):
    return (
        phase is not LifecyclePhase.PREPARED
        and foundation is FoundationObservation.NOT_OBSERVED
        and target is DomainObservation.NOT_OBSERVED
    ) or (
        phase is LifecyclePhase.PREPARED
        and foundation is FoundationObservation.READY
        and target is not DomainObservation.NOT_OBSERVED
    ) or (
        phase is LifecyclePhase.PREPARED
        and foundation in {
            FoundationObservation.UNINITIALISED,
            FoundationObservation.OUTDATED,
            FoundationObservation.DIRTY,
            FoundationObservation.INCOMPATIBLE,
            FoundationObservation.BUSY,
            FoundationObservation.UNAVAILABLE,
            FoundationObservation.UNCERTAIN,
        }
        and target is DomainObservation.NOT_OBSERVED
    )

def materialise_expected(phase, foundation, target):
    if phase is LifecyclePhase.COMPLETED:
        return RecoveryAction.REPLAY_COMPLETE, None
    if phase is LifecyclePhase.RECOVERY_REQUIRED:
        return RecoveryAction.RECOVERY_REQUIRED, None
    deferred = deferred_expected(foundation) or deferred_expected(target)
    if deferred is not None:
        return RecoveryAction.DEFER_OBSERVATION, deferred
    if foundation in {
        FoundationObservation.UNINITIALISED,
        FoundationObservation.OUTDATED,
        FoundationObservation.DIRTY,
    }:
        return RecoveryAction.PREPARE_FOUNDATION, None
    if foundation is FoundationObservation.READY:
        if target is DomainObservation.UNMATERIALISED:
            return RecoveryAction.APPLY_INITIAL_SCHEMA, None
        if target is DomainObservation.READY:
            return RecoveryAction.FINALIZE_REGISTRY, None
    return RecoveryAction.RECOVERY_REQUIRED, None

materialise_intents = {
    LifecyclePhase.PREPARED: materialise_intent,
    LifecyclePhase.COMPLETED: completed,
    LifecyclePhase.RECOVERY_REQUIRED: recovery_required,
}
materialise_valid_count = 0
for phase, foundation, target in product(
    LifecyclePhase, FoundationObservation, DomainObservation
):
    valid = materialise_valid(phase, foundation, target)
    if not valid:
        try:
            MaterialiseRecoveryObservation(phase, foundation, target)
        except LifecycleContractError:
            continue
        raise AssertionError("invalid materialise recovery observation was accepted")
    materialise_valid_count += 1
    observation = MaterialiseRecoveryObservation(phase, foundation, target)
    assert_plan(materialise_intents[phase], observation, *materialise_expected(phase, foundation, target))
assert materialise_valid_count == 15

def evolve_valid(phase, foundation, target, source):
    terminal = (
        phase is not LifecyclePhase.PREPARED
        and foundation is FoundationObservation.NOT_OBSERVED
        and target is DomainObservation.NOT_OBSERVED
        and source is DomainObservation.NOT_OBSERVED
    )
    non_ready_foundation = (
        phase is LifecyclePhase.PREPARED
        and foundation is not FoundationObservation.READY
        and foundation is not FoundationObservation.NOT_OBSERVED
        and target is DomainObservation.NOT_OBSERVED
        and source is DomainObservation.NOT_OBSERVED
    )
    ready_target = phase is LifecyclePhase.PREPARED and foundation is FoundationObservation.READY
    ready_target = ready_target and (
        (target in {
            DomainObservation.READY,
            DomainObservation.BUSY,
            DomainObservation.UNAVAILABLE,
            DomainObservation.UNCERTAIN,
        } and source is DomainObservation.NOT_OBSERVED)
        or (
            target is DomainObservation.UNMATERIALISED
            and source in {
                DomainObservation.UNMATERIALISED,
                DomainObservation.BUSY,
                DomainObservation.UNAVAILABLE,
                DomainObservation.UNCERTAIN,
            }
        )
        or (
            target is DomainObservation.INCOMPATIBLE
            and source in {
                DomainObservation.READY,
                DomainObservation.INCOMPATIBLE,
                DomainObservation.BUSY,
                DomainObservation.UNAVAILABLE,
                DomainObservation.UNCERTAIN,
            }
        )
    )
    return terminal or non_ready_foundation or ready_target

def evolve_expected(phase, foundation, target, source):
    if phase is LifecyclePhase.COMPLETED:
        return RecoveryAction.REPLAY_COMPLETE, None
    if phase is LifecyclePhase.RECOVERY_REQUIRED:
        return RecoveryAction.RECOVERY_REQUIRED, None
    deferred = deferred_expected(foundation) or deferred_expected(target) or deferred_expected(source)
    if deferred is not None:
        return RecoveryAction.DEFER_OBSERVATION, deferred
    if foundation in {FoundationObservation.OUTDATED, FoundationObservation.DIRTY}:
        return RecoveryAction.PREPARE_FOUNDATION, None
    if foundation is not FoundationObservation.READY:
        return RecoveryAction.RECOVERY_REQUIRED, None
    if target is DomainObservation.READY:
        return RecoveryAction.FINALIZE_REGISTRY, None
    if target is DomainObservation.INCOMPATIBLE and source is DomainObservation.READY:
        return RecoveryAction.APPLY_TRANSITION, None
    return RecoveryAction.RECOVERY_REQUIRED, None

evolve_intents = {
    LifecyclePhase.PREPARED: evolve_intent,
    LifecyclePhase.COMPLETED: evolve_completed,
    LifecyclePhase.RECOVERY_REQUIRED: evolve_recovery_required,
}
evolve_valid_count = 0
for phase, foundation, target, source in product(
    LifecyclePhase, FoundationObservation, DomainObservation, DomainObservation
):
    valid = evolve_valid(phase, foundation, target, source)
    if not valid:
        try:
            EvolveRecoveryObservation(phase, foundation, target, source)
        except LifecycleContractError:
            continue
        raise AssertionError("invalid evolve recovery observation was accepted")
    evolve_valid_count += 1
    observation = EvolveRecoveryObservation(phase, foundation, target, source)
    assert_plan(
        evolve_intents[phase], observation, *evolve_expected(phase, foundation, target, source)
    )
assert evolve_valid_count == 22

assert tuple(cwd.iterdir()) == before
'''


def run_installed_lifecycle_contract_smoke(
    python: Path,
    workspace: Path,
    name: str,
    *,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> None:
    """Run the V1-08A pure-contract proof from an empty external cwd."""

    client = workspace / f"{name}-lifecycle-contract-smoke.py"
    client.write_text(_lifecycle_contract_smoke_program(), encoding="utf-8")
    cwd = workspace / f"{name}-lifecycle-contract-cwd"
    cwd.mkdir(mode=0o700)
    run_stage(
        f"installed {name} lifecycle contract smoke",
        [python, "-I", client],
        cwd=cwd,
        environment=environment,
        timeout=SMOKE_TIMEOUT_SECONDS,
        redaction_roots=redaction_roots,
    )


def _registry_r2_smoke_program() -> str:
    """Standalone installed registry R2-to-R4 migration and authority proof."""

    program = r'''
from __future__ import annotations

import hashlib
import json
import os
import shutil
import site
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import aipcs_mcp.application.models as models_module
import aipcs_mcp.lifecycle as lifecycle_module
import aipcs_mcp.manifest_v2 as manifest_module
import aipcs_mcp.storage as storage_module
import aipcs_mcp.storage.sqlite as sqlite_module
import aipcs_mcp.storage.sqlite.migrations as migrations_module
from aipcs_mcp.application.models import (
    CompletedLifecycleClaim,
    CompletedNonLifecycleClaim,
    MaterialisationStorage,
    MaterialiseCompletion,
    PreparedLifecycleClaim,
    RecoveryRequiredLifecycleClaim,
    Service,
)
from aipcs_mcp.lifecycle import LifecyclePhase, MaterialiseCommand
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.storage import MigrationState
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter
from aipcs_mcp.storage.sqlite.migrations import R1, R2, R3, R4

frozen_r1_ddl = __FROZEN_R1_DDL__
frozen_r1_migration_id = "__FROZEN_R1_MIGRATION_ID__"
frozen_r1_checksum = "__FROZEN_R1_CHECKSUM__"
frozen_r2_migration_id = "__FROZEN_R2_MIGRATION_ID__"
frozen_r2_checksum = "__FROZEN_R2_CHECKSUM__"
assert hashlib.sha256("\n".join(frozen_r1_ddl).encode()).hexdigest() == frozen_r1_checksum
assert R1.ddl == frozen_r1_ddl
assert R1.migration_id == frozen_r1_migration_id
assert R1.checksum == frozen_r1_checksum
assert R2.migration_id == frozen_r2_migration_id
assert R2.checksum == frozen_r2_checksum

cwd = Path.cwd()
assert tuple(cwd.iterdir()) == ()
sites = tuple(Path(value).resolve() for value in site.getsitepackages())
for module in (
    models_module,
    lifecycle_module,
    manifest_module,
    storage_module,
    sqlite_module,
    migrations_module,
):
    origin = Path(module.__file__).resolve()
    assert any(origin.is_relative_to(value) for value in sites), origin
at_text = "2026-01-01T00:00:00.000000Z"
at = datetime(2026, 1, 1, tzinfo=UTC)


def legacy_result(service_id, principal, domain):
    return json.dumps(
        {
            "service_id": service_id,
            "principal_id": principal,
            "domain_name": domain,
            "domain_class": "release",
            "intent_description": f"{domain} release proof",
            "created_at": at_text,
            "updated_at": at_text,
            "last_activity_at": at_text,
            "manifest": None,
            "schema_version": None,
            "design_state": "seeded",
            "operational_status": "active",
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


root = (cwd / "registry-root").resolve()
root.mkdir(mode=0o700)
database = root / "registry.sqlite"
descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(descriptor)
service_a = "00000000-0000-0000-0000-000000000001"
service_b = "00000000-0000-0000-0000-000000000002"
result_a = legacy_result(service_a, "principal-a", "notes_a")
result_b = legacy_result(service_b, "principal-b", "notes_b")
with sqlite3.connect(database) as connection:
    connection.execute("PRAGMA foreign_keys=ON")
    for statement in frozen_r1_ddl:
        connection.execute(statement)
    connection.execute(
        'INSERT INTO "aipcs_registry_meta" VALUES (1,?,?,?,?)',
        ("aipcs.sqlite.registry", "registry", 1, 0),
    )
    connection.execute(
        'INSERT INTO "aipcs_registry_migration" VALUES (?,?,?,?,?)',
        ("registry", 1, frozen_r1_migration_id, frozen_r1_checksum, at_text),
    )
    connection.executemany(
        'INSERT INTO "aipcs_registry_service" VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        [
            (
                service_a,
                "principal-a",
                "notes_a",
                "release",
                "notes_a release proof",
                at_text,
                at_text,
                at_text,
                None,
                None,
                "seeded",
                "active",
            ),
            (
                service_b,
                "principal-b",
                "notes_b",
                "release",
                "notes_b release proof",
                at_text,
                at_text,
                at_text,
                None,
                None,
                "seeded",
                "active",
            ),
        ],
    )
    connection.executemany(
        'INSERT INTO "aipcs_registry_mutation" VALUES (?,?,?,?,?)',
        [
            ("principal-a", "legacy-a", "a" * 64, service_a, result_a),
            ("principal-b", "legacy-b", "b" * 64, service_b, result_b),
        ],
    )
    connection.executemany(
        'INSERT INTO "aipcs_registry_audit" VALUES (?,?,?,?,?,?,?)',
        [
            (audit_id, "seed", "created", service_a, "principal-a", "release", at_text)
            for audit_id in range(1, 1002)
        ]
        + [
            (audit_id, "seed", "created", service_b, "principal-b", "release", at_text)
            for audit_id in range(1002, 1003)
        ],
    )

adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(root))
assert adapter.inspect_migration() == MigrationState("registry", 1, 4, "incompatible")
assert adapter.migrate() == MigrationState("registry", 4, 4, "ready")
assert adapter.inspect_migration() == MigrationState("registry", 4, 4, "ready")
with sqlite3.connect(database) as connection:
    assert connection.execute(
        'SELECT revision,migration_id,checksum FROM "aipcs_registry_migration" ORDER BY revision'
    ).fetchall() == [
        (1, frozen_r1_migration_id, frozen_r1_checksum),
        (2, frozen_r2_migration_id, frozen_r2_checksum),
        (3, R3.migration_id, R3.checksum),
        (4, R4.migration_id, R4.checksum),
    ]
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute(
        'SELECT "service_revision","materialised_at","storage_backend","storage_namespace" '
        'FROM "aipcs_registry_service" ORDER BY "service_id"'
    ).fetchall() == [
        (1, None, None, None),
        (1, None, None, None),
    ]
    assert connection.execute(
        'SELECT "operation_kind","phase","intent_json","result_json" '
        'FROM "aipcs_registry_claim" '
        'WHERE "principal_id"=? AND "idempotency_key"=?',
        ("principal-a", "legacy-a"),
    ).fetchone() == ("legacy", "completed", None, result_a)
    assert connection.execute(
        'SELECT "identity_state","domain_name","storage_backend",'
        '"storage_namespace","claim_idempotency_key","lifecycle_at" '
        'FROM "aipcs_registry_identity" WHERE "service_id"=?',
        (service_a,),
    ).fetchone() == (
        "live", "notes_a", None, f"svc_{UUID(service_a).hex}", None, None
    )
    assert connection.execute(
        'SELECT count(*) FROM "aipcs_registry_receipt"'
    ).fetchone() == (0,)
    assert connection.execute(
        'SELECT count(*),min(audit_id),max(audit_id) FROM "aipcs_registry_audit" '
        'WHERE "principal_id"=?',
        ("principal-a",),
    ).fetchone() == (1000, 2, 1001)
    assert connection.execute(
        'SELECT count(*),min(audit_id),max(audit_id) FROM "aipcs_registry_audit" '
        'WHERE "principal_id"=?',
        ("principal-b",),
    ).fetchone() == (1, 1002, 1002)


def prove_under_cap_retention(count):
    case_root = (cwd / f"retention-{count}-root").resolve()
    case_root.mkdir(mode=0o700)
    case_database = case_root / "registry.sqlite"
    case_descriptor = os.open(
        case_database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
    )
    os.close(case_descriptor)
    with sqlite3.connect(case_database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for statement in frozen_r1_ddl:
            connection.execute(statement)
        connection.execute(
            'INSERT INTO "aipcs_registry_meta" VALUES (1,?,?,?,?)',
            ("aipcs.sqlite.registry", "registry", 1, 0),
        )
        connection.execute(
            'INSERT INTO "aipcs_registry_migration" VALUES (?,?,?,?,?)',
            (
                "registry",
                1,
                frozen_r1_migration_id,
                frozen_r1_checksum,
                at_text,
            ),
        )
        connection.execute(
            'INSERT INTO "aipcs_registry_service" VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                service_a,
                "principal-a",
                "notes_a",
                "release",
                "notes_a release proof",
                at_text,
                at_text,
                at_text,
                None,
                None,
                "seeded",
                "active",
            ),
        )
        connection.execute(
            'INSERT INTO "aipcs_registry_mutation" VALUES (?,?,?,?,?)',
            ("principal-a", "legacy-a", "a" * 64, service_a, result_a),
        )
        connection.executemany(
            'INSERT INTO "aipcs_registry_audit" VALUES (?,?,?,?,?,?,?)',
            [
                (
                    audit_id,
                    "seed",
                    "created",
                    service_a,
                    "principal-a",
                    "release",
                    at_text,
                )
                for audit_id in range(1, count + 1)
            ],
        )
    case_adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(case_root))
    assert case_adapter.inspect_migration() == MigrationState(
        "registry", 1, 4, "incompatible"
    )
    assert case_adapter.migrate() == MigrationState("registry", 4, 4, "ready")
    with sqlite3.connect(case_database) as connection:
        assert connection.execute(
            'SELECT count(*),min(audit_id),max(audit_id) '
            'FROM "aipcs_registry_audit"'
        ).fetchone() == (count, 1, count)


prove_under_cap_retention(999)
prove_under_cap_retention(1000)

uow = adapter.open_uow()
legacy = uow.mutations.resolve_non_lifecycle(
    "seed", "principal-a", "legacy-a", "a" * 64
)
assert isinstance(legacy, CompletedNonLifecycleClaim)
assert legacy.operation_kind == "legacy"
assert str(legacy.service.service_id) == service_a
uow.rollback()
uow.close()

manifest = ManifestV2.model_validate(
    {
        "manifest_version": 2,
        "schema_version": 1,
        "entities": [
            {
                "name": "project",
                "attributes": [
                    {"name": "id", "type": "uuid", "required": True, "primary_key": True},
                    {"name": "owner_id", "type": "string", "required": True},
                    {"name": "created_at", "type": "datetime", "required": True},
                    {"name": "updated_at", "type": "datetime", "required": True},
                    {"name": "created_via", "type": "string", "required": True},
                    {"name": "record_version", "type": "integer", "required": True},
                    {"name": "title", "type": "string", "required": True},
                ],
            }
        ],
        "relationships": [],
        "indices": [{"name": "project_owner_idx", "entity": "project", "fields": ["owner_id"]}],
        "query_patterns": ["Find projects by owner."],
        "discovery_facets": [{"entity": "project", "field": "owner_id"}],
        "retrieval_guidance": "Use exact owner filters before listing.",
        "migration_history": [],
    }
)


def add_service(service_id):
    service = Service(
        service_id=service_id,
        principal_id="lifecycle-principal",
        domain_name=f"service_{service_id.int}",
        domain_class="release",
        intent_description="installed R2 ledger proof",
        created_at=at,
        updated_at=at,
        last_activity_at=at,
        manifest=manifest,
        schema_version=1,
    )
    unit = adapter.open_uow()
    unit.services.add(service)
    unit.commit()
    unit.close()


def admit(service_id, key):
    command = MaterialiseCommand(
        principal_id="lifecycle-principal",
        created_via="release",
        service_id=service_id,
        expected_service_revision=1,
        expected_schema_version=1,
        idempotency_key=key,
    )
    unit = adapter.open_uow()
    claim = unit.mutations.resolve_or_admit(command)
    assert isinstance(claim, PreparedLifecycleClaim)
    unit.commit()
    unit.close()
    return claim.intent


prepared_id = UUID(int=3)
completed_id = UUID(int=4)
recovery_id = UUID(int=5)
for item in (prepared_id, completed_id, recovery_id):
    add_service(item)
prepared_intent = admit(prepared_id, "prepared-key")
completed_intent = admit(completed_id, "completed-key")
recovery_intent = admit(recovery_id, "recovery-key")

uow = adapter.open_uow()
completed = uow.mutations.finalize_completed(
    MaterialiseCompletion(
        completed_intent,
        at,
        MaterialisationStorage("sqlite", f"svc_{completed_id.hex}"),
    )
)
assert isinstance(completed, CompletedLifecycleClaim)
uow.commit()
uow.close()

uow = adapter.open_uow()
recovery = uow.mutations.finalize_recovery_required(recovery_intent, at)
assert isinstance(recovery, RecoveryRequiredLifecycleClaim)
uow.commit()
uow.close()

with sqlite3.connect(database) as connection:
    phases = connection.execute(
        'SELECT "idempotency_key","phase","result_json","recovery_category" '
        'FROM "aipcs_registry_claim" WHERE "principal_id"=? '
        'ORDER BY "idempotency_key"',
        ("lifecycle-principal",),
    ).fetchall()
    invalid_insert = (
        'INSERT INTO "aipcs_registry_claim"('
        '"principal_id","idempotency_key","fingerprint","service_id","operation_kind",'
        '"phase","created_via","intent_json","result_json","recovery_category") '
        'SELECT "principal_id",?,"fingerprint","service_id",?,?,"created_via",'
        '"intent_json",?,? '
        'FROM "aipcs_registry_claim" WHERE "principal_id"=? AND "idempotency_key"=?'
    )
    invalid_rows = (
        ("invalid-kind-key", "unknown", "prepared", None, None),
        ("invalid-phase-key", "materialise", "unknown", None, None),
        ("invalid-completed-key", "materialise", "completed", None, None),
        ("invalid-recovery-key", "materialise", "recovery_required", None, None),
    )
    for values in invalid_rows:
        try:
            connection.execute(
                invalid_insert,
                (*values, "lifecycle-principal", "prepared-key"),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("invalid lifecycle kind/phase/evidence row was accepted")
    assert connection.execute(
        'SELECT count(*) FROM "aipcs_registry_claim" '
        'WHERE "idempotency_key" LIKE \'invalid-%\''
    ).fetchone() == (0,)
assert [(row[0], row[1]) for row in phases] == [
    ("completed-key", LifecyclePhase.COMPLETED.value),
    ("prepared-key", LifecyclePhase.PREPARED.value),
    ("recovery-key", LifecyclePhase.RECOVERY_REQUIRED.value),
]
assert phases[0][2] is not None and phases[0][3] is None
assert phases[1][2:] == (None, None)
assert phases[2][2:] == (None, "recovery_required")
assert adapter.inspect_migration() == MigrationState("registry", 4, 4, "ready")
# A plain main-file copy must not omit committed WAL frames in this legacy corruption check.
with sqlite3.connect(database) as connection:
    connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()

def prove_corruption_fails_closed(name, statement, parameters=()):
    corrupt_root = (cwd / f"corrupt-{name}-root").resolve()
    corrupt_root.mkdir(mode=0o700)
    corrupt_database = corrupt_root / "registry.sqlite"
    shutil.copy2(database, corrupt_database)
    corrupt_database.chmod(0o600)
    with sqlite3.connect(corrupt_database) as connection:
        connection.execute(statement, parameters)
    corrupt_before = corrupt_database.read_bytes()
    corrupt_adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy(corrupt_root))
    assert corrupt_adapter.inspect_migration() == MigrationState(
        "registry", 4, 4, "incompatible"
    )
    assert corrupt_database.read_bytes() == corrupt_before
    assert corrupt_adapter.migrate() == MigrationState(
        "registry", 4, 4, "incompatible"
    )
    assert corrupt_database.read_bytes() == corrupt_before


prove_corruption_fails_closed(
    "row",
    'UPDATE "aipcs_registry_claim" SET "result_json"=? '
    'WHERE "principal_id"=? AND "idempotency_key"=?',
    ("{}", "lifecycle-principal", "completed-key"),
)
prove_corruption_fails_closed(
    "checksum",
    'UPDATE "aipcs_registry_migration" SET "checksum"=? WHERE "revision"=2',
    ("c" * 64,),
)
prove_corruption_fails_closed(
    "schema",
    'DROP INDEX "aipcs_registry_audit_principal"',
)
'''
    return (
        program.replace("__FROZEN_R1_DDL__", repr(_FROZEN_R1_DDL))
        .replace("__FROZEN_R1_MIGRATION_ID__", _FROZEN_R1_MIGRATION_ID)
        .replace("__FROZEN_R1_CHECKSUM__", _FROZEN_R1_CHECKSUM)
        .replace("__FROZEN_R2_MIGRATION_ID__", _FROZEN_R2_MIGRATION_ID)
        .replace("__FROZEN_R2_CHECKSUM__", _FROZEN_R2_CHECKSUM)
    )


def run_installed_registry_r2_smoke(
    python: Path,
    workspace: Path,
    name: str,
    *,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> None:
    """Run the V1-08B installed registry proof from an empty external cwd."""

    client = workspace / f"{name}-registry-r2-smoke.py"
    client.write_text(_registry_r2_smoke_program(), encoding="utf-8")
    cwd = workspace / f"{name}-registry-r2-cwd"
    cwd.mkdir(mode=0o700)
    run_stage(
        f"installed {name} registry R2 smoke",
        [python, "-I", client],
        cwd=cwd,
        environment=environment,
        timeout=SMOKE_TIMEOUT_SECONDS,
        redaction_roots=redaction_roots,
    )


def _wal_release_smoke_program() -> str:
    """Installed R4/R2 WAL proof; child modes deliberately use this same -I program."""

    program = r'''
from __future__ import annotations
import json, os, site, sqlite3, subprocess, sys
from pathlib import Path
from uuid import UUID
import aipcs_mcp.storage.sqlite.adapter as adapter_module
import aipcs_mcp.storage.sqlite.service_store as store_module
from aipcs_mcp.storage import MigrationState, StorageBusy, StorageMigrationError, StorageUnavailable
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter, SQLiteServiceStoreCatalog

root = Path(sys.argv[1]).resolve()
mode = sys.argv[2] if len(sys.argv) > 2 else "parent"
assert sqlite3.sqlite_version_info >= (3, 51, 3)
sites = tuple(Path(value).resolve() for value in site.getsitepackages())
for module in (adapter_module, store_module):
    assert any(Path(module.__file__).resolve().is_relative_to(value) for value in sites)
db = root / "registry.sqlite"
if mode == "holder":
    connection = sqlite3.connect(db, isolation_level=None)
    connection.execute("BEGIN IMMEDIATE")
    print("LOCK_HELD", flush=True)
    sys.stdin.read(1)
    connection.rollback(); connection.close(); raise SystemExit(0)
if mode == "crash":
    connection = sqlite3.connect(db, isolation_level=None)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute('UPDATE "aipcs_registry_meta" SET "dirty"=0')
    connection.commit(); print("COMMIT_RETURNED", flush=True)
    os._exit(0)

root.mkdir(mode=0o700)
registry = SQLiteRegistryAdapter(SQLiteLocationPolicy(root), busy_timeout_ms=50)
assert registry.migrate() == MigrationState("registry", 4, 4, "ready")
assert registry.inspect_migration() == MigrationState("registry", 4, 4, "ready")
with sqlite3.connect(db) as connection:
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute('SELECT phase FROM "aipcs_registry_storage_policy"').fetchone() == ("ready",)
holder = subprocess.Popen([sys.executable, "-I", __file__, str(root), "holder"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
assert holder.stdout.readline().strip() == "LOCK_HELD"
# WAL readers coexist with an IMMEDIATE writer; a second writer is bounded and typed.
assert registry.inspect_migration().status == "ready"
try:
    contender = registry.open_uow()
    try:
        contender.mutations.resolve_non_lifecycle(
            "seed",
            "release-principal",
            "release-busy-key",
            "a" * 64,
        )
    finally:
        contender.close()
except StorageBusy:
    pass
else:
    raise AssertionError("writer was not bounded busy")
holder.stdin.write("x"); holder.stdin.close(); assert holder.wait(timeout=10) == 0
crash = subprocess.Popen([sys.executable, "-I", __file__, str(root), "crash"], stdout=subprocess.PIPE, text=True)
assert crash.stdout.readline().strip() == "COMMIT_RETURNED"; assert crash.wait(timeout=10) == 0
assert registry.inspect_migration().status == "ready"

# SQLite-created operational files are allowed only with secure regular-file metadata.
with sqlite3.connect(db) as connection:
    connection.execute("BEGIN IMMEDIATE"); connection.execute('SELECT 1'); connection.rollback()
for suffix in ("-wal", "-shm"):
    path = Path(str(db) + suffix)
    if path.exists(): assert (path.stat().st_mode & 0o077) == 0
bad = Path(str(db) + "-wal")
bad.unlink(missing_ok=True)
bad.write_bytes(b"hostile"); bad.chmod(0o644)
try:
    registry.inspect_migration()
except (StorageMigrationError, StorageUnavailable):
    pass
else:
    raise AssertionError("hostile sidecar was accepted")
bad.unlink(missing_ok=True)

# Frozen R2 is embedded by the release script; fixture construction deliberately imports no R2.
frozen_r1_ddl = __FROZEN_R1_DDL__
frozen_r2_ddl = __FROZEN_R2_DDL__
registry_policy_ddl = """CREATE TABLE "aipcs_registry_storage_policy" (
    "singleton" INTEGER PRIMARY KEY NOT NULL CHECK ("singleton" = 1),
    "policy_id" TEXT NOT NULL CHECK ("policy_id" = 'aipcs.sqlite.wal.v1'),
    "policy_checksum" TEXT NOT NULL CHECK (
        length("policy_checksum") = 64
        AND "policy_checksum" = lower("policy_checksum")
        AND "policy_checksum" NOT GLOB '*[^0-9a-f]*'
    ),
    "phase" TEXT NOT NULL CHECK ("phase" IN ('prepared', 'ready'))
) STRICT"""
registry_policy_checksum = "661d993c71dfe09a237e8f8018a9636b154ae5f9d39deabcbb8a55b6e49baade"
def frozen_registry(mode):
    fixture_root = root / ("registry-" + mode); fixture_root.mkdir(mode=0o700)
    fixture_db = fixture_root / "registry.sqlite"
    service_id = "00000000-0000-0000-0000-00000000000a"; at = "2026-01-01T00:00:00.000000Z"
    result_json = json.dumps(
        {
            "service_id": service_id,
            "principal_id": "frozen-principal",
            "domain_name": "frozen_domain",
            "domain_class": "release",
            "intent_description": "frozen registry",
            "created_at": at,
            "updated_at": at,
            "last_activity_at": at,
            "manifest": None,
            "schema_version": None,
            "design_state": "seeded",
            "operational_status": "active",
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    with sqlite3.connect(fixture_db) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for statement in frozen_r1_ddl[:2]: connection.execute(statement)
        for statement in frozen_r2_ddl: connection.execute(statement)
        connection.execute('INSERT INTO "aipcs_registry_meta" VALUES (1,?,?,?,?)', ("aipcs.sqlite.registry", "registry", 2, 1 if mode != "clean" else 0))
        connection.executemany('INSERT INTO "aipcs_registry_migration" VALUES (?,?,?,?,?)', [("registry", 1, "registry-0001-initial", "d40691d8ae8e09b10767b262ac716bc1689c52f4887770d9f43cd84679d291bc", at), ("registry", 2, "registry-0002-durable-intent", "b6190247d4f709728bab59cb4eb5fd149d4b7424472615f377b83f5191e0d8ea", at)])
        connection.execute('INSERT INTO "aipcs_registry_service" VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (service_id, "frozen-principal", "frozen_domain", "release", "frozen registry", at, at, at, None, None, "seeded", "active", 1, None, None, None))
        connection.execute('INSERT INTO "aipcs_registry_mutation" VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', ("frozen-principal", "frozen-key", "a" * 64, service_id, "legacy", "completed", None, None, None, None, result_json, None))
        connection.execute('INSERT INTO "aipcs_registry_audit" VALUES (?,?,?,?,?,?,?)', (1, "seed", "created", service_id, "frozen-principal", "release", at))
        if mode != "clean":
            connection.execute(registry_policy_ddl)
            connection.execute('INSERT INTO "aipcs_registry_storage_policy" VALUES (1,?,?,?)', ("aipcs.sqlite.wal.v1", registry_policy_checksum, "prepared"))
    fixture_db.chmod(0o600)
    if mode == "prepared-wal":
        with sqlite3.connect(fixture_db) as connection: assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    frozen = SQLiteRegistryAdapter(SQLiteLocationPolicy(fixture_root))
    outcome = frozen.migrate()
    assert outcome == MigrationState("registry", 4, 4, "ready"), (mode, outcome)
    assert frozen.inspect_migration() == MigrationState("registry", 4, 4, "ready")
    with sqlite3.connect(fixture_db) as connection:
        assert connection.execute(
            'SELECT "operation_kind","phase","result_json" '
            'FROM "aipcs_registry_claim" WHERE "idempotency_key"=\'frozen-key\''
        ).fetchone() == ("legacy", "completed", result_json)
        assert connection.execute(
            'SELECT "identity_state","domain_name","storage_backend" '
            'FROM "aipcs_registry_identity" WHERE "service_id"=?',
            (service_id,),
        ).fetchone() == ("live", "frozen_domain", None)
        assert connection.execute('SELECT count(*),min("audit_id"),max("audit_id") FROM "aipcs_registry_audit"').fetchone() == (1, 1, 1)
        assert connection.execute('SELECT revision,migration_id,checksum FROM "aipcs_registry_migration" ORDER BY revision').fetchall() == [(1, "registry-0001-initial", "d40691d8ae8e09b10767b262ac716bc1689c52f4887770d9f43cd84679d291bc"), (2, "registry-0002-durable-intent", "b6190247d4f709728bab59cb4eb5fd149d4b7424472615f377b83f5191e0d8ea"), (3, "registry-0003-wal-policy", registry_policy_checksum), (4, "registry-0004-portable-lifecycle", "2647ae605b191187bfee7057959699ea2d993a4f9450ada9c02fa191c7395300")]
        assert connection.execute('SELECT phase FROM "aipcs_registry_storage_policy"').fetchone() == ("ready",)
for fixture_mode in ("clean", "prepared-delete", "prepared-wal"): frozen_registry(fixture_mode)

catalog = SQLiteServiceStoreCatalog(SQLiteLocationPolicy(root))
locator = catalog.allocate(UUID(int=9))
assert catalog.migrate(locator) == MigrationState("service_store", 3, 3, "ready")
assert catalog.inspect_migration(locator) == MigrationState("service_store", 3, 3, "ready")
service_db = root / "service-stores" / f"{locator.namespace}.sqlite"
with sqlite3.connect(service_db) as connection:
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute('SELECT phase FROM "__aipcs_service_store_policy"').fetchone() == ("ready",)

# Independently constructed frozen service-store R1 / prepared DELETE / prepared WAL fixtures.
service_r1_ddl = (
"""CREATE TABLE "__aipcs_service_store_meta" (
    "singleton" INTEGER PRIMARY KEY NOT NULL CHECK ("singleton" = 1),
    "adapter_id" TEXT NOT NULL CHECK ("adapter_id" = 'aipcs.sqlite.service_store'),
    "component" TEXT NOT NULL CHECK ("component" = 'service_store'),
    "namespace" TEXT NOT NULL CHECK (
        length("namespace") = 36 AND substr("namespace", 1, 4) = 'svc_'
        AND "namespace" = lower("namespace")
        AND substr("namespace", 5) NOT GLOB '*[^0-9a-f]*'
    ),
    "applied_revision" INTEGER NOT NULL CHECK ("applied_revision" >= 0),
    "dirty" INTEGER NOT NULL CHECK ("dirty" IN (0, 1))
) STRICT""",
"""CREATE TABLE "__aipcs_service_store_migration" (
    "component" TEXT NOT NULL CHECK ("component" = 'service_store'),
    "revision" INTEGER NOT NULL CHECK ("revision" > 0),
    "migration_id" TEXT NOT NULL CHECK (length("migration_id") BETWEEN 1 AND 96),
    "checksum" TEXT NOT NULL CHECK (
        length("checksum") = 64 AND "checksum" = lower("checksum")
        AND "checksum" NOT GLOB '*[^0-9a-f]*'
    ),
    "applied_at" TEXT NOT NULL CHECK (
        length("applied_at") = 27 AND substr("applied_at", 11, 1) = 'T'
        AND substr("applied_at", 20, 1) = '.' AND substr("applied_at", 27, 1) = 'Z'
    ),
    PRIMARY KEY ("component", "revision"),
    UNIQUE ("component", "migration_id")
) STRICT""")
service_policy_ddl = """CREATE TABLE "__aipcs_service_store_policy" (
    "singleton" INTEGER PRIMARY KEY NOT NULL CHECK ("singleton" = 1),
    "policy_id" TEXT NOT NULL CHECK ("policy_id" = 'aipcs.sqlite.wal.v1'),
    "policy_checksum" TEXT NOT NULL CHECK (
        length("policy_checksum") = 64
        AND "policy_checksum" = lower("policy_checksum")
        AND "policy_checksum" NOT GLOB '*[^0-9a-f]*'
    ),
    "phase" TEXT NOT NULL CHECK ("phase" IN ('prepared', 'ready'))
) STRICT"""
service_r1_checksum = "e56f32848bf4f5b762abe4a164e5869658360408d908da73a6b75d8f52c42f1b"
service_policy_checksum = "d639ef1e1a897e6eaa5109676417f4ab1bc9bbd7a56ce798eee8e2816ce4d8f5"
def frozen_service(mode):
    fixture_root = root / ("service-" + mode); fixture_root.mkdir(mode=0o700)
    namespace = "svc_0000000000000000000000000000000a"
    fixture_db = fixture_root / "service-stores" / (namespace + ".sqlite")
    fixture_db.parent.mkdir(mode=0o700)
    with sqlite3.connect(fixture_db) as connection:
        for statement in service_r1_ddl: connection.execute(statement)
        connection.execute('CREATE TABLE "note" ("id" TEXT PRIMARY KEY, "body" TEXT NOT NULL) STRICT')
        connection.execute('INSERT INTO "note" VALUES (?,?)', ("frozen-note", "byte-preserved"))
        connection.execute('INSERT INTO "__aipcs_service_store_meta" VALUES (1,?,?,?,?,?)', ("aipcs.sqlite.service_store", "service_store", namespace, 1, 1 if mode != "clean" else 0))
        connection.execute('INSERT INTO "__aipcs_service_store_migration" VALUES (?,?,?,?,?)', ("service_store", 1, "service-store-0001-foundation", service_r1_checksum, "2026-01-01T00:00:00.000000Z"))
        if mode != "clean":
            connection.execute(service_policy_ddl)
            connection.execute('INSERT INTO "__aipcs_service_store_policy" VALUES (1,?,?,?)', ("aipcs.sqlite.wal.v1", service_policy_checksum, "prepared"))
    fixture_db.chmod(0o600)
    if mode == "prepared-wal":
        with sqlite3.connect(fixture_db) as connection: assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    frozen = SQLiteServiceStoreCatalog(SQLiteLocationPolicy(fixture_root))
    fixture_locator = frozen.allocate(UUID(hex=namespace[4:]))
    outcome = frozen.migrate(fixture_locator)
    assert outcome == MigrationState("service_store", 3, 3, "ready"), (mode, outcome)
    assert frozen.inspect_migration(fixture_locator) == MigrationState("service_store", 3, 3, "ready")
    with sqlite3.connect(fixture_db) as connection:
        assert connection.execute('SELECT "body" FROM "note"').fetchone() == ("byte-preserved",)
        assert connection.execute('SELECT revision,migration_id,checksum FROM "__aipcs_service_store_migration" ORDER BY revision').fetchall() == [(1, "service-store-0001-foundation", service_r1_checksum), (2, "service-store-0002-wal-policy", service_policy_checksum), (3, "service-store-0003-record-topology", "8181f28aa6e4f8812ae169ea9754eb13fbfe10b574a88a36968564a4595896d7")]
        assert connection.execute('SELECT phase FROM "__aipcs_service_store_policy"').fetchone() == ("ready",)
for fixture_mode in ("clean", "prepared-delete", "prepared-wal"): frozen_service(fixture_mode)
'''
    return (
        program.replace("__FROZEN_R1_DDL__", repr(_FROZEN_R1_DDL))
        .replace("__FROZEN_R2_DDL__", repr(_FROZEN_R2_DDL))
    )


def run_installed_wal_release_smoke(
    python: Path, workspace: Path, name: str, *, environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> None:
    """Run the one bounded installed WAL/process/restart proof per artifact."""

    client = workspace / f"{name}-wal-release-smoke.py"
    client.write_text(_wal_release_smoke_program(), encoding="utf-8")
    cwd = workspace / f"{name}-wal-release-cwd"
    cwd.mkdir(mode=0o700)
    run_stage(
        f"installed {name} WAL release smoke",
        [python, "-I", client, workspace / f"{name}-wal-root"],
        cwd=cwd,
        environment=environment,
        timeout=SMOKE_TIMEOUT_SECONDS,
        redaction_roots=redaction_roots,
    )


def _lifecycle_coordinator_smoke_program() -> str:
    """Private installed lifecycle orchestration proof; public transport stays unchanged."""

    return r'''
from __future__ import annotations

import site
import sqlite3
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import aipcs_mcp.application.data as data_application_module
import aipcs_mcp.data_requests as data_requests_module
import aipcs_mcp.data_results as data_results_module
import aipcs_mcp.data_store as data_store_module
import aipcs_mcp.lifecycle_coordinator as lifecycle_coordinator_module
import aipcs_mcp.mcp_server as mcp_server_module
import aipcs_mcp.records as records_module
import aipcs_mcp.runtime as runtime_module
import aipcs_mcp.storage.sqlite.data_store as sqlite_data_store_module
import aipcs_mcp.storage.sqlite.discovery as discovery_module
import aipcs_mcp.storage.sqlite.domain_schema as domain_schema_module
import aipcs_mcp.storage.sqlite.topology as topology_module
from aipcs_mcp.application.models import PreparedLifecycleClaim, Service
from aipcs_mcp.lifecycle import EvolveCommand, MaterialiseCommand
from aipcs_mcp.lifecycle_coordinator import LifecycleCoordinator
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.relational import classify_transition, compile_manifest
from aipcs_mcp.storage import MigrationState
from aipcs_mcp.storage.sqlite import (
    SQLiteLocationPolicy,
    SQLiteRegistryAdapter,
    SQLiteServiceStoreCatalog,
)
from aipcs_mcp.storage.sqlite.domain_schema import SQLiteDomainSchemaStore

root = Path(sys.argv[1])
mode = sys.argv[2]
assert mode in {"initial", "restart"}
sites = tuple(Path(value).resolve() for value in site.getsitepackages())
for module in (
    data_application_module,
    data_requests_module,
    data_results_module,
    data_store_module,
    lifecycle_coordinator_module,
    mcp_server_module,
    records_module,
    runtime_module,
    sqlite_data_store_module,
    discovery_module,
    domain_schema_module,
    topology_module,
):
    origin = Path(module.__file__).resolve()
    assert any(origin.is_relative_to(value) for value in sites), origin

assert tuple(tool.name for tool in mcp_server_module._tools(True, True, True)) == (
    "aipcs_server_info",
    "aipcs_service_seed",
    "aipcs_service_list",
    "aipcs_service_inspect",
    "aipcs_service_design",
    "aipcs_service_materialise",
    "aipcs_service_evolve",
    "aipcs_record_create",
    "aipcs_record_get",
    "aipcs_record_list",
    "aipcs_record_search",
    "aipcs_record_update",
    "aipcs_record_delete",
    "aipcs_record_history",
    "aipcs_bootstrap",
    "aipcs_service_summary",
    "aipcs_branch_create",
    "aipcs_branch_list",
    "aipcs_branch_update",
    "aipcs_branch_assign_records",
    "aipcs_maintenance_scan",
)

managed = [
    {"name": "id", "type": "uuid", "required": True, "primary_key": True},
    {"name": "owner_id", "type": "string", "required": True},
    {"name": "created_at", "type": "datetime", "required": True},
    {"name": "updated_at", "type": "datetime", "required": True},
    {"name": "created_via", "type": "string", "required": True},
    {"name": "record_version", "type": "integer", "required": True},
]
source_document = {
    "manifest_version": 2,
    "schema_version": 1,
    "entities": [{
        "name": "memory",
        "attributes": managed + [{"name": "title", "type": "string", "required": True}],
    }],
    "relationships": [],
    "indices": [],
    "query_patterns": [],
    "discovery_facets": [],
    "migration_history": [],
}
target_document = deepcopy(source_document)
target_document["schema_version"] = 2
target_document["migration_history"] = [{
    "from_schema_version": 1,
    "to_schema_version": 2,
    "operations": ["add memory summary"],
}]
target_document["entities"][0]["attributes"].append(
    {"name": "summary", "type": "string"}
)
v1 = ManifestV2.model_validate(source_document)
v2 = ManifestV2.model_validate(target_document)

principal = "installed-coordinator"
created_via = "release"
first_id = UUID(int=71)
adopt_id = UUID(int=72)
restart_id = UUID(int=73)
recovery_id = UUID(int=74)
at = datetime(2026, 1, 1, tzinfo=UTC)

class Clock:
    def __init__(self):
        self.tick = 0

    def now(self):
        self.tick += 1
        day = 2 if mode == "initial" else 3
        return datetime(2026, 1, day, 0, 0, self.tick, tzinfo=UTC)

policy = SQLiteLocationPolicy(root)
registry = SQLiteRegistryAdapter(policy)
catalog = SQLiteServiceStoreCatalog(policy)
domain = SQLiteDomainSchemaStore(policy)
coordinator = LifecycleCoordinator(registry.open_uow, Clock(), catalog, domain)

def seeded(service_id):
    return Service(
        service_id=service_id,
        principal_id=principal,
        domain_name=f"domain_{service_id.int}",
        domain_class="release",
        intent_description="installed coordinator smoke",
        created_at=at,
        updated_at=at,
        last_activity_at=at,
        manifest=v1,
        schema_version=1,
        design_state="seeded",
        service_revision=2,
    )

def materialise(service_id, key):
    return MaterialiseCommand(
        principal_id=principal,
        created_via=created_via,
        service_id=service_id,
        expected_service_revision=2,
        expected_schema_version=1,
        idempotency_key=key,
    )

def evolve(service_id, key):
    return EvolveCommand(
        principal_id=principal,
        created_via=created_via,
        service_id=service_id,
        expected_service_revision=3,
        expected_schema_version=1,
        idempotency_key=key,
        target_manifest=v2,
    )

def prepare(command):
    uow = registry.open_uow()
    claim = uow.mutations.resolve_or_admit(command)
    assert type(claim) is PreparedLifecycleClaim
    uow.commit()
    uow.close()
    return claim.intent

def service(service_id):
    uow = registry.open_uow()
    value = uow.services.get(principal, service_id)
    uow.rollback()
    uow.close()
    assert type(value) is Service
    return value

def service_database(service_id):
    return root / "service-stores" / f"svc_{service_id.hex}.sqlite"

if mode == "initial":
    assert registry.migrate() == MigrationState("registry", 4, 4, "ready")
    uow = registry.open_uow()
    for item in (seeded(first_id), seeded(adopt_id), seeded(restart_id), seeded(recovery_id)):
        uow.services.add(item)
    uow.commit()
    uow.close()

    first_materialise = materialise(first_id, "first-materialise")
    first_result = coordinator.execute(first_materialise)
    assert first_result.category == "completed"
    assert first_result.service is not None and first_result.service.service_revision == 3
    replay = coordinator.execute(first_materialise)
    assert replay.category == "completed"
    assert replay.service == first_result.service
    first_evolve = coordinator.execute(evolve(first_id, "first-evolve"))
    assert first_evolve.category == "completed"
    assert first_evolve.service is not None and first_evolve.service.schema_version == 2

    # Seed an exact physical materialise target while its registry intent remains prepared.
    prepared_adopt = prepare(materialise(adopt_id, "adopt-materialise"))
    adopt_locator = catalog.allocate(adopt_id)
    assert catalog.migrate(adopt_locator) == MigrationState("service_store", 3, 3, "ready")
    assert domain.materialise(adopt_locator, compile_manifest(prepared_adopt.target_manifest)).status == "ready"

    # Independently finish physical adjacent evolution before its registry completion.
    restart_materialise = coordinator.execute(materialise(restart_id, "restart-materialise"))
    assert restart_materialise.category == "completed"
    prepared_evolve = prepare(evolve(restart_id, "restart-evolve"))
    restart_locator = catalog.allocate(restart_id)
    assert domain.evolve(
        restart_locator, classify_transition(v1, prepared_evolve.target_manifest)
    ).status == "ready"

    # A non-empty, otherwise ordinary SQLite database is an incompatible service foundation.
    # It is constructed inside this private release workspace without calling a private repair seam.
    prepare(materialise(recovery_id, "recovery-materialise"))
    recovery_database = service_database(recovery_id)
    recovery_database.parent.mkdir(mode=0o700, exist_ok=True)
    with sqlite3.connect(recovery_database) as connection:
        connection.execute('CREATE TABLE "unexpected" ("id" TEXT PRIMARY KEY) STRICT')
    recovery_database.chmod(0o600)
else:
    adopted = coordinator.execute(materialise(adopt_id, "adopt-materialise"))
    assert adopted.category == "completed"
    assert adopted.service is not None and adopted.service.service_revision == 3
    restarted = coordinator.execute(evolve(restart_id, "restart-evolve"))
    assert restarted.category == "completed"
    assert restarted.service is not None and restarted.service.schema_version == 2
    assert service(first_id).service_revision == 4
    assert service(adopt_id).service_revision == 3
    assert service(restart_id).service_revision == 4
    recovered = coordinator.execute(materialise(recovery_id, "recovery-materialise"))
    assert recovered.category.value == "recovery_required"
    recovery_replay = coordinator.execute(materialise(recovery_id, "recovery-materialise"))
    assert recovery_replay.category.value == "recovery_required"
    recovery_service = service(recovery_id)
    assert recovery_service.service_revision == 2 and recovery_service.design_state == "seeded"

    assert registry.inspect_migration() == MigrationState("registry", 4, 4, "ready")
    for service_id in (first_id, adopt_id, restart_id):
        locator = catalog.allocate(service_id)
        assert catalog.inspect_migration(locator) == MigrationState(
            "service_store", 3, 3, "ready"
        )
        with sqlite3.connect(service_database(service_id)) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    recovery_locator = catalog.allocate(recovery_id)
    assert catalog.inspect_migration(recovery_locator) == MigrationState(
        "service_store", 0, 3, "incompatible"
    )
    with sqlite3.connect(service_database(recovery_id)) as connection:
        assert connection.execute(
            'SELECT name FROM sqlite_schema WHERE type=\'table\' AND name=\'unexpected\''
        ).fetchone() == ("unexpected",)
    with sqlite3.connect(root / "registry.sqlite") as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        terminal = connection.execute(
            'SELECT "service_id","operation_kind","phase","intent_json",'
            '"result_json","recovery_category" FROM "aipcs_registry_claim" '
            'WHERE "principal_id"=? ORDER BY "service_id","idempotency_key"',
            (principal,),
        ).fetchall()
        assert [row[:3] for row in terminal] == [
            (str(first_id), "evolve", "completed"),
            (str(first_id), "materialise", "completed"),
            (str(adopt_id), "materialise", "completed"),
            (str(restart_id), "evolve", "completed"),
            (str(restart_id), "materialise", "completed"),
            (str(recovery_id), "materialise", "recovery_required"),
        ]
        assert all(row[3] is not None for row in terminal)
        assert all(row[4] is not None and row[5] is None for row in terminal[:-1])
        assert terminal[-1][4:] == (None, "recovery_required")
        identities = connection.execute(
            'SELECT "service_id","identity_state","domain_name","storage_namespace",'
            '"claim_idempotency_key","lifecycle_at" FROM "aipcs_registry_identity" '
            'WHERE "principal_id"=? ORDER BY "service_id"',
            (principal,),
        ).fetchall()
        assert [(row[0], row[1], row[2]) for row in identities] == [
            (str(first_id), "live", f"domain_{first_id.int}"),
            (str(adopt_id), "live", f"domain_{adopt_id.int}"),
            (str(restart_id), "live", f"domain_{restart_id.int}"),
            (str(recovery_id), "live", f"domain_{recovery_id.int}"),
        ]
        assert all(
            row[3:] == (f"svc_{UUID(row[0]).hex}", None, None) for row in identities
        )
        assert connection.execute(
            'SELECT count(*) FROM "aipcs_registry_receipt" WHERE "principal_id"=?',
            (principal,),
        ).fetchone() == (0,)
        audits = connection.execute(
            'SELECT "service_id","action","outcome",count(*) FROM "aipcs_registry_audit" '
            'WHERE "principal_id"=? GROUP BY "service_id","action","outcome" '
            'ORDER BY "service_id","action"',
            (principal,),
        ).fetchall()
        assert audits == [
            (str(first_id), "evolve", "completed", 1),
            (str(first_id), "materialise", "completed", 1),
            (str(adopt_id), "materialise", "completed", 1),
            (str(restart_id), "evolve", "completed", 1),
            (str(restart_id), "materialise", "completed", 1),
            (str(recovery_id), "materialise", "recovery_required", 1),
        ]
'''


def run_installed_lifecycle_coordinator_smoke(
    python: Path,
    workspace: Path,
    name: str,
    *,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> None:
    """Run the private V1-08D coordinator recovery proof for one installed artifact."""

    client = workspace / f"{name}-lifecycle-coordinator-smoke.py"
    client.write_text(_lifecycle_coordinator_smoke_program(), encoding="utf-8")
    cwd = workspace / f"{name}-lifecycle-coordinator-cwd"
    cwd.mkdir(mode=0o700)
    root = workspace / f"{name}-lifecycle-coordinator-root"
    for mode in ("initial", "restart"):
        run_stage(
            f"installed {name} lifecycle coordinator {mode}",
            [python, "-I", client, root, mode],
            cwd=cwd,
            environment=environment,
            timeout=SMOKE_TIMEOUT_SECONDS,
            redaction_roots=redaction_roots,
        )


def _portable_lifecycle_smoke_program() -> str:
    """Return the installed private-stream transfer, tamper, and restart proof."""

    return r"""
import os
import site
import sys
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

package_roots = tuple(Path(value).resolve() for value in site.getsitepackages())
for module in (
    "aipcs_mcp.application.models",
    "aipcs_mcp.application.operational_lifecycle",
    "aipcs_mcp.application.portable",
    "aipcs_mcp.application.portable_store",
    "aipcs_mcp.application.registry_authority",
    "aipcs_mcp.manifest_v2",
    "aipcs_mcp.portable_coordinator",
    "aipcs_mcp.records",
    "aipcs_mcp.storage.postgresql",
    "aipcs_mcp.storage.postgresql.registry_migrations",
    "aipcs_mcp.storage.sqlite",
    "aipcs_mcp.storage.sqlite.portable",
):
    loaded = __import__(module, fromlist=["*"])
    origin = Path(loaded.__file__).resolve()
    assert any(origin.is_relative_to(root) for root in package_roots)

from aipcs_mcp.application.models import MaterialisationStorage, Service
from aipcs_mcp.application.operational_lifecycle import (
    ArchiveCommand,
    ResumeCommand,
    SuspendCommand,
)
from aipcs_mcp.application.portable import ImportBundleRequest, PortableResultCategory
from aipcs_mcp.application.portable_store import (
    PortableStoreMember,
    WriteAdmissionFence,
    portable_member_sort_key,
)
from aipcs_mcp.application.registry_authority import (
    ExportCommand,
    PurgeAuthority,
    PurgeAuthorityKind,
    PurgeCommand,
    StorageBackend,
)
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.portable_coordinator import PortableCoordinator
from aipcs_mcp.records import FrozenJsonObject, RecordHistoryEvent, RecordValue
from aipcs_mcp.storage.postgresql import (
    PostgreSQLConnectionPolicy,
    PostgreSQLDsn,
    PostgreSQLPortableServiceStore,
    PostgreSQLRegistryAdapter,
)
from aipcs_mcp.storage.postgresql.registry_migrations import SCHEMA
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter
from aipcs_mcp.storage.sqlite.portable import SQLitePortableServiceStore

AT = datetime(2026, 7, 24, 12, tzinfo=UTC)
LATER = datetime(2026, 7, 24, 12, 1, tzinfo=UTC)
FENCE = WriteAdmissionFence(7, "closed")
root, scenario, source_name, destination_name, mode = sys.argv[1:]
root = Path(root).resolve()
root.mkdir(mode=0o700, parents=True, exist_ok=True)
assert mode in {"initial", "restart"}


class Clock:
    def now(self):
        return LATER


class UUIDFactory:
    def __init__(self, label):
        self.label = label
        self.count = 0

    def __call__(self):
        self.count += 1
        return uuid5(NAMESPACE_URL, f"{self.label}:{self.count}")


def server_fields():
    return [
        {"name": "id", "type": "uuid", "required": True, "primary_key": True},
        {"name": "owner_id", "type": "string", "required": True},
        {"name": "created_at", "type": "datetime", "required": True},
        {"name": "updated_at", "type": "datetime", "required": True},
        {"name": "created_via", "type": "string", "required": True},
        {"name": "record_version", "type": "integer", "required": True},
    ]


manifest = ManifestV2.model_validate(
    {
        "manifest_version": 2,
        "schema_version": 1,
        "entities": [
            {
                "name": "memory",
                "attributes": [
                    *server_fields(),
                    {"name": "body", "type": "string", "required": True},
                    {"name": "rank", "type": "integer", "required": True},
                ],
            }
        ],
        "relationships": [],
        "indices": [],
        "query_patterns": ["Find installed portable memory."],
        "discovery_facets": [{"entity": "memory", "field": "body"}],
        "retrieval_guidance": "Use exact installed fixture values.",
        "migration_history": [],
    }
)
service_id = uuid5(NAMESPACE_URL, f"{scenario}:service")
record_id = uuid5(service_id, "record")
fields = FrozenJsonObject.from_mapping({"body": "remember", "rank": 7})
members = tuple(
    sorted(
        (
            PortableStoreMember(
                "record",
                RecordValue(
                    "memory",
                    record_id,
                    1,
                    AT,
                    AT,
                    "installed-fixture",
                    fields,
                ),
            ),
            PortableStoreMember(
                "history",
                RecordHistoryEvent(
                    "memory", record_id, 1, "create", AT, None, fields
                ),
            ),
        ),
        key=portable_member_sort_key,
    )
)


def installation(backend_name, location, principal, label):
    backend = StorageBackend(backend_name)
    if backend is StorageBackend.SQLITE:
        policy = SQLiteLocationPolicy(location)
        registry = SQLiteRegistryAdapter(policy)
        store = SQLitePortableServiceStore(policy, clock=Clock().now)
    else:
        dsn = PostgreSQLDsn(os.environ["AIPCS_RELEASE_POSTGRES_DSN"])
        registry = PostgreSQLRegistryAdapter(
            dsn,
            PostgreSQLConnectionPolicy(SCHEMA, 5, 1_000, 10_000),
        )
        store = PostgreSQLPortableServiceStore(dsn, clock=Clock().now)
    assert registry.migrate().status == "ready"
    coordinator = PortableCoordinator(
        registry.open_uow,
        Clock(),
        UUIDFactory(label),
        backend,
        store,
    )
    return backend, principal, registry.open_uow, store, coordinator


source = installation(
    source_name,
    root / "source",
    f"{scenario}-source",
    f"{scenario}:source",
)
destination = installation(
    destination_name,
    root / "destination",
    f"{scenario}-destination",
    f"{scenario}:destination",
)
print("PORTABLE_MIGRATED", flush=True)


def bound_service(installation):
    backend, principal, _uows, _store, _coordinator = installation
    return Service(
        service_id,
        principal,
        f"installed_{service_id.hex[:20]}",
        "project",
        "Installed private-stream portability proof",
        AT,
        AT,
        AT,
        manifest,
        1,
        "materialised",
        "suspended",
        5,
        AT,
        MaterialisationStorage(backend.value, f"svc_{service_id.hex}"),
    )


def get_service(installation):
    _backend, principal, uows, _store, _coordinator = installation
    uow = uows()
    try:
        observed = uow.services.get(principal, service_id)
        uow.rollback()
        return observed
    finally:
        uow.close()


source_service = bound_service(source)
destination_service = bound_service(destination)
source_backend, source_principal, source_uows, source_store, source_coordinator = source
(
    destination_backend,
    destination_principal,
    _destination_uows,
    destination_store,
    destination_coordinator,
) = destination

if mode == "initial":
    source_uow = source_uows()
    try:
        source_uow.services.add(source_service)
        source_uow.commit()
    finally:
        source_uow.close()
    assert source_store.stage(source_service, members, FENCE).status == "staged"
    print("PORTABLE_SOURCE_STAGED", flush=True)
    output = BytesIO()
    exported = source_coordinator.export(
        ExportCommand(
            source_principal,
            "installed-verifier",
            service_id,
            5,
            "installed-export",
        ),
        output,
    )
    assert exported.category is PortableResultCategory.COMPLETED
    assert exported.artifact_written
    print("PORTABLE_EXPORTED", flush=True)
    artifact = output.getvalue()
    forbidden = (
        source_principal,
        destination_principal,
        str(root),
        "svc_",
        "installed-export",
        "installed-import",
        os.environ.get("AIPCS_RELEASE_POSTGRES_DSN", ""),
    )
    assert all(not value or value.encode() not in artifact for value in forbidden)
    assert f'"source_backend":"{source_backend.value}"'.encode() in artifact
    tampered = artifact.replace(
        b'"body":"remember"',
        b'"body":"remembfr"',
        1,
    )
    assert tampered != artifact
    rejected = destination_coordinator.import_bundle(
        ImportBundleRequest(
            destination_principal,
            "installed-verifier",
            destination_backend,
            "installed-tamper",
        ),
        BytesIO(tampered),
    )
    assert rejected.category is PortableResultCategory.MALFORMED_INPUT
    assert get_service(destination) is None
    assert destination_store.is_absent(
        replace(destination_service, operational_status="archived")
    )
    print("PORTABLE_TAMPER_REJECTED", flush=True)
    imported = destination_coordinator.import_bundle(
        ImportBundleRequest(
            destination_principal,
            "installed-verifier",
            destination_backend,
            "installed-import",
        ),
        BytesIO(artifact),
    )
    assert imported.category is PortableResultCategory.COMPLETED
    assert imported.service == destination_service
    assert destination_store.observe(destination_service, FENCE, members)
    print("PORTABLE_IMPORTED", flush=True)
    transitions = (
        ResumeCommand(
            destination_principal,
            "installed-verifier",
            service_id,
            5,
            "installed-resume",
        ),
        SuspendCommand(
            destination_principal,
            "installed-verifier",
            service_id,
            6,
            "installed-suspend",
        ),
        ArchiveCommand(
            destination_principal,
            "installed-verifier",
            service_id,
            7,
            "installed-archive",
        ),
    )
    for label, command in zip(
        ("RESUME", "SUSPEND", "ARCHIVE"),
        transitions,
        strict=True,
    ):
        transition = destination_coordinator.transition(command)
        print(
            f"PORTABLE_{label}_{transition.category.value.upper()}",
            flush=True,
        )
        assert transition.category is PortableResultCategory.COMPLETED
    archived = get_service(destination)
    assert archived is not None
    assert archived.operational_status == "archived"
    assert archived.service_revision == 8
    print("PORTABLE_ARCHIVED", flush=True)
    purged = destination_coordinator.purge(
        PurgeCommand(
            destination_principal,
            "installed-verifier",
            service_id,
            8,
            "installed-purge",
            PurgeAuthority(PurgeAuthorityKind.EXPLICIT_OVERRIDE),
        )
    )
    assert purged.category is PortableResultCategory.COMPLETED
    assert purged.tombstone is not None
    assert purged.tombstone.receipt_id is None
    assert get_service(destination) is None
    assert destination_store.is_absent(archived)
    print("PORTABLE_PURGED", flush=True)
else:
    assert get_service(source) == source_service
    assert source_store.observe(source_service, FENCE, members)
    assert get_service(destination) is None
    replay = destination_coordinator.purge(
        PurgeCommand(
            destination_principal,
            "installed-verifier",
            service_id,
            8,
            "installed-purge",
            PurgeAuthority(PurgeAuthorityKind.EXPLICIT_OVERRIDE),
        )
    )
    assert replay.category is PortableResultCategory.COMPLETED
    assert replay.tombstone is not None
    assert replay.tombstone.receipt_id is None
    assert destination_store.is_absent(
        replace(destination_service, operational_status="archived")
    )
    print("PORTABLE_RESTARTED", flush=True)
"""


def run_installed_portable_lifecycle_smoke(
    python: Path,
    workspace: Path,
    name: str,
    *,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> None:
    """Run installed SQLite private-stream transfer and restart proof."""

    client = workspace / f"{name}-portable-lifecycle-smoke.py"
    client.write_text(_portable_lifecycle_smoke_program(), encoding="utf-8")
    cwd = workspace / f"{name}-portable-lifecycle-cwd"
    cwd.mkdir(mode=0o700)
    root = workspace / f"{name}-portable-lifecycle-root"
    scenario = f"installed-{name}-sqlite-sqlite"
    for mode in ("initial", "restart"):
        run_stage(
            f"installed {name} portable lifecycle {mode}",
            [python, "-I", client, root, scenario, "sqlite", "sqlite", mode],
            cwd=cwd,
            environment=environment,
            timeout=SMOKE_TIMEOUT_SECONDS,
            redaction_roots=redaction_roots,
        )


def run_postgresql_portable_lifecycle_smoke(
    python: Path,
    workspace: Path,
    name: str,
    target: PostgreSQLReleaseTarget,
    *,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> None:
    """Run both installed cross-backend directions with restart on owned PostgreSQL."""

    client = workspace / f"postgres-{name}-portable-lifecycle-smoke.py"
    client.write_text(_portable_lifecycle_smoke_program(), encoding="utf-8")
    cwd = workspace / f"postgres-{name}-portable-lifecycle-cwd"
    cwd.mkdir(mode=0o700)
    child_environment = dict(environment)
    child_environment[POSTGRES_RELEASE_DSN_ENV] = target.dsn
    for source, destination in (
        ("sqlite", "postgresql"),
        ("postgresql", "sqlite"),
    ):
        root = workspace / f"postgres-{name}-{source}-{destination}-portable-root"
        scenario = f"installed-{name}-{source}-{destination}"
        for mode in ("initial", "restart"):
            try:
                run_stage(
                    f"installed PostgreSQL {name} {source} to {destination} {mode}",
                    [
                        python,
                        "-I",
                        client,
                        root,
                        scenario,
                        source,
                        destination,
                        mode,
                    ],
                    cwd=cwd,
                    environment=child_environment,
                    timeout=POSTGRES_SMOKE_TIMEOUT_SECONDS,
                    redaction_roots=redaction_roots,
                )
            except ReleaseVerificationError as error:
                checkpoints = re.findall(r"PORTABLE_[A-Z_]+", str(error))
                checkpoint = checkpoints[-1] if checkpoints else "PORTABLE_START"
                raise ReleaseVerificationError(
                    "Installed PostgreSQL portable lifecycle verification failed "
                    f"for {source}-to-{destination} {mode} after {checkpoint}."
                ) from None


def run_installed_public_lifecycle_smoke(
    python: Path,
    workspace: Path,
    name: str,
    *,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> None:
    """Exercise one installed artifact through public lifecycle, replay, restart, and isolation."""

    executable = python.parent / "aipcs"
    if not executable.is_file():
        raise ReleaseVerificationError(f"installed {name} did not provide the aipcs console command.")
    sqlite_root = workspace / f"{name}-public-lifecycle-root"
    sqlite_root.mkdir(mode=0o700)
    _run_smoke_client(
        python,
        workspace,
        executable,
        sqlite_root,
        f"release-principal-{name}-a",
        "principal_a",
        environment=environment,
        redaction_roots=redaction_roots,
    )
    _run_smoke_client(
        python,
        workspace,
        executable,
        sqlite_root,
        f"release-principal-{name}-a",
        "principal_a",
        environment=environment,
        redaction_roots=redaction_roots,
    )
    _run_smoke_client(
        python,
        workspace,
        executable,
        sqlite_root,
        f"release-principal-{name}-b",
        "principal_b",
        environment=environment,
        redaction_roots=redaction_roots,
    )
    _run_smoke_client(
        python,
        workspace,
        executable,
        sqlite_root,
        f"release-principal-{name}-a",
        "recovery",
        environment=environment,
        redaction_roots=redaction_roots,
    )


def _admin_cli_smoke_program() -> str:
    """Return an installed-only SQLite administration CLI workflow."""

    return r"""
import json
import os
import subprocess
import sys
from pathlib import Path

from aipcs_mcp.application.models import ApplicationContext, SeedCommand
from aipcs_mcp.application.services import ServiceApplication
from aipcs_mcp.runtime import SystemUtcClock, Uuid4ServiceIds
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteRegistryAdapter

executable = Path(sys.argv[1])
workspace = Path(sys.argv[2])
principal = "installed-admin-principal"
source_root = workspace / "source"
destination_root = workspace / "destination"
missing_root = workspace / "missing"
bundle = workspace / "bundle.jsonl"
archive_bundle = workspace / "archive.jsonl"


def invoke(arguments, expected=0):
    completed = subprocess.run(
        [str(executable), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=30,
    )
    assert completed.returncode == expected, (completed.returncode, completed.stdout, completed.stderr)
    stream = completed.stdout if expected == 0 else completed.stderr
    return json.loads(stream)


def config(root):
    return [
        "--profile", "sqlite",
        "--principal-id", principal,
        "--sqlite-data-root", str(root),
    ]


def ready(root):
    adapter = SQLiteRegistryAdapter(SQLiteLocationPolicy.from_resolved(root, "cli"))
    assert adapter.migrate().status == "ready"
    return adapter


help_result = subprocess.run(
    [str(executable), "--help"],
    check=False,
    capture_output=True,
    text=True,
    env=dict(os.environ),
    timeout=30,
)
assert help_result.returncode == 0
for command in ("status", "doctor", "storage", "service", "export", "import", "maintenance"):
    assert command in help_result.stdout

missing = invoke(["status", *config(missing_root)])
assert missing["result"]["registry"]["status"] == "uninitialised"
assert not missing_root.exists()

source = ready(source_root)
application = ServiceApplication(source.open_uow, SystemUtcClock(), Uuid4ServiceIds())
service = application.seed(
    ApplicationContext(principal, "cli"),
    SeedCommand("release_notes", "knowledge", "Installed admin smoke.", "seed-admin"),
)
service_id = str(service.service_id)

assert invoke(["status", *config(source_root)])["result"]["overall"] == "ready"
assert invoke(["doctor", "--service", service_id, *config(source_root)])["result"]["overall"] == "warning"
assert len(invoke(["service", "list", *config(source_root)])["result"]["services"]) == 1
assert invoke(["service", "inspect", service_id, *config(source_root)])["result"]["service_id"] == service_id
assert invoke(["storage", "status", "--service", service_id, *config(source_root)])["result"]["status"] == "uninitialised"

suspend_id = "11111111-1111-4111-8111-111111111111"
suspended = invoke([
    "service", "suspend", service_id,
    "--expected-revision", "1",
    "--operation-id", suspend_id,
    *config(source_root),
])
assert suspended["result"]["service"]["operational_status"] == "suspended"

export_id = "22222222-2222-4222-8222-222222222222"
exported = invoke([
    "export", service_id,
    "--output", str(bundle),
    "--expected-revision", "2",
    "--operation-id", export_id,
    *config(source_root),
])
assert exported["result"]["summary"]["source_backend"] == "sqlite"
assert bundle.is_file()
assert bundle.stat().st_mode & 0o777 == 0o600
already = invoke([
    "export", service_id,
    "--output", str(bundle),
    "--expected-revision", "2",
    "--operation-id", export_id,
    *config(source_root),
], expected=3)
assert already["error"]["code"] == "already_exists"
assert str(bundle) not in json.dumps(already)

dry = invoke(["import", "--input", str(bundle), "--dry-run", *config(destination_root)])
assert dry["result"]["service"]["service_id"] == service_id
assert not destination_root.exists()
ready(destination_root)
import_id = "33333333-3333-4333-8333-333333333333"
imported = invoke([
    "import", "--input", str(bundle),
    "--operation-id", import_id,
    "--yes",
    *config(destination_root),
])
assert imported["result"]["service"]["service_id"] == service_id
assert invoke([
    "import", "--input", str(bundle),
    "--operation-id", import_id,
    "--yes",
    *config(destination_root),
])["result"] == imported["result"]

tampered = workspace / "tampered.jsonl"
payload = bytearray(bundle.read_bytes())
payload[len(payload) // 2] ^= 1
tampered.write_bytes(payload)
tamper_root = workspace / "tamper-destination"
tamper_adapter = ready(tamper_root)
invalid = invoke([
    "import", "--input", str(tampered),
    "--operation-id", "44444444-4444-4444-8444-444444444444",
    "--yes",
    *config(tamper_root),
], expected=2)
assert invalid["error"]["code"] == "validation_failed"
uow = tamper_adapter.open_uow()
assert uow.services.count(principal) == 0
uow.close()

archived = invoke([
    "service", "archive", service_id,
    "--expected-revision", "2",
    "--operation-id", "55555555-5555-4555-8555-555555555555",
    "--yes",
    *config(source_root),
])
assert archived["result"]["service"]["operational_status"] == "archived"
archive_export = invoke([
    "export", service_id,
    "--output", str(archive_bundle),
    "--expected-revision", "3",
    "--operation-id", "66666666-6666-4666-8666-666666666666",
    *config(source_root),
])
receipt_id = archive_export["result"]["receipt"]["receipt_id"]
purge = [
    "service", "purge", service_id,
    "--expected-revision", "3",
    "--operation-id", "77777777-7777-4777-8777-777777777777",
    "--receipt", receipt_id,
    "--confirm-service-id", service_id,
    "--yes",
    *config(source_root),
]
purged = invoke(purge)
assert purged["result"]["tombstone"]["service_id"] == service_id
assert purged["result"]["tombstone"]["authority"] == "verified_receipt"
assert invoke(purge)["result"] == purged["result"]
assert invoke(["service", "inspect", service_id, *config(source_root)], expected=3)["error"]["code"] == "not_found"

print("ADMIN_CLI_OK", flush=True)
"""


def run_installed_admin_cli_smoke(
    python: Path,
    workspace: Path,
    name: str,
    *,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> None:
    """Exercise the installed V1-11 command surface outside the checkout."""

    executable = python.parent / "aipcs"
    if not executable.is_file():
        raise ReleaseVerificationError(
            f"installed {name} did not provide the aipcs console command."
        )
    client = workspace / f"{name}-admin-cli-smoke.py"
    client.write_text(_admin_cli_smoke_program(), encoding="utf-8")
    root = workspace / f"{name}-admin-cli"
    root.mkdir(mode=0o700)
    run_stage(
        f"installed {name} administration CLI",
        [python, "-I", client, executable, root],
        cwd=root,
        environment=environment,
        timeout=SMOKE_TIMEOUT_SECONDS,
        redaction_roots=redaction_roots,
    )


def run_sdist_startup_smoke(
    python: Path,
    workspace: Path,
    *,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> None:
    """Exercise installed sdist stateless startup with the external client."""

    executable = python.parent / "aipcs"
    if not executable.is_file():
        raise ReleaseVerificationError(
            "installed source distribution did not provide the aipcs console command."
        )
    root = workspace / "stateless-root"
    root.mkdir(mode=0o700)
    _run_smoke_client(
        python,
        workspace,
        executable,
        root,
        "release-principal-s",
        "stateless",
        environment=environment,
        redaction_roots=redaction_roots,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _commit(root: Path, *, environment: Mapping[str, str]) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        env=dict(environment),
        check=False,
        capture_output=True,
        text=True,
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    if result.returncode:
        raise ReleaseVerificationError("could not determine the source commit.")
    return result.stdout.strip()


def _source_state(root: Path, *, environment: Mapping[str, str]) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        env=dict(environment),
        check=False,
        capture_output=True,
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    if result.returncode:
        raise ReleaseVerificationError("could not determine the source-tree state.")
    return "dirty" if result.stdout else "clean"


def require_exact_tip(root: Path, *, environment: Mapping[str, str]) -> str:
    """Require the candidate bytes to be exactly the clean checked-out HEAD commit."""

    if _source_state(root, environment=environment) != "clean":
        raise ReleaseVerificationError(
            "exact-tip verification requires a clean source checkout."
        )
    return _commit(root, environment=environment)


def require_same_exact_tip(
    root: Path,
    expected_commit: str,
    *,
    environment: Mapping[str, str],
) -> None:
    """Reject a clean-but-different commit or any byte change after binding."""

    if require_exact_tip(root, environment=environment) != expected_commit:
        raise ReleaseVerificationError(
            "exact-tip verification source changed after candidate binding."
        )


def intended_source_sha256(root: Path, paths: Iterable[Path]) -> str:
    """Hash the canonical path, mode, size, and bytes of the intended source set."""

    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        data = path.read_bytes()
        fields = (
            relative,
            f"{stat.S_IMODE(path.stat().st_mode):o}".encode(),
            str(len(data)).encode(),
        )
        for field in fields:
            digest.update(str(len(field)).encode())
            digest.update(b":")
            digest.update(field)
            digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def _summary(
    commit: str,
    source_state: str,
    source_digest: str,
    source: ArtifactSet,
    copy: ArtifactSet,
) -> str:
    subject = (
        f"commit {commit}"
        if source_state == "clean"
        else f"dirty source tree based on commit {commit}"
    )
    entries = (
        ("source-build", source.wheel),
        ("source-build", source.sdist),
        ("clean-copy-build", copy.wheel),
        ("clean-copy-build", copy.sdist),
    )
    return "\n".join(
        [
            f"release verification passed for {subject}",
            f"source_state={source_state} intended_source_sha256={source_digest}",
            *[f"{label} {path.name} sha256={_sha256(path)}" for label, path in entries],
        ]
    )


def cleanup_workspace(workspace: Path, *, failed: bool, keep_failed_workdir: bool) -> bool:
    """Remove the private workspace unless the explicit failure-only escape hatch applies."""

    if failed and keep_failed_workdir:
        return False
    try:
        shutil.rmtree(workspace)
    except OSError:
        if failed:
            return False
        raise ReleaseVerificationError("release workspace cleanup failed.") from None
    return True


def create_workspace() -> Path:
    """Create one canonical private path suitable for the explicit SQLite policy."""

    created = Path(tempfile.mkdtemp(prefix="aipcs-release-"))
    try:
        return created.resolve(strict=True)
    except OSError:
        with suppress(OSError):
            shutil.rmtree(created)
        raise ReleaseVerificationError("release workspace creation failed.") from None


def verify_postgresql_integration(
    root: Path = ROOT,
    *,
    postgres_major: str = "16",
    keep_failed_workdir: bool = False,
) -> None:
    """Verify one installed PostgreSQL candidate against only a fresh pinned container.

    This is deliberately separate from the normal SQLite release rehearsal.
    It neither accepts a DSN nor searches for a database, Compose project,
    configuration file, dotenv file, image tag, or existing container.
    """

    root = root.resolve()
    environment = postgresql_release_environment()
    uv = require_local_preconditions(root)
    require_clean_checkout_artifacts(root)
    workspace = create_workspace()
    failed = True
    try:
        redaction_roots = (root, workspace)
        source_environment = stage_environment(environment, workspace / "postgres-source-uv")
        snapshot = make_clean_copy(root, workspace / "postgres-source-snapshot", environment=environment)
        run_source_gates(
            snapshot.root,
            uv=uv,
            environment=source_environment,
            redaction_roots=redaction_roots,
        )
        artifacts = build_and_guard(
            snapshot.root,
            workspace / "postgres-dist",
            uv=uv,
            environment=source_environment,
            redaction_roots=redaction_roots,
        )
        require_postgresql_extra(artifacts)
        with DisposablePostgreSQLRelease(postgres_major) as target:
            for name, artifact in (("wheel", artifacts.wheel), ("sdist", artifacts.sdist)):
                python = install_postgresql_distribution(
                    artifact,
                    workspace,
                    name,
                    uv=uv,
                    environment=environment,
                    redaction_roots=redaction_roots,
                )
                prove_site_packages_import(
                    python,
                    workspace,
                    root,
                    snapshot.root,
                    name=f"postgres-{name}",
                    environment=environment,
                    redaction_roots=redaction_roots,
                )
                principal = f"release_{name}"
                executable = python.parent / "aipcs"
                run_postgresql_config_redaction(
                    python,
                    workspace,
                    executable,
                    principal,
                    target,
                    environment=environment,
                    redaction_roots=redaction_roots,
                )
                run_postgresql_stdio_probe(
                    python,
                    workspace,
                    executable,
                    principal,
                    target,
                    name=name,
                    environment=environment,
                    redaction_roots=redaction_roots,
                )
                run_postgresql_admin_cli_smoke(
                    python,
                    workspace,
                    executable,
                    principal,
                    target,
                    name=name,
                    environment=environment,
                    redaction_roots=redaction_roots,
                )
                run_postgresql_portable_lifecycle_smoke(
                    python,
                    workspace,
                    name,
                    target,
                    environment=environment,
                    redaction_roots=redaction_roots,
                )
        require_clean_checkout_artifacts(root)
        failed = False
    finally:
        retained = not cleanup_workspace(
            workspace,
            failed=failed,
            keep_failed_workdir=keep_failed_workdir,
        )
        if retained:
            print(
                "PostgreSQL release verification failed; retained private diagnostic workspace.",
                file=sys.stderr,
            )
    print(f"PostgreSQL release verification passed for pinned major {postgres_major}.")


def verify_release(
    root: Path = ROOT,
    *,
    keep_failed_workdir: bool = False,
    exact_tip: bool = False,
) -> None:
    """Run the complete source/copy/distribution rehearsal and print a short summary."""

    root = root.resolve()
    environment = scrubbed_environment()
    uv = require_local_preconditions(root)
    require_clean_checkout_artifacts(root)
    exact_commit = (
        require_exact_tip(root, environment=environment)
        if exact_tip
        else None
    )
    workspace = create_workspace()
    failed = True
    summary: str | None = None
    try:
        redaction_roots = (root, workspace)
        source_environment = stage_environment(environment, workspace / "source-uv-environment")
        copy_environment = stage_environment(environment, workspace / "copy-uv-environment")
        run_source_gates(
            root,
            uv=uv,
            environment=source_environment,
            redaction_roots=redaction_roots,
        )
        if exact_commit is not None:
            require_same_exact_tip(root, exact_commit, environment=environment)
        source_snapshot = make_clean_copy(
            root,
            workspace / "source-build-snapshot",
            environment=environment,
        )
        if exact_commit is not None:
            require_same_exact_tip(
                source_snapshot.root,
                exact_commit,
                environment=environment,
            )
            require_same_exact_tip(root, exact_commit, environment=environment)
        source_paths = intended_source_paths(source_snapshot.root, environment=environment)
        source_state = _source_state(source_snapshot.root, environment=environment)
        source_digest = intended_source_sha256(source_snapshot.root, source_paths)
        source_artifacts = build_and_guard(
            source_snapshot.root,
            workspace / "source-dist",
            uv=uv,
            environment=source_environment,
            redaction_roots=redaction_roots,
        )
        clean_copy = make_clean_copy(root, workspace, environment=environment)
        if exact_commit is not None:
            require_same_exact_tip(clean_copy.root, exact_commit, environment=environment)
            require_same_exact_tip(root, exact_commit, environment=environment)
        copy_paths = intended_source_paths(clean_copy.root, environment=environment)
        if (
            _source_state(clean_copy.root, environment=environment) != source_state
            or intended_source_sha256(clean_copy.root, copy_paths) != source_digest
        ):
            raise ReleaseVerificationError("source tree changed during release verification.")
        run_source_gates(
            clean_copy.root,
            uv=uv,
            environment=copy_environment,
            redaction_roots=redaction_roots,
        )
        copy_artifacts = build_and_guard(
            clean_copy.root,
            workspace / "copy-dist",
            uv=uv,
            environment=copy_environment,
            redaction_roots=redaction_roots,
        )
        wheel_python = install_distribution(
            source_artifacts.wheel,
            workspace,
            "wheel",
            uv=uv,
            environment=environment,
            redaction_roots=redaction_roots,
        )
        prove_site_packages_import(
            wheel_python,
            workspace,
            root,
            clean_copy.root,
            name="wheel",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_catalog_smoke(
            wheel_python,
            workspace,
            "wheel",
            heavy=True,
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_domain_schema_smoke(
            wheel_python,
            workspace,
            "wheel",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_relational_contract_smoke(
            wheel_python,
            workspace,
            "wheel",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_lifecycle_contract_smoke(
            wheel_python,
            workspace,
            "wheel",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_registry_r2_smoke(
            wheel_python,
            workspace,
            "wheel",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_wal_release_smoke(
            wheel_python,
            workspace,
            "wheel",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_lifecycle_coordinator_smoke(
            wheel_python,
            workspace,
            "wheel",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_portable_lifecycle_smoke(
            wheel_python,
            workspace,
            "wheel",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_admin_cli_smoke(
            wheel_python,
            workspace,
            "wheel",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_public_lifecycle_smoke(
            wheel_python,
            workspace,
            "wheel",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        sdist_python = install_distribution(
            source_artifacts.sdist,
            workspace,
            "sdist",
            uv=uv,
            environment=environment,
            redaction_roots=redaction_roots,
        )
        prove_site_packages_import(
            sdist_python,
            workspace,
            root,
            clean_copy.root,
            name="sdist",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_catalog_smoke(
            sdist_python,
            workspace,
            "sdist",
            heavy=False,
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_domain_schema_smoke(
            sdist_python,
            workspace,
            "sdist",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_relational_contract_smoke(
            sdist_python,
            workspace,
            "sdist",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_lifecycle_contract_smoke(
            sdist_python,
            workspace,
            "sdist",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_registry_r2_smoke(
            sdist_python,
            workspace,
            "sdist",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_wal_release_smoke(
            sdist_python,
            workspace,
            "sdist",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_lifecycle_coordinator_smoke(
            sdist_python,
            workspace,
            "sdist",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_portable_lifecycle_smoke(
            sdist_python,
            workspace,
            "sdist",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_admin_cli_smoke(
            sdist_python,
            workspace,
            "sdist",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_installed_public_lifecycle_smoke(
            sdist_python,
            workspace,
            "sdist",
            environment=environment,
            redaction_roots=redaction_roots,
        )
        run_sdist_startup_smoke(
            sdist_python,
            workspace,
            environment=environment,
            redaction_roots=redaction_roots,
        )
        require_clean_checkout_artifacts(root)
        if exact_commit is not None:
            require_same_exact_tip(root, exact_commit, environment=environment)
        summary = _summary(
            _commit(source_snapshot.root, environment=environment),
            source_state,
            source_digest,
            source_artifacts,
            copy_artifacts,
        )
        failed = False
    finally:
        retained = not cleanup_workspace(
            workspace,
            failed=failed,
            keep_failed_workdir=keep_failed_workdir,
        )
        if retained:
            print(
                "release verification failed; retained private diagnostic workspace.",
                file=sys.stderr,
            )
    if summary is None:
        raise ReleaseVerificationError("release verification summary was not produced.")
    print(summary)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify an AIPCS release candidate offline.")
    parser.add_argument(
        "--keep-failed-workdir",
        action="store_true",
        help="retain only a failed temporary workspace for local diagnosis",
    )
    parser.add_argument(
        "--exact-tip",
        action="store_true",
        help="require the candidate to be exactly the clean checked-out HEAD commit",
    )
    parser.add_argument(
        "--postgres-integration",
        action="store_true",
        help="run the isolated pinned-image PostgreSQL installed-artifact verifier",
    )
    parser.add_argument(
        "--postgres-major",
        choices=tuple(sorted(_POSTGRES_RELEASE_IMAGES)),
        default="16",
        help="pinned PostgreSQL major used only with --postgres-integration",
    )
    args = parser.parse_args(argv)
    try:
        if args.postgres_integration:
            if args.exact_tip:
                raise ReleaseVerificationError("--exact-tip is unavailable with --postgres-integration.")
            verify_postgresql_integration(
                postgres_major=args.postgres_major,
                keep_failed_workdir=args.keep_failed_workdir,
            )
        else:
            verify_release(
                keep_failed_workdir=args.keep_failed_workdir,
                exact_tip=args.exact_tip,
            )
    except ReleaseVerificationError as error:
        print(f"release verification failed: {_bounded(str(error))}", file=sys.stderr)
        return 1
    except Exception:
        print("release verification failed: unexpected internal failure.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
