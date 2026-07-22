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


def verify_release(root: Path = ROOT, *, keep_failed_workdir: bool = False) -> None:
    """Run the complete source/copy/distribution rehearsal and print a short summary."""

    root = root.resolve()
    environment = scrubbed_environment()
    uv = require_local_preconditions(root)
    workspace = Path(tempfile.mkdtemp(prefix="aipcs-release-"))
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
        run_sdist_startup_smoke(
            sdist_python,
            workspace,
            environment=environment,
            redaction_roots=redaction_roots,
        )
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
