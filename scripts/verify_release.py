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
