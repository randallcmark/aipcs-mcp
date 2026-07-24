from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

from aipcs_mcp import __version__

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "public_hygiene", ROOT / "scripts/check_public_hygiene.py"
)
assert SPEC and SPEC.loader
hygiene = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hygiene)


def reasons(path: str, data: bytes = b"safe", *, symlink: bool = False) -> list[str]:
    return hygiene.violations_for_blob(PurePosixPath(path), data, is_symlink=symlink)


def test_package_metadata_and_normal_tree() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert metadata["project"]["name"] == "aipcs-mcp"
    assert metadata["project"]["dynamic"] == ["version"]
    assert metadata["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "aipcs_mcp.__version__"
    }
    assert __version__ == "0.0.0.dev0"
    assert set(metadata["project"]["optional-dependencies"]) == {"postgresql"}
    assert set(metadata["dependency-groups"]) == {"dev"}
    result = subprocess.run(
        [sys.executable, "scripts/check_public_hygiene.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_path_and_binary_probes_are_rejected() -> None:
    assert "tracked symlink" in reasons("docs/link.md", symlink=True)
    assert "local data path" in reasons("examples/.data-old/note.txt")
    assert "database or likely credential filename" in reasons("state/service.sqlite")
    assert "SQLite file magic" in reasons("state/cache.bin", b"SQLite format 3" + b"\x00")
    assert "unexpected binary content" in reasons("assets/archive.bin", b"PK\x03\x04\x00")
    assert "unexpected non-UTF-8 content" in reasons("assets/opaque.bin", b"\xff\xfe")
    assert "captured transcript path" in reasons("captures/agent-session.txt")
    assert "private agent/archive/plan path" in reasons("docs/exec-plans/plan.md")
    assert "private preservation path" in reasons("docs/private-artifact-report.md")


def test_exact_credential_filenames_are_ignored_and_rejected_from_tracked_blobs() -> None:
    credential_names = (
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "credentials.yaml",
        "credentials.yml",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    )
    for name in credential_names:
        assert "likely credential filename" in reasons(
            f"examples/{name}", b"non-sensitive representative payload\n"
        )
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", "--", f"examples/{name}"],
            cwd=ROOT,
            check=False,
        )
        assert ignored.returncode == 0, name

    assert reasons("src/id_rsa_helper.py") == []
    assert reasons("docs/credentials.yaml.example") == []


def test_private_filename_and_path_checks_are_case_insensitive_and_report_original_path() -> None:
    assert "private agent/archive/plan path" in reasons("Agents.md")
    assert reasons("examples/agent-instructions/AGENTS.md") == []
    assert "private agent/archive/plan path" in reasons("examples/other/AGENTS.md")
    assert "likely credential filename" in reasons("examples/CREDENTIALS.YAML")
    assert "private agent/archive/plan path" in reasons("Docs/EXEC-PLANS/plan.md")
    assert "private agent/archive/plan path" in reasons("RESEARCH/note.md")
    assert "local data path" in reasons("examples/.DATA-cache/note.txt")
    assert reasons("docs/.ENV.EXAMPLE") == []

    reported = hygiene.report(
        "worktree", [(PurePosixPath("Docs/EXEC-PLANS/plan.md"), b"safe", False)]
    )
    assert reported == ["worktree:Docs/EXEC-PLANS/plan.md: private agent/archive/plan path"]


def test_content_and_fixture_probes_are_rejected() -> None:
    local_path = b"/" + b"Users/example/workspace"
    credential = ("gh" + "p_" + "x" * 24).encode()
    legacy = ("aipcs" + "_server").encode()
    assert "local absolute path" in reasons("docs/guide.md", local_path)
    assert "likely credential" in reasons("docs/guide.md", credential)
    assert "legacy private package namespace" in reasons("src/module.py", legacy)
    assert reasons("src/module.py", b"aipcs_server_info") == []
    assert "fixture lacks synthetic marker and provenance comment" in reasons(
        "tests/fixtures/demo.json"
    )
    good_fixture = (
        b"SYNTHETIC" + b"_FIXTURE\nSynthetic fixture provenance: hand-written test data\n"
    )
    assert reasons("tests/fixtures/demo.json", good_fixture) == []
