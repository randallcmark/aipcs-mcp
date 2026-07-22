"""Fast stdlib-only unit coverage for the manual release-rehearsal command."""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_release.py"


@pytest.fixture(scope="module")
def verifier():
    spec = importlib.util.spec_from_file_location("release_verifier_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "release-test@example.invalid")
    _git(root, "config", "user.name", "Release Test")
    (root / ".gitignore").write_text("*.sqlite\n.venv/\ncache/\n", encoding="utf-8")
    (root / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    (root / "tracked.txt").write_text("dirty tracked content\n", encoding="utf-8")
    (root / "new.txt").write_text("untracked source\n", encoding="utf-8")
    (root / "ignored.sqlite").write_text("not source\n", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "ignored.txt").write_text("not source\n", encoding="utf-8")
    (root / "cache").mkdir()
    (root / "cache" / "ignored.txt").write_text("not source\n", encoding="utf-8")
    return root


def test_intended_set_keeps_dirty_and_untracked_but_excludes_ignored(
    tmp_path: Path, verifier
) -> None:
    root = _repository(tmp_path)
    paths = verifier.intended_source_paths(root, environment=verifier.scrubbed_environment({}))
    relative = {path.relative_to(root).as_posix() for path in paths}
    assert {".gitignore", "tracked.txt", "new.txt"} <= relative
    assert "ignored.sqlite" not in relative
    assert ".venv/ignored.txt" not in relative
    assert (root / "tracked.txt").read_text(encoding="utf-8") == "dirty tracked content\n"


def test_generated_checkout_artifacts_excludes_only_root_git_and_venv(
    tmp_path: Path, verifier
) -> None:
    root = tmp_path / "source"
    (root / ".git" / "__pycache__").mkdir(parents=True)
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / ".venv" / "lib" / "ignored.pyc").write_bytes(b"ignored")
    (root / "src" / "__pycache__").mkdir(parents=True)
    (root / "src" / "__pycache__" / "module.pyc").write_bytes(b"generated")
    (root / ".mypy_cache").mkdir()
    (root / "coverage.xml").write_text("generated", encoding="utf-8")
    (root / "module.py~").write_text("generated", encoding="utf-8")
    (root / "state.sqlite").write_bytes(b"generated")

    relative = {
        path.relative_to(root).as_posix() for path in verifier.generated_checkout_artifacts(root)
    }
    assert relative == {
        ".mypy_cache",
        "coverage.xml",
        "module.py~",
        "src/__pycache__",
        "state.sqlite",
    }
    with pytest.raises(verifier.ReleaseVerificationError, match="generated or database"):
        verifier.require_clean_checkout_artifacts(root)


def test_release_workspace_canonicalises_a_system_style_intermediate_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verifier
) -> None:
    actual_parent = tmp_path / "actual-parent"
    workspace = actual_parent / "workspace"
    workspace.mkdir(mode=0o700, parents=True)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    presented = linked_parent / workspace.name
    monkeypatch.setattr(verifier.tempfile, "mkdtemp", lambda **_: str(presented))

    canonical = verifier.create_workspace()

    assert canonical == workspace.resolve(strict=True)
    assert canonical.is_dir()


def test_clean_copy_preserves_content_and_mode_with_distinct_inodes(
    tmp_path: Path, verifier
) -> None:
    root = _repository(tmp_path)
    executable = root / "new.txt"
    executable.chmod(0o744)
    copied = verifier.make_clean_copy(
        root,
        tmp_path / "workspace",
        environment=verifier.scrubbed_environment({}),
    )
    copied_file = copied.root / "new.txt"
    assert copied_file.read_text(encoding="utf-8") == "untracked source\n"
    assert stat.S_IMODE(copied_file.stat().st_mode) == 0o744
    assert (copied.root / "ignored.sqlite").exists() is False
    assert (copied.root / ".git").is_dir()
    assert not (root / "new.txt").samefile(copied_file)
    _git(copied.root, "fsck", "--no-dangling")
    verifier.assert_distinct_inodes(copied)


def test_inode_check_rejects_a_deliberate_hard_link(tmp_path: Path, verifier) -> None:
    root = tmp_path / "source"
    destination = tmp_path / "destination"
    root.mkdir()
    destination.mkdir()
    source = root / "file.txt"
    source.write_text("source", encoding="utf-8")
    linked = destination / "different-name.txt"
    os.link(source, linked)
    copy = verifier.CleanCopy(destination, (source,), (linked,))
    with pytest.raises(verifier.ReleaseVerificationError, match="hard-linked"):
        verifier.assert_distinct_inodes(copy)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform has no symbolic links")
def test_clean_copy_rejects_symlink_source_entries(tmp_path: Path, verifier) -> None:
    root = _repository(tmp_path)
    target = root / "tracked.txt"
    link = root / "new-link.txt"
    link.symlink_to(target)
    _git(root, "add", "new-link.txt")
    with pytest.raises(verifier.ReleaseVerificationError, match="regular source files only"):
        verifier.make_clean_copy(
            root,
            tmp_path / "workspace",
            environment=verifier.scrubbed_environment({}),
        )


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform has no symbolic links")
def test_clean_copy_rejects_symlinked_source_ancestor(tmp_path: Path, verifier) -> None:
    root = _repository(tmp_path)
    nested = root / "nested"
    nested.mkdir()
    tracked = nested / "tracked.txt"
    tracked.write_text("inside repository\n", encoding="utf-8")
    _git(root, "add", "nested/tracked.txt")
    _git(root, "commit", "-m", "add nested file")
    tracked.unlink()
    nested.rmdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "tracked.txt").write_text("outside repository\n", encoding="utf-8")
    nested.symlink_to(external, target_is_directory=True)

    with pytest.raises(verifier.ReleaseVerificationError, match="ancestors"):
        verifier._require_regular_file(nested / "tracked.txt", root)
    with pytest.raises(verifier.ReleaseVerificationError):
        verifier.make_clean_copy(
            root,
            tmp_path / "workspace",
            environment=verifier.scrubbed_environment({}),
        )


def test_clean_copy_rejects_git_alternates(tmp_path: Path, verifier) -> None:
    root = _repository(tmp_path)
    alternates = root / ".git" / "objects" / "info" / "alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text("/private/hidden-object-store\n", encoding="utf-8")
    with pytest.raises(verifier.ReleaseVerificationError, match="alternates"):
        verifier.make_clean_copy(
            root,
            tmp_path / "workspace",
            environment=verifier.scrubbed_environment({}),
        )


def test_discover_artifacts_requires_exact_wheel_and_sdist(tmp_path: Path, verifier) -> None:
    (tmp_path / "aipcs_mcp-0.whl").write_bytes(b"wheel")
    (tmp_path / "aipcs_mcp-0.tar.gz").write_bytes(b"sdist")
    (tmp_path / ".gitignore").write_text("*\n", encoding="utf-8")
    artifacts = verifier.discover_artifacts(tmp_path)
    assert artifacts.wheel.name.endswith(".whl")
    (tmp_path / "extra.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(verifier.ReleaseVerificationError, match="exactly one wheel"):
        verifier.discover_artifacts(tmp_path)


def test_scrubbing_redaction_and_cleanup_are_bounded(tmp_path: Path, verifier) -> None:
    environment = verifier.scrubbed_environment(
        {
            "PYTHONPATH": "bad",
            "PYTHONHOME": "bad",
            "VIRTUAL_ENV": "bad",
            "CONDA_PREFIX": "bad",
            "KEEP": "yes",
        }
    )
    assert all(
        key not in environment
        for key in (
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
            "CONDA_PREFIX",
            "GIT_DIR",
            "GIT_WORK_TREE",
        )
    )
    assert environment["UV_OFFLINE"] == "1"
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment["KEEP"] == "yes"
    staged = verifier.stage_environment(environment, tmp_path / "external-uv-environment")
    assert staged["UV_PROJECT_ENVIRONMENT"] == str(tmp_path / "external-uv-environment")
    hidden = verifier.redact_text(f"{tmp_path}/secret " + "x" * 8_000, (tmp_path,))
    assert str(tmp_path) not in hidden
    assert "<redacted-path>" in hidden
    assert len(hidden) <= verifier.MAX_OUTPUT_CHARS + len("\n[output truncated]")
    removable = tmp_path / "remove"
    removable.mkdir()
    assert verifier.cleanup_workspace(removable, failed=False, keep_failed_workdir=True) is True
    assert not removable.exists()
    retained = tmp_path / "retain"
    retained.mkdir()
    assert verifier.cleanup_workspace(retained, failed=True, keep_failed_workdir=True) is False
    assert retained.exists()


def test_cleanup_failure_blocks_success_without_masking_an_existing_failure(
    monkeypatch, tmp_path: Path, verifier
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def fail_cleanup(_path: Path) -> None:
        raise OSError("private cleanup detail")

    monkeypatch.setattr(verifier.shutil, "rmtree", fail_cleanup)
    with pytest.raises(verifier.ReleaseVerificationError, match="cleanup failed"):
        verifier.cleanup_workspace(workspace, failed=False, keep_failed_workdir=False)
    assert verifier.cleanup_workspace(workspace, failed=True, keep_failed_workdir=False) is False


def test_dirty_summary_names_base_commit_and_hashes_intended_source(
    tmp_path: Path, verifier
) -> None:
    root = _repository(tmp_path)
    environment = verifier.scrubbed_environment({})
    paths = verifier.intended_source_paths(root, environment=environment)
    digest = verifier.intended_source_sha256(root, paths)
    assert verifier._source_state(root, environment=environment) == "dirty"
    wheel = tmp_path / "candidate.whl"
    sdist = tmp_path / "candidate.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    artifacts = verifier.ArtifactSet(wheel, sdist)
    summary = verifier._summary("abc123", "dirty", digest, artifacts, artifacts)
    assert summary.startswith(
        "release verification passed for dirty source tree based on commit abc123"
    )
    assert f"source_state=dirty intended_source_sha256={digest}" in summary
    assert "passed for commit abc123" not in summary
    (root / "new.txt").write_text("changed source\n", encoding="utf-8")
    changed = verifier.intended_source_sha256(
        root, verifier.intended_source_paths(root, environment=environment)
    )
    assert changed != digest


def test_origin_rule_rejects_source_and_non_site_imports(tmp_path: Path, verifier) -> None:
    site_packages = tmp_path / "venv" / "site-packages"
    installed = site_packages / "aipcs_mcp" / "__init__.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    source_import = source / "aipcs_mcp" / "__init__.py"
    source_import.parent.mkdir()
    source_import.write_text("", encoding="utf-8")
    assert verifier.origin_is_isolated(installed, (site_packages,), (source,), ("/usr/lib",))
    assert not verifier.origin_is_isolated(
        source_import, (site_packages,), (source,), ("/usr/lib",)
    )
    assert not verifier.origin_is_isolated(installed, (site_packages,), (source,), (str(source),))


def test_wheel_and_sdist_origin_probes_use_distinct_working_directories(
    monkeypatch, tmp_path: Path, verifier
) -> None:
    working_directories: list[Path] = []

    def fake_stage(*_args, cwd, **_kwargs):
        working_directories.append(cwd)
        return verifier.StageResult("installed import origin")

    monkeypatch.setattr(verifier, "run_stage", fake_stage)
    for name in ("wheel", "sdist"):
        verifier.prove_site_packages_import(
            tmp_path / f"{name}-venv" / "bin" / "python",
            tmp_path,
            tmp_path / "source",
            tmp_path / "copy",
            name=name,
            environment={},
            redaction_roots=(),
        )

    assert working_directories == [
        tmp_path / "wheel-origin-probe",
        tmp_path / "sdist-origin-probe",
    ]


def test_embedded_client_is_syntactically_valid(verifier) -> None:
    program = verifier._smoke_client_program()
    compile(program, "installed_smoke_client.py", "exec")
    assert "metadata_fields" in program
    assert 'features"]["registry_lifecycle"] is True' in program
    assert "design_replay == designed" in program


def test_embedded_catalog_client_is_syntactically_valid(verifier) -> None:
    program = verifier._catalog_smoke_program()
    compile(program, "installed_catalog_smoke.py", "exec")
    assert "aipcs_mcp.storage.sqlite.service_store" in program
    assert 'mode == "heavy"' in program
    assert 'substituted.status == "incompatible"' in program
    assert "site.getsitepackages()" in program


def test_embedded_catalog_client_exercises_the_source_contract(
    monkeypatch, tmp_path: Path, verifier
) -> None:
    root = tmp_path / "catalog-root"
    monkeypatch.setattr("site.getsitepackages", lambda: [str(ROOT / "src")])
    monkeypatch.setattr(sys, "argv", ["installed_catalog_smoke.py", str(root), "heavy"])
    exec(compile(verifier._catalog_smoke_program(), "installed_catalog_smoke.py", "exec"), {})
    assert (root / "service-stores").is_dir()
    monkeypatch.setattr(sys, "argv", ["installed_catalog_smoke.py", str(root), "restart"])
    with pytest.raises(SystemExit) as restarted:
        exec(compile(verifier._catalog_smoke_program(), "installed_catalog_smoke.py", "exec"), {})
    assert restarted.value.code == 0


def test_embedded_domain_schema_client_exercises_the_source_contract(
    monkeypatch, tmp_path: Path, verifier
) -> None:
    program = verifier._domain_schema_smoke_program()
    compile(program, "installed_domain_schema_smoke.py", "exec")
    assert "SQLiteDomainSchemaStore" in program
    assert 'catalog.migrate(locator).status == "ready"' in program
    assert 'ordered == ("title", "quantity")' in program
    assert "site.getsitepackages()" in program

    root = tmp_path / "domain-schema-root"
    monkeypatch.setattr("site.getsitepackages", lambda: [str(ROOT / "src")])
    monkeypatch.setattr(sys, "argv", ["installed_domain_schema_smoke.py", str(root)])
    exec(compile(program, "installed_domain_schema_smoke.py", "exec"), {})
    assert (root / "service-stores").is_dir()


def test_embedded_relational_client_exercises_the_source_contract(
    monkeypatch, verifier
) -> None:
    program = verifier._relational_contract_smoke_program()
    compile(program, "installed_relational_contract_smoke.py", "exec")
    assert "aipcs_mcp.relational" in program
    assert "DomainSchemaStore" in program
    assert "site.getsitepackages()" in program

    monkeypatch.setattr("site.getsitepackages", lambda: [str(ROOT / "src")])
    exec(compile(program, "installed_relational_contract_smoke.py", "exec"), {})


def test_catalog_smoke_uses_each_installed_interpreter_and_external_cwd(
    monkeypatch, tmp_path: Path, verifier
) -> None:
    calls: list[tuple[str, tuple[str, ...], Path]] = []

    def fake_stage(label, args, *, cwd, **kwargs):
        calls.append((label, tuple(map(str, args)), cwd))
        return verifier.StageResult(label)

    monkeypatch.setattr(verifier, "run_stage", fake_stage)
    verifier.run_installed_catalog_smoke(
        tmp_path / "wheel-venv" / "bin" / "python",
        tmp_path,
        "wheel",
        heavy=True,
        environment={},
        redaction_roots=(),
    )
    verifier.run_installed_catalog_smoke(
        tmp_path / "sdist-venv" / "bin" / "python",
        tmp_path,
        "sdist",
        heavy=False,
        environment={},
        redaction_roots=(),
    )
    assert [label for label, _, _ in calls] == [
        "installed wheel catalog smoke",
        "installed wheel catalog restart",
        "installed sdist catalog smoke",
        "installed sdist catalog restart",
    ]
    assert calls[0][1][0].endswith("wheel-venv/bin/python")
    assert calls[1][1][0].endswith("wheel-venv/bin/python")
    assert calls[2][1][0].endswith("sdist-venv/bin/python")
    assert calls[3][1][0].endswith("sdist-venv/bin/python")
    assert all(call[1][1] == "-I" for call in calls)
    assert calls[0][1][-1] == "heavy"
    assert calls[1][1][-1] == "restart"
    assert calls[2][1][-1] == "baseline"
    assert calls[3][1][-1] == "restart"
    assert calls[0][2] == tmp_path / "wheel-catalog-cwd"
    assert calls[1][2] == tmp_path / "wheel-catalog-cwd"
    assert calls[2][2] == tmp_path / "sdist-catalog-cwd"
    assert calls[3][2] == tmp_path / "sdist-catalog-cwd"
    assert calls[0][2] != calls[2][2]


def test_domain_schema_smoke_uses_each_installed_interpreter_and_external_cwd(
    monkeypatch, tmp_path: Path, verifier
) -> None:
    calls: list[tuple[str, tuple[str, ...], Path]] = []

    def fake_stage(label, args, *, cwd, **kwargs):
        calls.append((label, tuple(map(str, args)), cwd))
        return verifier.StageResult(label)

    monkeypatch.setattr(verifier, "run_stage", fake_stage)
    for name in ("wheel", "sdist"):
        verifier.run_installed_domain_schema_smoke(
            tmp_path / f"{name}-venv" / "bin" / "python",
            tmp_path,
            name,
            environment={},
            redaction_roots=(),
        )

    assert [label for label, _, _ in calls] == [
        "installed wheel domain schema smoke",
        "installed sdist domain schema smoke",
    ]
    assert calls[0][1][0].endswith("wheel-venv/bin/python")
    assert calls[1][1][0].endswith("sdist-venv/bin/python")
    assert all(args[1] == "-I" for _, args, _ in calls)
    assert calls[0][2] == tmp_path / "wheel-domain-schema-cwd"
    assert calls[1][2] == tmp_path / "sdist-domain-schema-cwd"
    assert calls[0][2] != calls[1][2]


def test_relational_smoke_uses_each_installed_interpreter_and_external_cwd(
    monkeypatch, tmp_path: Path, verifier
) -> None:
    calls: list[tuple[str, tuple[str, ...], Path]] = []

    def fake_stage(label, args, *, cwd, **kwargs):
        calls.append((label, tuple(map(str, args)), cwd))
        return verifier.StageResult(label)

    monkeypatch.setattr(verifier, "run_stage", fake_stage)
    for name in ("wheel", "sdist"):
        verifier.run_installed_relational_contract_smoke(
            tmp_path / f"{name}-venv" / "bin" / "python",
            tmp_path,
            name,
            environment={},
            redaction_roots=(),
        )

    assert [label for label, _, _ in calls] == [
        "installed wheel relational contract smoke",
        "installed sdist relational contract smoke",
    ]
    assert all(args[1] == "-I" for _, args, _ in calls)
    assert calls[0][2] == tmp_path / "wheel-relational-contract-cwd"
    assert calls[1][2] == tmp_path / "sdist-relational-contract-cwd"
    assert calls[0][2] != calls[1][2]


def test_main_hides_unexpected_exception_details(monkeypatch, capsys, verifier) -> None:
    def unexpected(**_: object) -> None:
        raise RuntimeError("private source path and stack detail")

    monkeypatch.setattr(verifier, "verify_release", unexpected)
    assert verifier.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "release verification failed: unexpected internal failure.\n"


def test_copy_side_gates_are_direct_and_never_reinvoke_verifier(
    monkeypatch, tmp_path: Path, verifier
) -> None:
    calls: list[tuple[str, tuple[str, ...], Path]] = []

    def fake_stage(label, args, *, cwd, **kwargs):
        calls.append((label, tuple(map(str, args)), cwd))
        return verifier.StageResult(label)

    monkeypatch.setattr(verifier, "run_stage", fake_stage)
    verifier.run_source_gates(
        tmp_path,
        uv="uv",
        environment={},
        redaction_roots=(),
    )
    assert calls
    assert all("verify_release.py" not in arguments for _, arguments, _ in calls)
    assert all(cwd == tmp_path for _, _, cwd in calls)
    assert "git diff --cached --check" in [label for label, _, _ in calls]
    pytest_call = next(arguments for label, arguments, _ in calls if label == "pytest")
    ruff_call = next(arguments for label, arguments, _ in calls if label == "ruff")
    assert pytest_call[-2:] == ("-p", "no:cacheprovider")
    assert ruff_call[-1] == "--no-cache"
