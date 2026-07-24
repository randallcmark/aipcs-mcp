"""SYNTHETIC_FIXTURE. Synthetic fixture provenance: public documentation contract."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
PRIVATE_TOKENS = (
    "/" + "Users/",
    "/" + "Volumes/",
    "mark" + "randall",
    "aipcs" + "-server",
    ".co" + "dex/",
    ".ag" + "ents/",
    "docs/exec-" + "plans",
)


def public_markdown() -> list[Path]:
    return sorted(
        [
            ROOT / "README.md",
            ROOT / "CONTRIBUTING.md",
            *ROOT.glob("docs/*.md"),
            *ROOT.glob("examples/**/*.md"),
        ]
    )


def test_public_documentation_has_no_broken_local_links() -> None:
    failures: list[str] = []
    for document in public_markdown():
        text = document.read_text(encoding="utf-8")
        for raw_target in MARKDOWN_LINK.findall(text):
            target = raw_target.strip().strip("<>")
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path_text = target.split("#", 1)[0]
            if not path_text:
                continue
            resolved = (document.parent / path_text).resolve()
            if not resolved.is_relative_to(ROOT) or not resolved.exists():
                failures.append(f"{document.relative_to(ROOT)} -> {raw_target}")
    assert failures == []


def test_public_documentation_contains_no_private_repository_context() -> None:
    failures = [
        f"{document.relative_to(ROOT)}: {token}"
        for document in public_markdown()
        for token in PRIVATE_TOKENS
        if token.casefold() in document.read_text(encoding="utf-8").casefold()
    ]
    assert failures == []


def test_required_consumer_guides_and_agent_example_exist() -> None:
    required = {
        "README.md",
        "CONTRIBUTING.md",
        "docs/quickstart.md",
        "docs/concepts.md",
        "docs/capabilities.md",
        "docs/configuration.md",
        "docs/storage.md",
        "docs/administration.md",
        "docs/backup-and-migration.md",
        "docs/troubleshooting.md",
        "docs/compatibility.md",
        "docs/design-evolution.md",
        "docs/architecture.md",
        "docs/agent-integration.md",
        "examples/agent-instructions/README.md",
        "examples/agent-instructions/AGENTS.md",
    }
    assert {path.relative_to(ROOT).as_posix() for path in public_markdown()} >= required
