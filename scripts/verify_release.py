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
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAX_OUTPUT_CHARS = 4_000
DEFAULT_TIMEOUT_SECONDS = 300
SMOKE_TIMEOUT_SECONDS = 60
_FROZEN_R1_MIGRATION_ID = "registry-0001-initial"
_FROZEN_R1_CHECKSUM = "d40691d8ae8e09b10767b262ac716bc1689c52f4887770d9f43cd84679d291bc"
_FROZEN_R2_MIGRATION_ID = "registry-0002-durable-intent"
_FROZEN_R2_CHECKSUM = "b6190247d4f709728bab59cb4eb5fd149d4b7424472615f377b83f5191e0d8ea"
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
    """Find denied ignored/generated material without entering .git or the development venv."""

    artifacts: list[Path] = []
    for current, directories, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        if current_path == root:
            directories[:] = [name for name in directories if name not in {".git", ".venv"}]
        for directory in tuple(directories):
            folded = directory.casefold()
            if folded in _GENERATED_DIRECTORY_NAMES or folded.endswith(".egg-info"):
                artifacts.append(current_path / directory)
                directories.remove(directory)
        for name in names:
            path = current_path / name
            folded = name.casefold()
            if (
                folded in _GENERATED_FILE_NAMES
                or path.suffix.casefold() in _GENERATED_FILE_SUFFIXES
                or folded.endswith(_GENERATED_FILE_ENDINGS)
                or name.endswith("~")
                or name.startswith(".#")
                or (len(name) > 2 and name.startswith("#") and name.endswith("#"))
            ):
                artifacts.append(path)
    return tuple(sorted(artifacts))


def require_clean_checkout_artifacts(root: Path) -> None:
    if generated_checkout_artifacts(root):
        raise ReleaseVerificationError("source checkout contains generated or database artifacts.")


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

    commands = (
        ("git diff --check", ["git", "diff", "--check"]),
        ("git diff --cached --check", ["git", "diff", "--cached", "--check"]),
        ("git fsck", ["git", "fsck", "--no-dangling"]),
        ("public hygiene", [sys.executable, "scripts/check_public_hygiene.py", "--history"]),
        (
            "pytest",
            [
                uv,
                "run",
                "--locked",
                "--offline",
                "--extra",
                "dev",
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
                "--locked",
                "--offline",
                "--extra",
                "dev",
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
            environment=environment,
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
    """Standalone installed-client proof; it contains no checkout test imports."""

    return r"""
import json
import sys
from pathlib import Path
import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

exe, root, principal, mode = sys.argv[1:]
manifest = {"manifest_version": 2, "schema_version": 1, "entities": [{"name": "note", "attributes": [{"name": "id", "type": "uuid", "required": True, "primary_key": True}, {"name": "owner_id", "type": "string", "required": True}, {"name": "created_at", "type": "datetime", "required": True}, {"name": "updated_at", "type": "datetime", "required": True}, {"name": "created_via", "type": "string", "required": True}, {"name": "record_version", "type": "integer", "required": True}]}], "relationships": [], "indices": [], "query_patterns": [], "discovery_facets": [], "migration_history": []}
metadata_fields = {"service_id", "domain_name", "domain_class", "intent_description", "design_state", "operational_status", "schema", "schema_version", "created_at", "updated_at", "last_activity_at", "materialised_at", "storage"}

async def call(session, name, arguments):
    with anyio.fail_after(10):
        result = await session.call_tool(name, arguments)
    assert result.structuredContent is not None
    return result.structuredContent

def assert_metadata(value, *, designed):
    assert set(value) == metadata_fields
    assert value["design_state"] == "seeded"
    assert value["operational_status"] == "active"
    assert value["materialised_at"] is None
    assert value["storage"] is None
    if designed:
        assert isinstance(value["schema"], dict)
        assert value["schema"]["manifest_version"] == 2
        assert value["schema"]["schema_version"] == 1
        assert value["schema"]["entities"][0]["name"] == "note"
        assert value["schema_version"] == 1
    else:
        assert value["schema"] is None
        assert value["schema_version"] is None

async def main():
    args = ["serve"] if mode == "stateless" else ["serve", "--profile", "sqlite", "--sqlite-data-root", root, "--principal-id", principal]
    params = StdioServerParameters(command=exe, args=args, cwd=str(Path(root).parent), env={"PYTHONNOUSERSITE": "1"})
    with anyio.fail_after(45):
      async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        with anyio.fail_after(10):
            await session.initialize()
        with anyio.fail_after(10):
            tools = await session.list_tools()
        names = [tool.name for tool in tools.tools]
        if mode == "stateless":
            assert names == ["aipcs_server_info"]
            info = await call(session, "aipcs_server_info", {})
            assert info["ok"] is True
            assert info["result"]["features"]["registry_lifecycle"] is False
            return
        assert names == ["aipcs_server_info", "aipcs_service_seed", "aipcs_service_list", "aipcs_service_inspect", "aipcs_service_design"]
        info = await call(session, "aipcs_server_info", {})
        assert info["ok"] is True
        assert info["result"]["features"]["registry_lifecycle"] is True
        seed = {"domain_name": "release_smoke", "domain_class": "release", "intent_description": "installed distribution smoke", "idempotency_key": "release-seed"}
        listed = await call(session, "aipcs_service_list", {})
        if mode == "principal_b":
            assert listed["ok"] is True and listed["result"]["services"] == []
            service_id = (Path(root).parent / "release-service-id.txt").read_text(encoding="utf-8")
            missing = await call(session, "aipcs_service_inspect", {"service_id": service_id})
            assert missing["ok"] is False and missing["error"]["code"] == "not_found"
            created = await call(session, "aipcs_service_seed", seed)
            assert created["ok"] is True
            assert_metadata(created["result"], designed=False)
            return
        services = listed["result"]["services"]
        if services:
            assert len(services) == 1
            assert_metadata(services[0], designed=True)
            service_id = services[0]["service_id"]
        else:
            created = await call(session, "aipcs_service_seed", seed)
            assert created["ok"] is True
            assert_metadata(created["result"], designed=False)
            service_id = created["result"]["service_id"]
            (Path(root).parent / "release-service-id.txt").write_text(service_id, encoding="utf-8")
        design = {"service_id": service_id, "schema": manifest, "idempotency_key": "release-design"}
        designed = await call(session, "aipcs_service_design", design)
        assert designed["ok"] is True
        assert_metadata(designed["result"], designed=True)
        design_replay = await call(session, "aipcs_service_design", design)
        assert design_replay == designed
        replay = await call(session, "aipcs_service_seed", seed)
        assert replay["ok"] is True and replay["result"]["service_id"] == service_id
        assert_metadata(replay["result"], designed=False)
        inspected = await call(session, "aipcs_service_inspect", {"service_id": service_id})
        assert inspected["ok"] is True
        assert_metadata(inspected["result"], designed=True)
        final_list = await call(session, "aipcs_service_list", {})
        assert final_list["ok"] is True and len(final_list["result"]["services"]) == 1
        assert_metadata(final_list["result"]["services"][0], designed=True)

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
    "service_store", 0, 1, "uninitialised"
)
assert not root.exists()

ready = first.migrate(locator_a)
assert (ready.component, ready.applied_revision, ready.target_revision, ready.status) == (
    "service_store", 1, 1, "ready"
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
assert objects == {"__aipcs_service_store_meta", "__aipcs_service_store_migration"}
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

    return r'''
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
    if foundation is FoundationObservation.UNINITIALISED:
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
assert materialise_valid_count == 14

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
assert evolve_valid_count == 21

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
    """Standalone installed V1-08B registry migration and ledger proof."""

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
from aipcs_mcp.storage.sqlite.migrations import R1, R2

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
assert adapter.inspect_migration() == MigrationState("registry", 1, 2, "incompatible")
assert adapter.migrate() == MigrationState("registry", 2, 2, "ready")
with sqlite3.connect(database) as connection:
    assert connection.execute(
        'SELECT revision,migration_id,checksum FROM "aipcs_registry_migration" ORDER BY revision'
    ).fetchall() == [
        (1, frozen_r1_migration_id, frozen_r1_checksum),
        (2, frozen_r2_migration_id, frozen_r2_checksum),
    ]
    assert connection.execute(
        'SELECT "service_revision","materialised_at","storage_backend","storage_namespace" '
        'FROM "aipcs_registry_service" ORDER BY "service_id"'
    ).fetchall() == [
        (1, None, None, None),
        (1, None, None, None),
    ]
    assert connection.execute(
        'SELECT result_json FROM "aipcs_registry_mutation" '
        'WHERE "principal_id"=? AND "idempotency_key"=?',
        ("principal-a", "legacy-a"),
    ).fetchone() == (result_a,)
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
        "registry", 1, 2, "incompatible"
    )
    assert case_adapter.migrate() == MigrationState("registry", 2, 2, "ready")
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
        'FROM "aipcs_registry_mutation" WHERE "principal_id"=? '
        'ORDER BY "idempotency_key"',
        ("lifecycle-principal",),
    ).fetchall()
    invalid_insert = (
        'INSERT INTO "aipcs_registry_mutation"('
        '"principal_id","idempotency_key","fingerprint","service_id","operation_kind",'
        '"phase","created_via","expected_service_revision","expected_schema_version",'
        '"target_manifest_json","result_json","recovery_category") '
        'SELECT "principal_id",?,"fingerprint","service_id",?,?,"created_via",'
        '"expected_service_revision","expected_schema_version","target_manifest_json",?,? '
        'FROM "aipcs_registry_mutation" WHERE "principal_id"=? AND "idempotency_key"=?'
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
        'SELECT count(*) FROM "aipcs_registry_mutation" '
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
assert adapter.inspect_migration() == MigrationState("registry", 2, 2, "ready")

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
        "registry", 2, 2, "incompatible"
    )
    assert corrupt_database.read_bytes() == corrupt_before
    assert corrupt_adapter.migrate() == MigrationState(
        "registry", 2, 2, "incompatible"
    )
    assert corrupt_database.read_bytes() == corrupt_before


prove_corruption_fails_closed(
    "row",
    'UPDATE "aipcs_registry_mutation" SET "result_json"=? '
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


def run_wheel_restart_principal_smoke(
    python: Path,
    workspace: Path,
    *,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> None:
    """Exercise installed wheel restart/idempotency/isolation with only public MCP data."""

    executable = python.parent / "aipcs"
    if not executable.is_file():
        raise ReleaseVerificationError("installed wheel did not provide the aipcs console command.")
    sqlite_root = workspace / "sqlite-root"
    sqlite_root.mkdir(mode=0o700)
    _run_smoke_client(
        python,
        workspace,
        executable,
        sqlite_root,
        "release-principal-a",
        "principal_a",
        environment=environment,
        redaction_roots=redaction_roots,
    )
    _run_smoke_client(
        python,
        workspace,
        executable,
        sqlite_root,
        "release-principal-a",
        "principal_a",
        environment=environment,
        redaction_roots=redaction_roots,
    )
    _run_smoke_client(
        python,
        workspace,
        executable,
        sqlite_root,
        "release-principal-b",
        "principal_b",
        environment=environment,
        redaction_roots=redaction_roots,
    )


def run_sdist_startup_smoke(
    python: Path,
    workspace: Path,
    *,
    environment: Mapping[str, str],
    redaction_roots: Iterable[Path],
) -> None:
    """Exercise real installed sdist stateless startup with the same external client."""

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


def verify_release(root: Path = ROOT, *, keep_failed_workdir: bool = False) -> None:
    """Run the complete source/copy/distribution rehearsal and print a short summary."""

    root = root.resolve()
    environment = scrubbed_environment()
    uv = require_local_preconditions(root)
    require_clean_checkout_artifacts(root)
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
        source_snapshot = make_clean_copy(
            root,
            workspace / "source-build-snapshot",
            environment=environment,
        )
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
        run_wheel_restart_principal_smoke(
            wheel_python,
            workspace,
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
        run_sdist_startup_smoke(
            sdist_python,
            workspace,
            environment=environment,
            redaction_roots=redaction_roots,
        )
        require_clean_checkout_artifacts(root)
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
    args = parser.parse_args(argv)
    try:
        verify_release(keep_failed_workdir=args.keep_failed_workdir)
    except ReleaseVerificationError as error:
        print(f"release verification failed: {_bounded(str(error))}", file=sys.stderr)
        return 1
    except Exception:
        print("release verification failed: unexpected internal failure.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
