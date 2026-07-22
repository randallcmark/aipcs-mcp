from __future__ import annotations

import io
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from zipfile import ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]


def _wheel(path: Path, members: tuple[str, ...]) -> Path:
    with ZipFile(path, "w") as archive:
        for member in members:
            archive.writestr(member, "synthetic public test content")
    return path


def _sdist(path: Path, members: tuple[str, ...]) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for member in members:
            payload = b"synthetic public test content"
            info = tarfile.TarInfo(member)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return path


def test_wheel_content_guard_accepts_production_and_rejects_test_harness(tmp_path: Path) -> None:
    safe = _wheel(
        tmp_path / "safe.whl",
        ("aipcs_mcp/storage/contracts.py", "aipcs_mcp-0.dist-info/METADATA"),
    )
    accepted = subprocess.run(
        [sys.executable, "scripts/check_wheel_contents.py", str(safe)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr

    unsafe = _wheel(
        tmp_path / "unsafe.whl",
        ("aipcs_mcp/storage/contracts.py", "tests/storage_contracts/conformance.py"),
    )
    rejected = subprocess.run(
        [sys.executable, "scripts/check_wheel_contents.py", str(unsafe)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 1
    assert "test, private, or local-state path" in rejected.stdout


def test_sdist_content_guard_accepts_production_and_rejects_private_material(
    tmp_path: Path,
) -> None:
    safe = _sdist(
        tmp_path / "safe.tar.gz",
        (
            "aipcs_mcp-0/src/aipcs_mcp/storage/contracts.py",
            "aipcs_mcp-0/src/aipcs_mcp.egg-info/SOURCES.txt",
            "aipcs_mcp-0/pyproject.toml",
        ),
    )
    accepted = subprocess.run(
        [sys.executable, "scripts/check_wheel_contents.py", str(safe)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert "Artifact content check passed." in accepted.stdout

    unsafe = _sdist(
        tmp_path / "unsafe.tar.gz",
        ("aipcs_mcp-0/src/aipcs_mcp/storage/contracts.py", "aipcs_mcp-0/AGENTS.md"),
    )
    rejected = subprocess.run(
        [sys.executable, "scripts/check_wheel_contents.py", str(unsafe)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 1
    assert "private operating instruction" in rejected.stdout


def test_content_guard_rejects_generated_build_paths(tmp_path: Path) -> None:
    unsafe = _wheel(
        tmp_path / "unsafe.whl",
        (
            "aipcs_mcp/storage/contracts.py",
            "build/lib/aipcs_mcp/contracts.py",
        ),
    )
    rejected = subprocess.run(
        [sys.executable, "scripts/check_wheel_contents.py", str(unsafe)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 1
    assert "test, private, or local-state path" in rejected.stdout


def test_content_guard_rejects_opaque_and_private_member_content(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe-content.whl"
    with ZipFile(unsafe, "w") as archive:
        archive.writestr("aipcs_mcp/archive.bin", b"PK\x03\x04\x00private")
        archive.writestr("aipcs_mcp/note.txt", b"/" + b"Users/example/private")
    rejected = subprocess.run(
        [sys.executable, "scripts/check_wheel_contents.py", str(unsafe)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 1
    assert "unexpected binary content" in rejected.stdout
    assert "local absolute path" in rejected.stdout


def test_content_guard_rejects_nonregular_zip_member(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe-mode.whl"
    fifo = ZipInfo("aipcs_mcp/pipe")
    fifo.create_system = 3
    fifo.external_attr = (stat.S_IFIFO | 0o644) << 16
    with ZipFile(unsafe, "w") as archive:
        archive.writestr(fifo, b"synthetic public test content")
    rejected = subprocess.run(
        [sys.executable, "scripts/check_wheel_contents.py", str(unsafe)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 1
    assert "non-regular archive entry" in rejected.stdout
