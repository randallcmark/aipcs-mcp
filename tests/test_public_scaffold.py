from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_package_metadata_matches_namespace_version() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert metadata["project"]["name"] == "aipcs-mcp"
    assert metadata["project"]["version"] == "0.0.0.dev0"
    assert metadata["project"]["requires-python"] == ">=3.12"
    assert '__version__ = "0.0.0.dev0"' in (ROOT / "src/aipcs_mcp/__init__.py").read_text()


def test_tracked_tree_passes_public_hygiene_check() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_public_hygiene.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
