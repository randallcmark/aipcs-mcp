"""SYNTHETIC_FIXTURE. Secure CLI export/import descriptor behavior."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from aipcs_mcp.admin_files import (
    AdminFileError,
    ExclusiveExportFile,
    open_import_file,
)


def _temp_files(parent: Path) -> list[Path]:
    return sorted(parent.glob(".aipcs-export-*.tmp"))


def test_export_publishes_private_regular_file_without_temp_residue(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "bundle.jsonl"

    with ExclusiveExportFile(str(destination)) as output:
        output.stream.write(b"portable\n")
        assert not destination.exists()
        assert len(_temp_files(tmp_path)) == 1
        output.publish()

    assert destination.read_bytes() == b"portable\n"
    observed = destination.stat()
    assert stat.S_ISREG(observed.st_mode)
    assert stat.S_IMODE(observed.st_mode) == 0o600
    assert _temp_files(tmp_path) == []


def test_export_never_overwrites_existing_file_or_symlink(tmp_path: Path) -> None:
    destination = tmp_path / "bundle.jsonl"
    destination.write_bytes(b"existing")
    with (
        pytest.raises(AdminFileError) as captured,
        ExclusiveExportFile(str(destination)),
    ):
        pass
    assert captured.value.code == "already_exists"
    assert destination.read_bytes() == b"existing"

    destination.unlink()
    target = tmp_path / "target"
    target.write_bytes(b"target")
    destination.symlink_to(target)
    with (
        pytest.raises(AdminFileError) as captured,
        ExclusiveExportFile(str(destination)),
    ):
        pass
    assert captured.value.code == "already_exists"
    assert target.read_bytes() == b"target"


def test_export_failure_removes_only_its_private_temporary_file(
    tmp_path: Path,
) -> None:
    unrelated = tmp_path / ".aipcs-export-unrelated.tmp"
    unrelated.write_bytes(b"keep")

    with (
        pytest.raises(RuntimeError),
        ExclusiveExportFile(str(tmp_path / "bundle.jsonl")) as output,
    ):
        output.stream.write(b"partial")
        raise RuntimeError("controlled")

    assert unrelated.read_bytes() == b"keep"
    assert sorted(tmp_path.iterdir()) == [unrelated]


def test_export_publish_race_preserves_existing_destination(tmp_path: Path) -> None:
    destination = tmp_path / "bundle.jsonl"

    with ExclusiveExportFile(str(destination)) as output:
        output.stream.write(b"ours")
        destination.write_bytes(b"peer")
        with pytest.raises(AdminFileError) as captured:
            output.publish()
        assert captured.value.code == "already_exists"

    assert destination.read_bytes() == b"peer"
    assert _temp_files(tmp_path) == []


def test_import_opens_only_existing_regular_non_symlink_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bundle.jsonl"
    source.write_bytes(b"portable\n")
    with open_import_file(str(source)) as stream:
        assert stream.read() == b"portable\n"

    link = tmp_path / "link.jsonl"
    link.symlink_to(source)
    with pytest.raises(AdminFileError) as captured, open_import_file(str(link)):
        pass
    assert captured.value.code == "not_regular"

    with pytest.raises(AdminFileError) as captured, open_import_file(str(tmp_path)):
        pass
    assert captured.value.code == "not_regular"

    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(AdminFileError) as captured, open_import_file(str(fifo)):
        pass
    assert captured.value.code == "not_regular"


def test_import_missing_file_and_invalid_paths_are_bounded(tmp_path: Path) -> None:
    with (
        pytest.raises(AdminFileError) as captured,
        open_import_file(str(tmp_path / "missing")),
    ):
        pass
    assert captured.value.code == "unavailable"

    with pytest.raises(AdminFileError) as captured, open_import_file(""):
        pass
    assert captured.value.code == "invalid_path"


def test_export_descriptor_is_closed_after_context(tmp_path: Path) -> None:
    output = ExclusiveExportFile(str(tmp_path / "bundle.jsonl"))
    with output:
        descriptor = output.stream.fileno()
        output.stream.write(b"x")
    with pytest.raises(OSError):
        os.fstat(descriptor)
