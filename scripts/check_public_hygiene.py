#!/usr/bin/env python3
"""Fail if tracked files violate the clean public-repository boundary."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQLITE_MAGIC = b"SQLite format 3\x00"
PRIVATE_PARTS = {".agents", ".claude", ".codex", ".cursor", "archives", "private-data"}
PRIVATE_NAMES = {
    "AGENTS.md",
    "CLAUDE.md",
    "credentials.json",
    "destination-sha256.tsv",
    "secrets.json",
    "source-sha256.tsv",
}
PRIVATE_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".db3", ".pem", ".key", ".p12", ".pfx", ".crt", ".cer"}
TRANSCRIPT = re.compile(r"(?:^|/)(?:20\d{2}-\d{2}-\d{2}-.+|[^/]*(?:transcript|session)[^/]*)\.(?:txt|md|json)$", re.I)
PRIVATE_MANIFEST = re.compile(r"(?:private-artifact|preservation-manifest|restore-test)", re.I)
CREDENTIAL = re.compile(
    r"(?i)(-----BEGIN(?:[ A-Z0-9]+)? PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|"
    r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b|"
    r"\bsk-[A-Za-z0-9_-]{20,}\b|\bxox[baprs]-[A-Za-z0-9-]{20,}\b)"
)
HOME_PATH = re.compile(
    r"(?i)(?:/" + r"Users/|/" + r"home/|/" + r"Volumes/|[A-Z]:\\Users\\|file:" + r"//)"
)


def tracked_paths() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    )
    return [ROOT / item.decode() for item in result.stdout.split(b"\0") if item]


def violations(path: Path) -> list[str]:
    relative = path.relative_to(ROOT)
    parts = set(relative.parts)
    problems: list[str] = []
    if parts & PRIVATE_PARTS or path.name in PRIVATE_NAMES:
        problems.append("private agent/archive path")
    if path.suffix.lower() in PRIVATE_SUFFIXES or (
        path.name.startswith(".env") and path.name != ".env.example"
    ):
        problems.append("database or likely credential filename")
    if TRANSCRIPT.search(relative.as_posix()):
        problems.append("captured transcript filename")
    if PRIVATE_MANIFEST.search(relative.as_posix()):
        problems.append("private preservation manifest")
    if path.is_symlink():
        problems.append("tracked symlink")
        return problems
    try:
        data = path.read_bytes()
    except OSError as error:
        return problems + [f"unreadable tracked file: {error}"]
    if data.startswith(SQLITE_MAGIC):
        problems.append("SQLite file magic")
        return problems
    if b"\0" in data:
        return problems
    text = data.decode("utf-8", errors="replace")
    if CREDENTIAL.search(text):
        problems.append("likely credential")
    if HOME_PATH.search(text):
        problems.append("local absolute path")
    tokens = [item.strip().casefold() for item in os.environ.get(
        "AIPCS_HYGIENE_FORBIDDEN_TOKENS", ""
    ).split(",") if item.strip()]
    if any(token in text.casefold() for token in tokens):
        problems.append("maintainer-supplied private token")
    if re.search(r"fixture|corpus|scenario|snapshot", path.name, re.I) and (
        "SYNTHETIC_FIXTURE" not in text
    ):
        problems.append("fixture lacks SYNTHETIC_FIXTURE marker")
    return problems


def main() -> int:
    failures = [
        f"{path.relative_to(ROOT)}: {problem}"
        for path in tracked_paths()
        for problem in violations(path)
    ]
    if failures:
        print("Public hygiene check failed:", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("Public hygiene check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
