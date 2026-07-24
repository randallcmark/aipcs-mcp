"""Private CLI file descriptors for safe portable export and import."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4


class AdminFileError(Exception):
    """Bounded file-boundary failure which never retains a caller path."""

    def __init__(self, code: str) -> None:
        if code not in {
            "invalid_path",
            "already_exists",
            "not_regular",
            "unavailable",
            "publication_uncertain",
        }:
            raise ValueError("Administration file error is invalid.")
        self.code = code
        super().__init__("Administration file operation failed.")


class ExclusiveExportFile:
    """One owner-private temporary file published atomically without replacement."""

    def __init__(self, path_text: str) -> None:
        self._parent_fd = -1
        self._stream: BinaryIO | None = None
        self._temp_name: str | None = None
        self._final_name: str | None = None
        self._published = False
        path = _path(path_text)
        self._parent = path.parent if str(path.parent) else Path(".")
        self._final_name = path.name

    def __enter__(self) -> ExclusiveExportFile:
        flags = os.O_RDONLY | _required_flag("O_DIRECTORY") | _required_flag("O_CLOEXEC")
        try:
            self._parent_fd = os.open(self._parent, flags)
            _require_absent(self._parent_fd, self._final_name)
            self._temp_name = f".aipcs-export-{uuid4().hex}.tmp"
            file_flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | _required_flag("O_CLOEXEC")
                | _required_flag("O_NOFOLLOW")
            )
            descriptor = os.open(
                self._temp_name,
                file_flags,
                0o600,
                dir_fd=self._parent_fd,
            )
            os.fchmod(descriptor, 0o600)
            self._stream = os.fdopen(descriptor, "w+b", closefd=True)
            return self
        except AdminFileError:
            self._cleanup()
            raise
        except FileExistsError:
            self._cleanup()
            raise AdminFileError("already_exists") from None
        except OSError:
            self._cleanup()
            raise AdminFileError("unavailable") from None

    @property
    def stream(self) -> BinaryIO:
        if self._stream is None or self._stream.closed or self._published:
            raise AdminFileError("unavailable")
        return self._stream

    def publish(self) -> None:
        if (
            self._stream is None
            or self._stream.closed
            or self._temp_name is None
            or self._final_name is None
            or self._parent_fd < 0
            or self._published
        ):
            raise AdminFileError("unavailable")
        try:
            self._stream.flush()
            os.fsync(self._stream.fileno())
            os.link(
                self._temp_name,
                self._final_name,
                src_dir_fd=self._parent_fd,
                dst_dir_fd=self._parent_fd,
                follow_symlinks=False,
            )
            self._published = True
            os.unlink(self._temp_name, dir_fd=self._parent_fd)
            self._temp_name = None
            _fsync_directory(self._parent_fd)
        except FileExistsError:
            raise AdminFileError("already_exists") from None
        except OSError:
            code = "publication_uncertain" if self._published else "unavailable"
            raise AdminFileError(code) from None

    def __exit__(self, *_: object) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        if self._stream is not None:
            with suppress(OSError):
                self._stream.close()
            self._stream = None
        if self._temp_name is not None and self._parent_fd >= 0:
            with suppress(OSError):
                os.unlink(self._temp_name, dir_fd=self._parent_fd)
            self._temp_name = None
        if self._parent_fd >= 0:
            with suppress(OSError):
                os.close(self._parent_fd)
            self._parent_fd = -1


@contextmanager
def open_import_file(path_text: str) -> Iterator[BinaryIO]:
    """Open one existing regular file read-only without following its final symlink."""

    path = _path(path_text)
    flags = (
        os.O_RDONLY
        | _required_flag("O_CLOEXEC")
        | _required_flag("O_NOFOLLOW")
        | _required_flag("O_NONBLOCK")
    )
    descriptor = -1
    stream: BinaryIO | None = None
    try:
        descriptor = os.open(path, flags)
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise AdminFileError("not_regular")
        stream = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = -1
        yield stream
    except AdminFileError:
        raise
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.EFTYPE if hasattr(errno, "EFTYPE") else -1}:
            raise AdminFileError("not_regular") from None
        raise AdminFileError("unavailable") from None
    finally:
        if stream is not None:
            with suppress(OSError):
                stream.close()
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)


def _path(value: object) -> Path:
    if (
        type(value) is not str
        or not value
        or "\x00" in value
        or len(value) > 4_096
    ):
        raise AdminFileError("invalid_path")
    path = Path(value)
    if path.name in {"", ".", ".."}:
        raise AdminFileError("invalid_path")
    return path


def _required_flag(name: str) -> int:
    value = getattr(os, name, None)
    if type(value) is not int or value == 0:
        raise AdminFileError("unavailable")
    return value


def _require_absent(parent_fd: int, name: str | None) -> None:
    if name is None:
        raise AdminFileError("invalid_path")
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise AdminFileError("unavailable") from None
    raise AdminFileError("already_exists")


def _fsync_directory(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in {
            errno.EINVAL,
            errno.EBADF,
            errno.EROFS,
            getattr(errno, "ENOTSUP", errno.EINVAL),
        }:
            raise
