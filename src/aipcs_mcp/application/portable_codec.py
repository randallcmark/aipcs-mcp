"""Canonical JSON and ordered digest logic for portable bundle format v1."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Final

from .portable_models import (
    MAX_BUNDLE_FRAMES,
    MAX_FRAME_BYTES,
    MAX_JSON_DEPTH,
    NON_TRAILER_KINDS,
    SECTION_KINDS,
    NonTrailerKind,
    PortableBundleContractError,
    PortableBundleLimits,
    PortableBundleSummary,
    PortableFrame,
    PortableHeader,
    PortableJsonObject,
    PortableJsonValue,
    PortableSection,
    PortableTrailer,
    freeze_portable_json,
)

_HEX: Final = "0123456789abcdef"
_FRAME_KEYS: Final = {"kind", "ordinal", "payload", "sha256"}
_TRAILER_KEYS: Final = {"kind", "ordinal", "payload"}
_TRAILER_PAYLOAD_KEYS: Final = {"pre_trailer_byte_count", "root_sha256", "sections"}
_SECTION_KEYS: Final = {"kind", "count", "sha256"}


def canonical_json_bytes(value: object) -> bytes:
    """Encode one value under the exact ``aipcs-json-c14n-1`` profile."""

    try:
        detached = freeze_portable_json(value)
        return _encode_json(detached).encode("utf-8")
    except PortableBundleContractError:
        raise
    except Exception:
        raise PortableBundleContractError() from None


def decode_canonical_json_object(value: bytes) -> PortableJsonObject:
    """Decode one canonical JSON object with duplicate-key and depth rejection."""

    try:
        if type(value) is not bytes or not value:
            raise PortableBundleContractError()
        text = value.decode("utf-8", errors="strict")
        if text.startswith("\ufeff"):
            raise PortableBundleContractError()
        _require_bounded_nesting(text)

        def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for name, item in items:
                if name in result:
                    raise PortableBundleContractError()
                result[name] = item
            return result

        def integer(text_value: str) -> int:
            parsed = int(text_value)
            if not -(2**63) <= parsed <= 2**63 - 1:
                raise PortableBundleContractError()
            return parsed

        def number(text_value: str) -> float:
            parsed = float(text_value)
            if not math.isfinite(parsed):
                raise PortableBundleContractError()
            return parsed

        def constant(_: str) -> object:
            raise PortableBundleContractError()

        decoded = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_int=integer,
            parse_float=number,
            parse_constant=constant,
        )
        detached = freeze_portable_json(decoded)
        if type(detached) is not PortableJsonObject or canonical_json_bytes(detached) != value:
            raise PortableBundleContractError()
        return detached
    except PortableBundleContractError:
        raise
    except Exception:
        raise PortableBundleContractError() from None


def encode_non_trailer_frame(
    kind: NonTrailerKind, ordinal: int, payload: PortableJsonObject | dict[str, object]
) -> bytes:
    """Build one canonical, independently hashed, newline-terminated frame."""

    try:
        detached = freeze_portable_json(payload)
        if (
            type(kind) is not str
            or kind not in NON_TRAILER_KINDS
            or type(ordinal) is not int
            or isinstance(ordinal, bool)
            or not 0 <= ordinal < MAX_BUNDLE_FRAMES
            or type(detached) is not PortableJsonObject
        ):
            raise PortableBundleContractError()
        core = PortableJsonObject.from_mapping(
            {"kind": kind, "ordinal": ordinal, "payload": detached}
        )
        digest = hashlib.sha256(canonical_json_bytes(core)).hexdigest()
        frame = PortableJsonObject.from_mapping(
            {
                "kind": kind,
                "ordinal": ordinal,
                "payload": detached,
                "sha256": digest,
            }
        )
        line = canonical_json_bytes(frame) + b"\n"
        if len(line) > MAX_FRAME_BYTES:
            raise PortableBundleContractError()
        return line
    except PortableBundleContractError:
        raise
    except Exception:
        raise PortableBundleContractError() from None


def decode_non_trailer_frame(value: PortableJsonObject) -> PortableFrame:
    """Validate an exact non-trailer frame including its independent digest."""

    try:
        if type(value) is not PortableJsonObject or set(value) != _FRAME_KEYS:
            raise PortableBundleContractError()
        kind = value["kind"]
        ordinal = value["ordinal"]
        payload = value["payload"]
        digest = value["sha256"]
        frame = PortableFrame(kind, ordinal, payload, digest)  # type: ignore[arg-type]
        core = PortableJsonObject.from_mapping(
            {"kind": frame.kind, "ordinal": frame.ordinal, "payload": frame.payload}
        )
        if hashlib.sha256(canonical_json_bytes(core)).hexdigest() != frame.sha256:
            raise PortableBundleContractError()
        if frame.kind == "header":
            PortableHeader.from_payload(frame.payload)
        return frame
    except PortableBundleContractError:
        raise
    except Exception:
        raise PortableBundleContractError() from None


def encode_trailer(trailer: PortableTrailer) -> bytes:
    """Encode the canonical newline-terminated trailer excluded from all digests."""

    if type(trailer) is not PortableTrailer:
        raise PortableBundleContractError()
    value = PortableJsonObject.from_mapping(
        {"kind": "trailer", "ordinal": trailer.ordinal, "payload": trailer.payload()}
    )
    line = canonical_json_bytes(value) + b"\n"
    if len(line) > MAX_FRAME_BYTES:
        raise PortableBundleContractError()
    return line


def decode_trailer(value: PortableJsonObject) -> PortableTrailer:
    """Validate the trailer's exact closed shape and reserved-zero audit section."""

    try:
        if (
            type(value) is not PortableJsonObject
            or set(value) != _TRAILER_KEYS
            or value["kind"] != "trailer"
        ):
            raise PortableBundleContractError()
        ordinal = value["ordinal"]
        payload = value["payload"]
        if type(payload) is not PortableJsonObject or set(payload) != _TRAILER_PAYLOAD_KEYS:
            raise PortableBundleContractError()
        raw_sections = payload["sections"]
        if type(raw_sections) is not tuple or len(raw_sections) != len(SECTION_KINDS):
            raise PortableBundleContractError()
        sections: list[PortableSection] = []
        for expected, raw in zip(SECTION_KINDS, raw_sections, strict=True):
            if (
                type(raw) is not PortableJsonObject
                or set(raw) != _SECTION_KEYS
                or raw["kind"] != expected
            ):
                raise PortableBundleContractError()
            sections.append(
                PortableSection(
                    kind=raw["kind"],  # type: ignore[arg-type]
                    count=raw["count"],  # type: ignore[arg-type]
                    sha256=raw["sha256"],  # type: ignore[arg-type]
                )
            )
        return PortableTrailer(
            ordinal=ordinal,  # type: ignore[arg-type]
            pre_trailer_byte_count=payload["pre_trailer_byte_count"],  # type: ignore[arg-type]
            root_sha256=payload["root_sha256"],  # type: ignore[arg-type]
            sections=tuple(sections),
        )
    except PortableBundleContractError:
        raise
    except Exception:
        raise PortableBundleContractError() from None


@dataclass(slots=True)
class OrderedBundleDigests:
    """Incremental order, section, root, ordinal, and byte-count authority."""

    _next_ordinal: int = 0
    _section_index: int = 0
    _pre_trailer_byte_count: int = 0
    _root: object = None
    _section_hashes: tuple[object, ...] = ()
    _section_counts: list[int] | None = None

    def __post_init__(self) -> None:
        self._root = hashlib.sha256()
        self._section_hashes = tuple(hashlib.sha256() for _ in SECTION_KINDS)
        self._section_counts = [0 for _ in SECTION_KINDS]

    @property
    def frame_count(self) -> int:
        return self._next_ordinal

    @property
    def pre_trailer_byte_count(self) -> int:
        return self._pre_trailer_byte_count

    def add(self, frame: PortableFrame, exact_line: bytes) -> None:
        try:
            if (
                type(frame) is not PortableFrame
                or type(exact_line) is not bytes
                or not exact_line.endswith(b"\n")
                or len(exact_line) > MAX_FRAME_BYTES
                or frame.ordinal != self._next_ordinal
                or frame.kind == "audit"
                or encode_non_trailer_frame(frame.kind, frame.ordinal, frame.payload)
                != exact_line
            ):
                raise PortableBundleContractError()
            section_index = SECTION_KINDS.index(frame.kind)
            if section_index < self._section_index:
                raise PortableBundleContractError()
            counts = self._counts()
            if frame.kind == "header":
                if frame.ordinal != 0 or counts[0] != 0:
                    raise PortableBundleContractError()
            elif counts[0] != 1:
                raise PortableBundleContractError()
            if frame.kind == "service":
                if frame.ordinal != 1 or counts[1] != 0:
                    raise PortableBundleContractError()
            elif section_index > 1 and counts[1] != 1:
                raise PortableBundleContractError()
            if self._next_ordinal >= MAX_BUNDLE_FRAMES - 1:
                raise PortableBundleContractError()
            self._section_index = section_index
            self._section_hashes[section_index].update(exact_line)  # type: ignore[attr-defined]
            self._root.update(exact_line)  # type: ignore[attr-defined]
            counts[section_index] += 1
            self._pre_trailer_byte_count += len(exact_line)
            self._next_ordinal += 1
        except PortableBundleContractError:
            raise
        except Exception:
            raise PortableBundleContractError() from None

    def trailer(self) -> PortableTrailer:
        counts = self._counts()
        if counts[0] != 1 or counts[1] != 1:
            raise PortableBundleContractError()
        sections = tuple(
            PortableSection(
                kind=kind,
                count=counts[index],
                sha256=self._section_hashes[index].hexdigest(),  # type: ignore[attr-defined]
            )
            for index, kind in enumerate(SECTION_KINDS)
        )
        return PortableTrailer(
            ordinal=self._next_ordinal,
            pre_trailer_byte_count=self._pre_trailer_byte_count,
            root_sha256=self._root.hexdigest(),  # type: ignore[attr-defined]
            sections=sections,
        )

    def verify(self, trailer: PortableTrailer) -> None:
        expected = self.trailer()
        if trailer != expected:
            raise PortableBundleContractError()

    def _counts(self) -> list[int]:
        if self._section_counts is None:
            raise PortableBundleContractError()
        return self._section_counts


class PortableBundleEncoder:
    """A bounded writer-side accumulator that yields one canonical line at a time."""

    def __init__(self, limits: PortableBundleLimits | None = None) -> None:
        if limits is None:
            limits = PortableBundleLimits()
        if type(limits) is not PortableBundleLimits:
            raise PortableBundleContractError()
        self._limits = limits
        self._digests = OrderedBundleDigests()
        self._header: PortableHeader | None = None
        self._finished = False

    def add(
        self, kind: NonTrailerKind, payload: PortableJsonObject | dict[str, object]
    ) -> bytes:
        if self._finished:
            raise PortableBundleContractError()
        ordinal = self._digests.frame_count
        line = encode_non_trailer_frame(kind, ordinal, payload)
        decoded = decode_non_trailer_frame(decode_canonical_json_object(line[:-1]))
        projected = self._digests.pre_trailer_byte_count + len(line)
        if projected > self._limits.maximum_total_bytes:
            raise PortableBundleContractError()
        self._digests.add(decoded, line)
        if decoded.kind == "header":
            self._header = PortableHeader.from_payload(decoded.payload)
        return line

    def finish(self) -> tuple[bytes, PortableBundleSummary]:
        if self._finished or self._header is None:
            raise PortableBundleContractError()
        trailer = self._digests.trailer()
        line = encode_trailer(trailer)
        total = trailer.pre_trailer_byte_count + len(line)
        if total > self._limits.maximum_total_bytes:
            raise PortableBundleContractError()
        self._finished = True
        return line, PortableBundleSummary(
            header=self._header,
            sections=trailer.sections,
            root_sha256=trailer.root_sha256,
            pre_trailer_byte_count=trailer.pre_trailer_byte_count,
            total_byte_count=total,
            frame_count=trailer.ordinal + 1,
        )


def _encode_json(value: PortableJsonValue) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if type(value) is float:
        return _encode_binary64(value)
    if type(value) is str:
        return _encode_string(value)
    if type(value) is tuple:
        return "[" + ",".join(_encode_json(item) for item in value) + "]"
    if type(value) is PortableJsonObject:
        return (
            "{"
            + ",".join(
                _encode_string(name) + ":" + _encode_json(item)
                for name, item in value.entries
            )
            + "}"
        )
    raise PortableBundleContractError()


def _encode_string(value: str) -> str:
    pieces = ['"']
    short = {"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
    for character in value:
        point = ord(character)
        if 0xD800 <= point <= 0xDFFF:
            raise PortableBundleContractError()
        if character in short:
            pieces.append(short[character])
        elif character == '"':
            pieces.append('\\"')
        elif character == "\\":
            pieces.append("\\\\")
        elif point <= 0x1F:
            pieces.append("\\u00" + _HEX[point >> 4] + _HEX[point & 0x0F])
        else:
            pieces.append(character)
    pieces.append('"')
    return "".join(pieces)


def _encode_binary64(value: float) -> str:
    if not math.isfinite(value):
        raise PortableBundleContractError()
    if value == 0:
        return "0"
    negative = value < 0
    raw = repr(-value if negative else value).lower()
    sign = "-" if negative else ""
    if "e" not in raw:
        return sign + (raw[:-2] if raw.endswith(".0") else raw)

    mantissa, exponent_text = raw.split("e", 1)
    exponent = int(exponent_text)
    digits = mantissa.replace(".", "")
    absolute = -value if negative else value
    if 1e-6 <= absolute < 1e21:
        decimal_position = exponent + 1
        if decimal_position <= 0:
            body = "0." + "0" * (-decimal_position) + digits
        elif decimal_position >= len(digits):
            body = digits + "0" * (decimal_position - len(digits))
        else:
            body = digits[:decimal_position] + "." + digits[decimal_position:]
        return sign + body
    scientific = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
    exponent_rendered = ("+" if exponent >= 0 else "") + str(exponent)
    return sign + scientific + "e" + exponent_rendered


def _require_bounded_nesting(value: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise PortableBundleContractError()
        elif character in "]}":
            depth -= 1
            if depth < 0:
                raise PortableBundleContractError()
    if in_string or escaped or depth != 0:
        raise PortableBundleContractError()
