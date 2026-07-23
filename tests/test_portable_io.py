"""Private stream, limit, permission, and cleanup tests for portable bundles."""

from __future__ import annotations

import io
import stat
from pathlib import Path

import pytest

from aipcs_mcp import portable_io
from aipcs_mcp.application.portable_codec import PortableBundleEncoder
from aipcs_mcp.application.portable_models import (
    MAX_FRAME_BYTES,
    PortableBundleContractError,
    PortableBundleLimits,
    PortableHeader,
)
from aipcs_mcp.portable_io import validate_and_spool_bundle


def _header() -> PortableHeader:
    return PortableHeader(
        export_format_version=1,
        canonicalization="aipcs-json-c14n-1",
        limit_profile="aipcs-bundle-limits-1",
        producer_package="aipcs-mcp",
        producer_package_version="0.0.0.dev0",
        producer_mcp_contract="1.2.0",
        created_at="2026-07-23T20:00:00.000000Z",
        source_backend="sqlite",
    )


def _bundle() -> bytes:
    encoder = PortableBundleEncoder()
    header = encoder.add("header", _header().payload())
    service = encoder.add(
        "service", {"service_id": "5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23"}
    )
    record = encoder.add("record", {"entity_name": "note", "record_id": "record-1"})
    trailer, _ = encoder.finish()
    return header + service + record + trailer


def test_valid_bundle_is_privately_spooled_summarised_and_exactly_cleaned(
    tmp_path: Path,
) -> None:
    artifact = _bundle()
    handle = validate_and_spool_bundle(
        io.BytesIO(artifact), _temporary_parent=str(tmp_path)
    )
    directory = Path(handle._directory)
    spool = directory / "bundle.jsonl"
    assert repr(handle) == "ValidatedPortableBundle(<redacted>)"
    assert handle.summary.total_byte_count == len(artifact)
    assert handle.summary.frame_count == 4
    assert b"".join(handle.iter_lines()) == artifact
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    evidence = spool.stat()
    assert stat.S_ISREG(evidence.st_mode)
    assert stat.S_IMODE(evidence.st_mode) == 0o600
    assert evidence.st_nlink == 1

    handle.close()
    handle.close()
    assert tuple(tmp_path.iterdir()) == ()
    with pytest.raises(PortableBundleContractError):
        tuple(handle.iter_lines())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value[:-1],
        lambda value: value + b"x",
        lambda value: value.replace(b"\n", b"\r\n", 1),
        lambda value: b"\n" + value,
        lambda value: value.replace(b'"ordinal":1', b'"ordinal":2', 1),
        lambda value: value.replace(b'"sha256":"', b'"sha256":"0', 1),
    ],
)
def test_malformed_streams_fail_redacted_and_leave_no_spool(
    tmp_path: Path, mutate
) -> None:
    with pytest.raises(PortableBundleContractError) as failure:
        validate_and_spool_bundle(
            io.BytesIO(mutate(_bundle())), _temporary_parent=str(tmp_path)
        )
    assert str(failure.value) == "The portable bundle is invalid."
    assert tuple(tmp_path.iterdir()) == ()


def test_frame_and_total_limits_include_every_terminating_lf(tmp_path: Path) -> None:
    with pytest.raises(PortableBundleContractError):
        validate_and_spool_bundle(
            io.BytesIO(b"{" + b" " * (MAX_FRAME_BYTES - 1) + b"}\n"),
            _temporary_parent=str(tmp_path),
        )
    assert tuple(tmp_path.iterdir()) == ()

    artifact = _bundle()
    with validate_and_spool_bundle(
        io.BytesIO(artifact),
        limits=PortableBundleLimits(len(artifact)),
        _temporary_parent=str(tmp_path),
    ):
        pass
    assert tuple(tmp_path.iterdir()) == ()
    with pytest.raises(PortableBundleContractError):
        validate_and_spool_bundle(
            io.BytesIO(artifact),
            limits=PortableBundleLimits(len(artifact) - 1),
            _temporary_parent=str(tmp_path),
        )
    assert tuple(tmp_path.iterdir()) == ()


def test_short_spool_write_is_rejected_and_failure_cleanup_is_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ShortWriter:
        def write(self, value: bytes) -> int:
            return len(value) - 1

    with pytest.raises(PortableBundleContractError):
        portable_io._write_exact(ShortWriter(), b"frame\n")  # type: ignore[arg-type]

    def short_write(stream: object, value: bytes) -> None:
        raise PortableBundleContractError()

    monkeypatch.setattr(portable_io, "_write_exact", short_write)
    with pytest.raises(PortableBundleContractError):
        validate_and_spool_bundle(
            io.BytesIO(_bundle()), _temporary_parent=str(tmp_path)
        )
    assert tuple(tmp_path.iterdir()) == ()


def test_reader_uses_bounded_incremental_reads() -> None:
    artifact = _bundle()

    class ObservedStream(io.BytesIO):
        requested: list[tuple[str, int]] = []

        def readline(self, size: int = -1) -> bytes:
            self.requested.append(("line", size))
            return super().readline(size)

        def read(self, size: int = -1) -> bytes:
            self.requested.append(("read", size))
            return super().read(size)

    source = ObservedStream(artifact)
    with validate_and_spool_bundle(source) as handle:
        assert handle.summary.frame_count == 4
    assert all(size == MAX_FRAME_BYTES + 1 for kind, size in source.requested if kind == "line")
    assert [item for item in source.requested if item[0] == "read"] == [("read", 1)]


def test_reserved_audit_frame_is_rejected_before_it_is_retained(tmp_path: Path) -> None:
    encoder = PortableBundleEncoder()
    encoder.add("header", _header().payload())
    encoder.add("service", {"id": "service"})
    with pytest.raises(PortableBundleContractError):
        encoder.add("audit", {"action": "never-portable"})
    assert tuple(tmp_path.iterdir()) == ()
