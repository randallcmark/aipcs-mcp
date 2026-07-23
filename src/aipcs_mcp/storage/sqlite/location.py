"""Descriptor-anchored POSIX location policy for the private registry file."""

from __future__ import annotations

import os
import re
import stat
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from aipcs_mcp.storage.errors import StorageTransientJournal, StorageUnavailable

_BASENAME = "registry.sqlite"
_SERVICE_STORES = "service-stores"
_LOCATOR = re.compile(r"^svc_[0-9a-f]{32}$")
_SQLITE_HEADER = b"SQLite format 3\x00"
_JOURNAL_SUFFIX = "-journal"
_WAL_SUFFIX = "-wal"
_SHM_SUFFIX = "-shm"
_SIDECAR_SUFFIXES = (_JOURNAL_SUFFIX, _WAL_SUFFIX, _SHM_SUFFIX)

type SQLiteHeaderMode = Literal["empty", "delete", "wal", "unknown"]
type _FileIdentity = tuple[int, int]


@dataclass(repr=False)
class AnchoredLocation:
    _root_fd: int
    _root_path: Path
    _database_stat: os.stat_result | None
    journal_present: bool
    _database_name: str = _BASENAME
    _acquired_header_mode: SQLiteHeaderMode | None = None
    _live_sidecars: dict[str, _FileIdentity] | None = None

    def __repr__(self) -> str:
        return "AnchoredLocation(<redacted>)"

    def close(self) -> None:
        os.close(self._root_fd)

    def _sqlite_path(self) -> Path:
        return self._root_path / self._database_name

    def verify_database_identity(self) -> None:
        """Verify the selected main file while no SQLite handle is live."""

        if self._database_stat is None:
            raise StorageUnavailable()
        try:
            path_stat = os.stat(self._root_path, follow_symlinks=False)
            descriptor_stat = os.fstat(self._root_fd)
        except Exception:
            raise StorageUnavailable() from None
        if not stat.S_ISDIR(path_stat.st_mode) or (path_stat.st_dev, path_stat.st_ino) != (
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
        ):
            raise StorageUnavailable()
        current = _open_existing_file(self._root_fd, self._database_name, required=True)
        if current is None or (current.st_dev, current.st_ino) != (
            self._database_stat.st_dev,
            self._database_stat.st_ino,
        ):
            raise StorageUnavailable()

    def verify_live_database_identity(self) -> None:
        """Verify main-file identity without opening a lock-releasing descriptor."""

        if self._database_stat is None:
            raise StorageUnavailable()
        try:
            path_stat = os.stat(self._root_path, follow_symlinks=False)
            descriptor_stat = os.fstat(self._root_fd)
            current = os.stat(
                self._database_name,
                dir_fd=self._root_fd,
                follow_symlinks=False,
            )
            _validate_file_stat(current)
        except Exception:
            raise StorageUnavailable() from None
        if (
            not stat.S_ISDIR(path_stat.st_mode)
            or (path_stat.st_dev, path_stat.st_ino)
            != (descriptor_stat.st_dev, descriptor_stat.st_ino)
            or (current.st_dev, current.st_ino)
            != (self._database_stat.st_dev, self._database_stat.st_ino)
        ):
            raise StorageUnavailable()

    def database_header_mode(self) -> SQLiteHeaderMode:
        """Read the selected main-file header without following another pathname."""

        if self._database_stat is None:
            raise StorageUnavailable()
        descriptor: int | None = None
        try:
            descriptor = os.open(self._database_name, _file_flags(), dir_fd=self._root_fd)
            current = os.fstat(descriptor)
            _validate_file_stat(current)
            if (current.st_dev, current.st_ino) != (
                self._database_stat.st_dev,
                self._database_stat.st_ino,
            ):
                raise StorageUnavailable()
            header = os.pread(descriptor, 100, 0)
            return _header_mode(header, current.st_size)
        except Exception:
            raise StorageUnavailable() from None
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)

    def record_connection_header_mode(self, mode: SQLiteHeaderMode) -> None:
        """Cache an exact mode reported by an identity-verified SQLite handle."""

        if mode not in {"delete", "wal"}:
            raise StorageUnavailable()
        self._acquired_header_mode = mode

    def adopt_live_sidecars(self, *, allow_journal: bool) -> None:
        """Record one safe observation of SQLite-owned operational siblings."""

        self._live_sidecars = _live_sidecars(
            self._root_fd,
            self._database_name,
            self._database_stat,
            self._acquired_header_mode,
            allow_journal=allow_journal,
        )

    def verify_live_sidecars(self, *, allow_journal: bool) -> None:
        if self._live_sidecars is None:
            raise StorageUnavailable()
        # A cooperating peer may legitimately unlink a checkpointed WAL/SHM
        # pathname while this process retains SQLite's open descriptor, then
        # recreate it for a later connection.  Python does not expose those
        # SQLite descriptors, so path identity cannot be pinned across peers.
        # Revalidate the complete metadata boundary and retain the newest
        # observation instead.
        self._live_sidecars = _live_sidecars(
            self._root_fd,
            self._database_name,
            self._database_stat,
            self._acquired_header_mode,
            allow_journal=allow_journal,
        )

    def validate_sidecars(self, *, allow_journal: bool) -> None:
        """Validate current sidecar metadata without changing the observation."""

        _live_sidecars(
            self._root_fd,
            self._database_name,
            self._database_stat,
            self._acquired_header_mode,
            allow_journal=allow_journal,
        )

    def verify_closed_sidecars(self, *, allow_journal: bool) -> None:
        """Accept absence or fresh safe identities after SQLite has closed."""

        _sidecars(
            self._root_fd,
            self._database_name,
            self._database_stat,
            allow_journal=allow_journal,
        )
        self._live_sidecars = None


class SQLiteLocationPolicy:
    """Validate one resolver-selected root without reading configuration/environment."""

    def __init__(self, root: Path) -> None:
        self._initialise(root, "explicit")

    @classmethod
    def from_resolved(cls, root: Path, source: str) -> SQLiteLocationPolicy:
        """Construct from `ResolvedConfiguration.sources['sqlite_data_root']`."""

        if source not in {"cli", "environment", "file", "default"}:
            raise StorageUnavailable()
        value = cls.__new__(cls)
        value._initialise(root, "default" if source == "default" else "explicit")
        return value

    def _initialise(self, root: Path, mode: str) -> None:
        _require_posix()
        if not isinstance(root, Path):
            raise StorageUnavailable()
        text = str(root)
        if (
            not root.is_absolute()
            or root == Path(root.anchor)
            or any(part in {".", ".."} for part in root.parts)
            or not text.isprintable()
            or "\x00" in text
            or text.startswith(("file:", "//", "\\\\"))
        ):
            raise StorageUnavailable()
        self._root = root
        self._mode = mode
        self._anchor, self._components = _layout(root, mode)

    def __repr__(self) -> str:
        return "SQLiteLocationPolicy(<redacted>)"

    def acquire(self, *, create_root: bool, allow_journal: bool) -> AnchoredLocation | None:
        _require_posix()
        root_fd = self._acquire_root(create=create_root)
        if root_fd is None:
            return None
        try:
            _require_private_root(root_fd)
            database_stat = _open_existing_file(root_fd, _BASENAME, required=False)
            sidecars = _sidecars(
                root_fd,
                _BASENAME,
                database_stat,
                allow_journal=allow_journal,
            )
            header_mode = (
                _database_header_mode(root_fd, _BASENAME, database_stat)
                if database_stat is not None
                else None
            )
            return AnchoredLocation(
                root_fd,
                self._root,
                database_stat,
                _JOURNAL_SUFFIX in sidecars,
                _BASENAME,
                header_mode,
            )
        except StorageTransientJournal:
            os.close(root_fd)
            failure = StorageTransientJournal()
        except Exception:
            os.close(root_fd)
            failure = StorageUnavailable()
        raise failure from None

    def acquire_service_store(
        self, namespace: str, *, create: bool, allow_journal: bool
    ) -> AnchoredLocation | None:
        """Acquire one fixed-name service-store file below the private container."""

        if type(namespace) is not str or not _LOCATOR.fullmatch(namespace):
            raise StorageUnavailable()
        root_fd = self._acquire_root(create=create)
        if root_fd is None:
            return None
        container_fd: int | None = None
        try:
            _require_private_root(root_fd)
            container_fd = _open_child_directory(root_fd, _SERVICE_STORES, create=create)
            if container_fd is None:
                return None
            _require_owner_directory(container_fd)
            database_name = namespace + ".sqlite"
            database_stat = _open_existing_file(container_fd, database_name, required=False)
            sidecars = _sidecars(
                container_fd,
                database_name,
                database_stat,
                allow_journal=allow_journal,
            )
            header_mode = (
                _database_header_mode(container_fd, database_name, database_stat)
                if database_stat is not None
                else None
            )
            os.close(root_fd)
            root_fd = -1
            result = AnchoredLocation(
                container_fd,
                self._root / _SERVICE_STORES,
                database_stat,
                _JOURNAL_SUFFIX in sidecars,
                database_name,
                header_mode,
            )
            container_fd = None
            return result
        except StorageTransientJournal:
            failure = StorageTransientJournal()
        except Exception:
            failure = StorageUnavailable()
        finally:
            if container_fd is not None:
                with suppress(OSError):
                    os.close(container_fd)
            if root_fd >= 0:
                with suppress(OSError):
                    os.close(root_fd)
        raise failure from None

    def _acquire_root(self, *, create: bool) -> int | None:
        if self._mode == "explicit":
            return _explicit_root(self._root, create=create)
        return _walk_directories(
            self._anchor,
            self._components,
            create=create,
            missing_ok=not create,
        )


def _explicit_root(root: Path, *, create: bool) -> int | None:
    parent_fd = _open_safe_directory(root.parent)
    try:
        try:
            return os.open(root.name, _directory_flags(), dir_fd=parent_fd)
        except FileNotFoundError:
            if not create:
                return None
            with suppress(FileExistsError):
                os.mkdir(root.name, 0o700, dir_fd=parent_fd)
            return os.open(root.name, _directory_flags(), dir_fd=parent_fd)
    except Exception:
        failure = StorageUnavailable()
    finally:
        os.close(parent_fd)
    raise failure from None


def _layout(root: Path, mode: str) -> tuple[Path, tuple[str, ...]]:
    if mode == "explicit":
        return root.parent, (root.name,)
    if sys.platform == "darwin":
        suffix = ("Library", "Application Support", "aipcs-mcp")
        if tuple(root.parts[-3:]) != suffix:
            raise StorageUnavailable()
        return root.parents[2], suffix
    if sys.platform.startswith("linux"):
        fallback = (".local", "share", "aipcs-mcp")
        if tuple(root.parts[-3:]) == fallback:
            return root.parents[2], fallback
        if root.name != "aipcs-mcp":
            raise StorageUnavailable()
        return root.parent, (root.name,)
    raise StorageUnavailable()


def _walk_directories(
    anchor: Path, components: tuple[str, ...], *, create: bool, missing_ok: bool
) -> int | None:
    descriptor = _open_safe_directory(anchor)
    try:
        for component in components:
            try:
                child = os.open(component, _directory_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    if missing_ok:
                        os.close(descriptor)
                        return None
                    raise
                with suppress(FileExistsError):
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                child = os.open(component, _directory_flags(), dir_fd=descriptor)
            value = os.fstat(child)
            if (
                not stat.S_ISDIR(value.st_mode)
                or value.st_uid != os.geteuid()
                or value.st_mode & 0o022
            ):
                os.close(child)
                raise ValueError
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        with suppress(OSError):
            os.close(descriptor)
        failure = StorageUnavailable()
        raise failure from None


def _open_child_directory(parent_fd: int, name: str, *, create: bool) -> int | None:
    try:
        child = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            return None
        try:
            with suppress(FileExistsError):
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            child = os.open(name, _directory_flags(), dir_fd=parent_fd)
        except Exception:
            raise StorageUnavailable() from None
    try:
        _require_owner_directory(child)
        return child
    except Exception:
        with suppress(OSError):
            os.close(child)
        raise StorageUnavailable() from None


def _require_owner_directory(descriptor: int) -> None:
    value = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.geteuid()
        or stat.S_IMODE(value.st_mode) != 0o700
    ):
        raise StorageUnavailable()


def _require_private_root(descriptor: int) -> None:
    _require_owner_directory(descriptor)


def _require_posix() -> None:
    required = (
        "O_DIRECTORY",
        "O_NOFOLLOW",
        "O_CLOEXEC",
        "supports_dir_fd",
        "supports_follow_symlinks",
        "geteuid",
        "pread",
    )
    if os.name != "posix" or any(not hasattr(os, name) for name in required):
        raise StorageUnavailable()
    if (
        os.open not in os.supports_dir_fd
        or os.mkdir not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
    ):
        raise StorageUnavailable()


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _file_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)


def _open_safe_directory(path: Path) -> int:
    descriptor: int | None = None
    try:
        if not path.is_absolute():
            raise ValueError
        descriptor = os.open(path.anchor, _directory_flags())
        for component in path.parts[1:]:
            child = os.open(component, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        value = os.fstat(descriptor)
        if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.geteuid() or value.st_mode & 0o022:
            raise ValueError
        return descriptor
    except Exception:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        failure = StorageUnavailable()
    raise failure from None


def create_database(root_fd: int, name: str = _BASENAME) -> os.stat_result:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=root_fd,
        )
        value = os.fstat(descriptor)
        _validate_file_stat(value)
        return value
    except FileExistsError:
        existing = _open_existing_file(root_fd, name, required=True)
        if existing is None:
            failure = StorageUnavailable()
        else:
            return existing
    except Exception:
        failure = StorageUnavailable()
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
    raise failure from None


def _open_existing_file(root_fd: int, name: str, *, required: bool) -> os.stat_result | None:
    descriptor: int | None = None
    try:
        descriptor = os.open(name, _file_flags(), dir_fd=root_fd)
        value = os.fstat(descriptor)
        _validate_file_stat(value)
        return value
    except FileNotFoundError:
        if not required:
            return None
        failure = StorageUnavailable()
    except Exception:
        failure = StorageUnavailable()
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
    raise failure from None


def _validate_file_stat(value: os.stat_result) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.geteuid()
        or stat.S_IMODE(value.st_mode) != 0o600
        or value.st_nlink != 1
    ):
        raise StorageUnavailable()


def _header_mode(header: bytes, size: int) -> SQLiteHeaderMode:
    if size == 0:
        return "empty"
    if len(header) < 100 or header[:16] != _SQLITE_HEADER:
        return "unknown"
    if header[18:20] == b"\x01\x01":
        return "delete"
    if header[18:20] == b"\x02\x02":
        return "wal"
    return "unknown"


def _sidecars(
    root_fd: int,
    database_name: str,
    database_stat: os.stat_result | None,
    *,
    allow_journal: bool,
) -> dict[str, _FileIdentity]:
    found: dict[str, _FileIdentity] = {}
    for suffix in _SIDECAR_SUFFIXES:
        name = database_name + suffix
        value = _open_existing_file(root_fd, name, required=False)
        if value is None:
            continue
        found[suffix] = (value.st_dev, value.st_ino)
    if found and database_stat is None:
        raise StorageUnavailable()
    header_mode = (
        _database_header_mode(root_fd, database_name, database_stat)
        if found
        else None
    )
    if (_WAL_SUFFIX in found or _SHM_SUFFIX in found) and header_mode != "wal":
        raise StorageUnavailable()
    if _JOURNAL_SUFFIX in found and (
        _WAL_SUFFIX in found
        or _SHM_SUFFIX in found
        or header_mode == "wal"
    ):
        raise StorageUnavailable()
    if _JOURNAL_SUFFIX in found and not allow_journal:
        if header_mode == "delete":
            raise StorageTransientJournal()
        raise StorageUnavailable()
    return found


def _live_sidecars(
    root_fd: int,
    database_name: str,
    database_stat: os.stat_result | None,
    acquired_header_mode: SQLiteHeaderMode | None,
    *,
    allow_journal: bool,
) -> dict[str, _FileIdentity]:
    """Validate live path metadata without closing an SQLite-locked file."""

    found: dict[str, _FileIdentity] = {}
    for suffix in _SIDECAR_SUFFIXES:
        try:
            value = os.stat(
                database_name + suffix,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        except Exception:
            raise StorageUnavailable() from None
        _validate_file_stat(value)
        found[suffix] = (value.st_dev, value.st_ino)
    if found and database_stat is None:
        raise StorageUnavailable()
    if _JOURNAL_SUFFIX in found and (
        _WAL_SUFFIX in found or _SHM_SUFFIX in found
    ):
        raise StorageUnavailable()
    if _JOURNAL_SUFFIX in found and not allow_journal:
        if acquired_header_mode == "delete":
            raise StorageTransientJournal()
        raise StorageUnavailable()
    return found


def _database_header_mode(
    root_fd: int,
    database_name: str,
    database_stat: os.stat_result | None,
) -> SQLiteHeaderMode:
    if database_stat is None:
        raise StorageUnavailable()
    descriptor: int | None = None
    try:
        descriptor = os.open(database_name, _file_flags(), dir_fd=root_fd)
        current = os.fstat(descriptor)
        _validate_file_stat(current)
        if (current.st_dev, current.st_ino) != (database_stat.st_dev, database_stat.st_ino):
            raise StorageUnavailable()
        return _header_mode(os.pread(descriptor, 100, 0), current.st_size)
    except Exception:
        raise StorageUnavailable() from None
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
