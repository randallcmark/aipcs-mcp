#!/usr/bin/env python3
"""Reject private artifacts from the tracked tree or any reachable history."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SQLITE_MAGIC = b"SQLite format 3\x00"
PRIVATE_PARTS = {
    ".agents",
    ".claude",
    ".codex",
    ".cursor",
    "archives",
    "captures",
    "private-data",
    "research",
    "transcripts",
    "exec-plans",
}
PRIVATE_NAMES = {
    "AGENTS.md",
    "CLAUDE.md",
    "credentials.json",
    "destination-sha256.tsv",
    "secrets.json",
    "source-sha256.tsv",
}
PRIVATE_SUFFIXES = {
    ".sqlite",
    ".sqlite3",
    ".db",
    ".db3",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".crt",
    ".cer",
}
TRANSCRIPT = re.compile(
    r"(?:^|/)(?:20\d{2}-\d{2}-\d{2}-.+|[^/]*(?:transcript|session)[^/]*)"
    r"\.(?:txt|md|json)$",
    re.I,
)
PRIVATE_MANIFEST = re.compile(r"(?:private-artifact|preservation-manifest|restore-test)", re.I)
LEGACY_NAMESPACE = "aipcs" + "_server"
PUBLIC_TOOL_IDENTIFIERS = ("aipcs_server_info",)
SYNTHETIC_MARKER = "SYNTHETIC" + "_FIXTURE"
PROVENANCE_MARKER = "Synthetic fixture " + "provenance:"
HOME_PATH = re.compile(
    r"(?i)(?:/" + r"Users/|/" + r"home/|/" + r"Volumes/|[A-Z]:\\Users\\|file:" + r"//)"
)
CREDENTIAL = re.compile(
    r"(?i)(-----BEGIN(?:[ A-Z0-9]+)? "
    + "PRIVATE"
    + r" KEY-----|\b"
    + "AK"
    + r"IA[0-9A-Z]{16}\b|\b"
    + "gh"
    + r"[pousr]_[A-Za-z0-9_]{20,}\b|\bgithub"
    + r"_pat_[A-Za-z0-9_]{20,}\b|\bsk-[A-Za-z0-9_-]{20,}\b"
    + r"|\bxox[baprs]-[A-Za-z0-9-]{20,}\b)"
)


def run_git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def path_violations(relative: PurePosixPath) -> list[str]:
    parts = relative.parts
    name = relative.name
    posix = relative.as_posix()
    problems: list[str] = []
    if any(part.startswith(".data") for part in parts):
        problems.append("local data path")
    if set(parts) & PRIVATE_PARTS or name in PRIVATE_NAMES:
        problems.append("private agent/archive/plan path")
    if name.startswith(".env") and name != ".env.example":
        problems.append("likely credential filename")
    if relative.suffix.lower() in PRIVATE_SUFFIXES:
        problems.append("database or likely credential filename")
    if TRANSCRIPT.search(posix):
        problems.append("captured transcript path")
    if PRIVATE_MANIFEST.search(posix):
        problems.append("private preservation path")
    return problems


def content_violations(relative: PurePosixPath, data: bytes) -> list[str]:
    if data.startswith(SQLITE_MAGIC):
        return ["SQLite file magic"]
    if b"\0" in data:
        return ["unexpected binary content"]
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return ["unexpected non-UTF-8 content"]
    problems: list[str] = []
    if CREDENTIAL.search(text):
        problems.append("likely credential")
    if HOME_PATH.search(text):
        problems.append("local absolute path")
    namespace_scan = text
    for identifier in PUBLIC_TOOL_IDENTIFIERS:
        namespace_scan = namespace_scan.replace(identifier, "")
    if LEGACY_NAMESPACE in namespace_scan:
        problems.append("legacy private package namespace")
    tokens = [
        item.strip().casefold()
        for item in os.environ.get("AIPCS_HYGIENE_FORBIDDEN_TOKENS", "").split(",")
        if item.strip()
    ]
    if any(token in text.casefold() for token in tokens):
        problems.append("maintainer-supplied private token")
    if re.search(r"fixture|corpus|scenario|snapshot", relative.as_posix(), re.I):
        if SYNTHETIC_MARKER not in text or PROVENANCE_MARKER not in text:
            problems.append("fixture lacks synthetic marker and provenance comment")
    return problems


def violations_for_blob(
    relative: PurePosixPath, data: bytes, *, is_symlink: bool = False
) -> list[str]:
    problems = path_violations(relative)
    if is_symlink:
        return problems + ["tracked symlink"]
    return problems + content_violations(relative, data)


def worktree_entries() -> list[tuple[PurePosixPath, bytes, bool]]:
    entries: list[tuple[PurePosixPath, bytes, bool]] = []
    for item in run_git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split(
        b"\0"
    ):
        if not item:
            continue
        relative = PurePosixPath(item.decode())
        path = ROOT / relative
        if not path.exists() and not path.is_symlink():
            continue
        entries.append(
            (
                relative,
                path.read_bytes() if not path.is_symlink() else b"",
                path.is_symlink(),
            )
        )
    return entries


def history_entries(commit: str) -> list[tuple[PurePosixPath, bytes, bool]]:
    entries: list[tuple[PurePosixPath, bytes, bool]] = []
    for item in run_git("ls-tree", "-rz", commit).split(b"\0"):
        if not item:
            continue
        metadata, raw_path = item.split(b"\t", 1)
        mode, _kind, object_id = metadata.decode().split()
        entries.append(
            (
                PurePosixPath(raw_path.decode()),
                run_git("cat-file", "-p", object_id),
                mode == "120000",
            )
        )
    return entries


def report(scope: str, entries: list[tuple[PurePosixPath, bytes, bool]]) -> list[str]:
    return [
        f"{scope}:{relative}: {problem}"
        for relative, data, is_symlink in entries
        for problem in violations_for_blob(relative, data, is_symlink=is_symlink)
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", action="store_true", help="scan every reachable commit")
    args = parser.parse_args()
    failures = report("worktree", worktree_entries())
    if args.history:
        for commit in run_git("rev-list", "--all").decode().splitlines():
            failures.extend(report(commit, history_entries(commit)))
    if failures:
        print("Public hygiene check failed:", file=sys.stderr)
        print("\n".join(sorted(set(failures))), file=sys.stderr)
        return 1
    print("Public hygiene check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
