"""Private bounded stream and spool boundary for portable bundles."""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from typing import BinaryIO

from aipcs_mcp.application.portable_codec import (
    OrderedBundleDigests,
    decode_canonical_json_object,
    decode_non_trailer_frame,
    decode_trailer,
)
from aipcs_mcp.application.portable_models import (
    MAX_BUNDLE_FRAMES,
    MAX_FRAME_BYTES,
    PortableBundleContractError,
    PortableBundleLimits,
    PortableBundleSummary,
    PortableHeader,
)

_SPOOL_NAME = "bundle.jsonl"


@dataclass(repr=False)
class ValidatedPortableBundle:
    """An owned private spool and its payload-free verified summary."""

    summary: PortableBundleSummary
    _stream: BinaryIO
    _directory: str
    _closed: bool = False

    def __repr__(self) -> str:
        return "ValidatedPortableBundle(<redacted>)"

    def __enter__(self) -> ValidatedPortableBundle:
        if self._closed:
            raise PortableBundleContractError()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(Exception):
            self._stream.close()
        spool = os.path.join(self._directory, _SPOOL_NAME)
        with suppress(Exception):
            os.unlink(spool)
        with suppress(Exception):
            os.rmdir(self._directory)

    def iter_lines(self) -> Iterator[bytes]:
        """Yield the already validated exact frame bytes without exposing a path."""

        if self._closed:
            raise PortableBundleContractError()
        try:
            self._stream.seek(0)
            while True:
                line = self._stream.readline(MAX_FRAME_BYTES + 1)
                if not line:
                    return
                if type(line) is not bytes or len(line) > MAX_FRAME_BYTES:
                    raise PortableBundleContractError()
                yield line
        except PortableBundleContractError:
            raise
        except Exception:
            raise PortableBundleContractError() from None

    def __del__(self) -> None:
        with suppress(Exception):
            self.close()


def validate_and_spool_bundle(
    source: BinaryIO,
    *,
    limits: PortableBundleLimits | None = None,
    _temporary_parent: str | None = None,
) -> ValidatedPortableBundle:
    """Incrementally validate an untrusted binary JSONL stream into a private spool."""

    limits = limits or PortableBundleLimits()
    if type(limits) is not PortableBundleLimits:
        raise PortableBundleContractError()
    directory: str | None = None
    spool: BinaryIO | None = None
    try:
        directory = tempfile.mkdtemp(prefix="aipcs-bundle-", dir=_temporary_parent)
        _verify_private_directory(directory)
        descriptor = _create_spool_descriptor(directory)
        try:
            spool = os.fdopen(descriptor, "w+b")
        except Exception:
            with suppress(Exception):
                os.close(descriptor)
            raise PortableBundleContractError() from None
        _verify_private_file(spool)

        digests = OrderedBundleDigests()
        header: PortableHeader | None = None
        total_bytes = 0
        while True:
            line = _read_frame(source)
            total_bytes += len(line)
            if total_bytes > limits.maximum_total_bytes:
                raise PortableBundleContractError()
            if digests.frame_count + 1 > MAX_BUNDLE_FRAMES:
                raise PortableBundleContractError()
            decoded = decode_canonical_json_object(line[:-1])
            if decoded.get("kind") == "trailer":
                trailer = decode_trailer(decoded)
                if digests.frame_count + 1 > MAX_BUNDLE_FRAMES:
                    raise PortableBundleContractError()
                digests.verify(trailer)
                if header is None:
                    raise PortableBundleContractError()
                _require_end(source)
                _write_exact(spool, line)
                spool.flush()
                spool.seek(0)
                summary = PortableBundleSummary(
                    header=header,
                    sections=trailer.sections,
                    root_sha256=trailer.root_sha256,
                    pre_trailer_byte_count=trailer.pre_trailer_byte_count,
                    total_byte_count=total_bytes,
                    frame_count=trailer.ordinal + 1,
                )
                result = ValidatedPortableBundle(summary, spool, directory)
                spool = None
                directory = None
                return result

            frame = decode_non_trailer_frame(decoded)
            digests.add(frame, line)
            if frame.kind == "header":
                header = PortableHeader.from_payload(frame.payload)
            _write_exact(spool, line)
    except PortableBundleContractError:
        raise
    except Exception:
        raise PortableBundleContractError() from None
    finally:
        if spool is not None:
            with suppress(Exception):
                spool.close()
        if directory is not None:
            with suppress(Exception):
                os.unlink(os.path.join(directory, _SPOOL_NAME))
            with suppress(Exception):
                os.rmdir(directory)


def _read_frame(source: BinaryIO) -> bytes:
    try:
        line = source.readline(MAX_FRAME_BYTES + 1)
    except Exception:
        raise PortableBundleContractError() from None
    if (
        type(line) is not bytes
        or not line
        or len(line) > MAX_FRAME_BYTES
        or not line.endswith(b"\n")
        or line == b"\n"
        or line.endswith(b"\r\n")
    ):
        raise PortableBundleContractError()
    return line


def _require_end(source: BinaryIO) -> None:
    try:
        extra = source.read(1)
    except Exception:
        raise PortableBundleContractError() from None
    if type(extra) is not bytes or extra:
        raise PortableBundleContractError()


def _verify_private_directory(directory: str) -> None:
    try:
        evidence = os.lstat(directory)
        if (
            not stat.S_ISDIR(evidence.st_mode)
            or stat.S_IMODE(evidence.st_mode) != 0o700
            or evidence.st_uid != os.geteuid()
        ):
            raise PortableBundleContractError()
    except PortableBundleContractError:
        raise
    except Exception:
        raise PortableBundleContractError() from None


def _verify_private_file(stream: BinaryIO) -> None:
    try:
        evidence = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(evidence.st_mode)
            or stat.S_IMODE(evidence.st_mode) != 0o600
            or evidence.st_uid != os.geteuid()
            or evidence.st_nlink != 1
        ):
            raise PortableBundleContractError()
    except PortableBundleContractError:
        raise
    except Exception:
        raise PortableBundleContractError() from None


def _write_exact(stream: BinaryIO, value: bytes) -> None:
    try:
        written = stream.write(value)
    except Exception:
        raise PortableBundleContractError() from None
    if type(written) is not int or written != len(value):
        raise PortableBundleContractError()


def _create_spool_descriptor(directory: str) -> int:
    directory_descriptor: int | None = None
    try:
        directory_descriptor = os.open(
            directory,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        return os.open(
            _SPOOL_NAME,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
    except Exception:
        raise PortableBundleContractError() from None
    finally:
        if directory_descriptor is not None:
            with suppress(Exception):
                os.close(directory_descriptor)
