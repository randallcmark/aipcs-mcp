"""Pure immutable values for the private portable-bundle contract."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

EXPORT_FORMAT_VERSION: Final = 1
CANONICALIZATION_PROFILE: Final = "aipcs-json-c14n-1"
LIMIT_PROFILE: Final = "aipcs-bundle-limits-1"
PRODUCER_PACKAGE: Final = "aipcs"

MAX_JSON_DEPTH: Final = 64
MAX_FRAME_BYTES: Final = 1024 * 1024
DEFAULT_MAX_BUNDLE_BYTES: Final = 256 * 1024 * 1024
ABSOLUTE_MAX_BUNDLE_BYTES: Final = 1024 * 1024 * 1024
MAX_BUNDLE_FRAMES: Final = 1_000_000

SectionKind = Literal[
    "header",
    "service",
    "record",
    "history",
    "branch",
    "branch_membership",
    "audit",
]
NonTrailerKind = SectionKind
FrameKind = Literal[
    "header",
    "service",
    "record",
    "history",
    "branch",
    "branch_membership",
    "audit",
    "trailer",
]
SourceBackend = Literal["sqlite", "postgresql"]

SECTION_KINDS: Final[tuple[SectionKind, ...]] = (
    "header",
    "service",
    "record",
    "history",
    "branch",
    "branch_membership",
    "audit",
)
NON_TRAILER_KINDS: Final[frozenset[str]] = frozenset(SECTION_KINDS)
EMPTY_SHA256: Final = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC_INSTANT = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)


class PortableBundleContractError(ValueError):
    """One fixed, redacted failure for an invalid portable artifact."""

    message = "The portable bundle is invalid."

    def __init__(self) -> None:
        super().__init__(self.message)


type PortableJsonScalar = None | bool | int | float | str
type PortableJsonValue = (
    PortableJsonScalar | tuple[PortableJsonValue, ...] | PortableJsonObject
)


@dataclass(frozen=True, slots=True)
class PortableJsonObject(Mapping[str, PortableJsonValue]):
    """A detached recursively immutable JSON object."""

    entries: tuple[tuple[str, PortableJsonValue], ...]

    def __post_init__(self) -> None:
        if type(self.entries) is not tuple:
            raise PortableBundleContractError()
        names: list[str] = []
        detached: list[tuple[str, PortableJsonValue]] = []
        for entry in self.entries:
            if type(entry) is not tuple or len(entry) != 2:
                raise PortableBundleContractError()
            name, value = entry
            _require_scalar_text(name)
            names.append(name)
            detached.append((name, _freeze_json(value, 1)))
        if len(names) != len(set(names)):
            raise PortableBundleContractError()
        detached.sort(key=lambda item: _utf16_sort_key(item[0]))
        object.__setattr__(self, "entries", tuple(detached))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PortableJsonObject:
        if not isinstance(value, Mapping):
            raise PortableBundleContractError()
        return _freeze_json(dict(value), 0)  # type: ignore[return-value]

    def __getitem__(self, key: str) -> PortableJsonValue:
        for name, value in self.entries:
            if name == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (name for name, _ in self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def to_dict(self) -> dict[str, object]:
        return {name: _thaw_json(value) for name, value in self.entries}


@dataclass(frozen=True, slots=True)
class PortableHeader:
    export_format_version: Literal[1]
    canonicalization: Literal["aipcs-json-c14n-1"]
    limit_profile: Literal["aipcs-bundle-limits-1"]
    producer_package: Literal["aipcs"]
    producer_package_version: str
    producer_mcp_contract: str
    created_at: str
    source_backend: SourceBackend

    def __post_init__(self) -> None:
        if (
            type(self.export_format_version) is not int
            or self.export_format_version != EXPORT_FORMAT_VERSION
            or type(self.canonicalization) is not str
            or self.canonicalization != CANONICALIZATION_PROFILE
            or type(self.limit_profile) is not str
            or self.limit_profile != LIMIT_PROFILE
            or type(self.producer_package) is not str
            or self.producer_package != PRODUCER_PACKAGE
            or type(self.producer_package_version) is not str
            or _VERSION.fullmatch(self.producer_package_version) is None
            or type(self.producer_mcp_contract) is not str
            or _VERSION.fullmatch(self.producer_mcp_contract) is None
            or type(self.created_at) is not str
            or _UTC_INSTANT.fullmatch(self.created_at) is None
            or type(self.source_backend) is not str
            or self.source_backend not in {"sqlite", "postgresql"}
        ):
            raise PortableBundleContractError()
        try:
            parsed = datetime.strptime(self.created_at, "%Y-%m-%dT%H:%M:%S.%fZ")
        except (TypeError, ValueError):
            raise PortableBundleContractError() from None
        if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != self.created_at:
            raise PortableBundleContractError()

    def payload(self) -> PortableJsonObject:
        return PortableJsonObject.from_mapping(
            {
                "export_format_version": self.export_format_version,
                "canonicalization": self.canonicalization,
                "limit_profile": self.limit_profile,
                "producer_package": self.producer_package,
                "producer_package_version": self.producer_package_version,
                "producer_mcp_contract": self.producer_mcp_contract,
                "created_at": self.created_at,
                "source_backend": self.source_backend,
            }
        )

    @classmethod
    def from_payload(cls, payload: PortableJsonObject) -> PortableHeader:
        expected = {
            "export_format_version",
            "canonicalization",
            "limit_profile",
            "producer_package",
            "producer_package_version",
            "producer_mcp_contract",
            "created_at",
            "source_backend",
        }
        if type(payload) is not PortableJsonObject or set(payload) != expected:
            raise PortableBundleContractError()
        try:
            return cls(
                export_format_version=payload["export_format_version"],  # type: ignore[arg-type]
                canonicalization=payload["canonicalization"],  # type: ignore[arg-type]
                limit_profile=payload["limit_profile"],  # type: ignore[arg-type]
                producer_package=payload["producer_package"],  # type: ignore[arg-type]
                producer_package_version=payload["producer_package_version"],  # type: ignore[arg-type]
                producer_mcp_contract=payload["producer_mcp_contract"],  # type: ignore[arg-type]
                created_at=payload["created_at"],  # type: ignore[arg-type]
                source_backend=payload["source_backend"],  # type: ignore[arg-type]
            )
        except Exception:
            raise PortableBundleContractError() from None


@dataclass(frozen=True, slots=True)
class PortableFrame:
    kind: NonTrailerKind
    ordinal: int
    payload: PortableJsonObject
    sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.kind) is not str
            or self.kind not in NON_TRAILER_KINDS
            or type(self.ordinal) is not int
            or isinstance(self.ordinal, bool)
            or not 0 <= self.ordinal < MAX_BUNDLE_FRAMES
            or type(self.payload) is not PortableJsonObject
            or type(self.sha256) is not str
            or _SHA256.fullmatch(self.sha256) is None
        ):
            raise PortableBundleContractError()


@dataclass(frozen=True, slots=True)
class PortableSection:
    kind: SectionKind
    count: int
    sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.kind) is not str
            or self.kind not in NON_TRAILER_KINDS
            or type(self.count) is not int
            or isinstance(self.count, bool)
            or not 0 <= self.count < MAX_BUNDLE_FRAMES
            or type(self.sha256) is not str
            or _SHA256.fullmatch(self.sha256) is None
        ):
            raise PortableBundleContractError()

    def payload(self) -> PortableJsonObject:
        return PortableJsonObject.from_mapping(
            {"kind": self.kind, "count": self.count, "sha256": self.sha256}
        )


@dataclass(frozen=True, slots=True)
class PortableTrailer:
    ordinal: int
    pre_trailer_byte_count: int
    root_sha256: str
    sections: tuple[PortableSection, ...]

    def __post_init__(self) -> None:
        if (
            type(self.ordinal) is not int
            or isinstance(self.ordinal, bool)
            or not 0 <= self.ordinal < MAX_BUNDLE_FRAMES
            or type(self.pre_trailer_byte_count) is not int
            or isinstance(self.pre_trailer_byte_count, bool)
            or not 0 <= self.pre_trailer_byte_count <= ABSOLUTE_MAX_BUNDLE_BYTES
            or type(self.root_sha256) is not str
            or _SHA256.fullmatch(self.root_sha256) is None
            or type(self.sections) is not tuple
            or len(self.sections) != len(SECTION_KINDS)
            or any(type(section) is not PortableSection for section in self.sections)
            or tuple(section.kind for section in self.sections) != SECTION_KINDS
            or sum(section.count for section in self.sections) != self.ordinal
            or self.sections[-1].count != 0
            or self.sections[-1].sha256 != EMPTY_SHA256
        ):
            raise PortableBundleContractError()

    def payload(self) -> PortableJsonObject:
        return PortableJsonObject.from_mapping(
            {
                "pre_trailer_byte_count": self.pre_trailer_byte_count,
                "root_sha256": self.root_sha256,
                "sections": tuple(section.payload() for section in self.sections),
            }
        )


@dataclass(frozen=True, slots=True)
class PortableBundleLimits:
    """The only V1 limit that a future private caller may lower or override."""

    maximum_total_bytes: int = DEFAULT_MAX_BUNDLE_BYTES

    def __post_init__(self) -> None:
        if (
            type(self.maximum_total_bytes) is not int
            or isinstance(self.maximum_total_bytes, bool)
            or not 1 <= self.maximum_total_bytes <= ABSOLUTE_MAX_BUNDLE_BYTES
        ):
            raise PortableBundleContractError()


@dataclass(frozen=True, slots=True)
class PortableBundleSummary:
    """Payload-free facts safe to retain after a private spool is closed."""

    header: PortableHeader
    sections: tuple[PortableSection, ...]
    root_sha256: str
    pre_trailer_byte_count: int
    total_byte_count: int
    frame_count: int

    def __post_init__(self) -> None:
        if (
            type(self.header) is not PortableHeader
            or type(self.sections) is not tuple
            or len(self.sections) != len(SECTION_KINDS)
            or any(type(section) is not PortableSection for section in self.sections)
            or tuple(section.kind for section in self.sections) != SECTION_KINDS
            or type(self.root_sha256) is not str
            or _SHA256.fullmatch(self.root_sha256) is None
            or type(self.pre_trailer_byte_count) is not int
            or type(self.total_byte_count) is not int
            or type(self.frame_count) is not int
            or not 0 <= self.pre_trailer_byte_count < self.total_byte_count
            or self.total_byte_count > ABSOLUTE_MAX_BUNDLE_BYTES
            or not 3 <= self.frame_count <= MAX_BUNDLE_FRAMES
            or sum(section.count for section in self.sections) + 1 != self.frame_count
        ):
            raise PortableBundleContractError()


def freeze_portable_json(value: object) -> PortableJsonValue:
    """Detach one supported JSON value and enforce the frozen depth/type domain."""

    return _freeze_json(value, 0)


def _freeze_json(value: object, depth: int) -> PortableJsonValue:
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not -(2**63) <= value <= 2**63 - 1:
            raise PortableBundleContractError()
        return value
    if type(value) is float:
        if value != value or value in {float("inf"), float("-inf")}:
            raise PortableBundleContractError()
        return value
    if type(value) is str:
        _require_scalar_text(value)
        return value
    if type(value) in {tuple, list}:
        next_depth = depth + 1
        if next_depth > MAX_JSON_DEPTH:
            raise PortableBundleContractError()
        return tuple(_freeze_json(item, next_depth) for item in value)
    if type(value) is PortableJsonObject:
        next_depth = depth + 1
        if next_depth > MAX_JSON_DEPTH:
            raise PortableBundleContractError()
        entries = tuple(
            (name, _freeze_json(item, next_depth)) for name, item in value.entries
        )
        instance = object.__new__(PortableJsonObject)
        object.__setattr__(instance, "entries", entries)
        return instance
    if type(value) is dict:
        next_depth = depth + 1
        if next_depth > MAX_JSON_DEPTH:
            raise PortableBundleContractError()
        entries: list[tuple[str, PortableJsonValue]] = []
        for name, item in value.items():
            _require_scalar_text(name)
            entries.append((name, _freeze_json(item, next_depth)))
        if len(entries) != len({name for name, _ in entries}):
            raise PortableBundleContractError()
        entries.sort(key=lambda item: _utf16_sort_key(item[0]))
        instance = object.__new__(PortableJsonObject)
        object.__setattr__(instance, "entries", tuple(entries))
        return instance
    raise PortableBundleContractError()


def _thaw_json(value: PortableJsonValue) -> object:
    if type(value) is PortableJsonObject:
        return value.to_dict()
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


def _require_scalar_text(value: object) -> None:
    if type(value) is not str or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise PortableBundleContractError()


def _utf16_sort_key(value: str) -> bytes:
    return value.encode("utf-16-be")
