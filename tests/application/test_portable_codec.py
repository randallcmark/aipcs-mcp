"""Pure conformance for canonical portable bundle values and digests."""

from __future__ import annotations

import hashlib
import struct

import pytest

from aipcs_mcp.application.portable_codec import (
    PortableBundleEncoder,
    canonical_json_bytes,
    decode_canonical_json_object,
    decode_non_trailer_frame,
    decode_trailer,
    encode_non_trailer_frame,
)
from aipcs_mcp.application.portable_models import (
    ABSOLUTE_MAX_BUNDLE_BYTES,
    DEFAULT_MAX_BUNDLE_BYTES,
    EMPTY_SHA256,
    MAX_BUNDLE_FRAMES,
    MAX_FRAME_BYTES,
    PortableBundleContractError,
    PortableBundleLimits,
    PortableFrame,
    PortableHeader,
    PortableJsonObject,
)


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


def _minimal_bundle() -> tuple[bytes, object]:
    encoder = PortableBundleEncoder()
    lines = [
        encoder.add("header", _header().payload()),
        encoder.add("service", {"service_id": "5b5d0b6a-976c-4e32-b4d2-cc0a64e3ee23"}),
    ]
    trailer, summary = encoder.finish()
    return b"".join((*lines, trailer)), summary


def test_utf16_key_order_and_rfc8785_string_escaping_are_exact() -> None:
    assert canonical_json_bytes({"\ue000": 1, "\U0001f600": 2}).hex() == (
        "7b22f09f9880223a322c22ee8080223a317d"
    )
    assert canonical_json_bytes(
        {"value": "\x00\b\t\n\f\r\"\\/😀\u2028"}
    ) == b'{"value":"\\u0000\\b\\t\\n\\f\\r\\"\\\\/\xf0\x9f\x98\x80\xe2\x80\xa8"}'


@pytest.mark.parametrize(
    "value,expected",
    [
        (0.0, b"0"),
        (-0.0, b"0"),
        (5e-324, b"5e-324"),
        (1e-6, b"0.000001"),
        (1e-7, b"1e-7"),
        (1e20, b"100000000000000000000"),
        (1e23, b"1e+23"),
        (1.7976931348623157e308, b"1.7976931348623157e+308"),
        (333333333.33333329, b"333333333.3333333"),
    ],
)
def test_binary64_spelling_matches_rfc8785(value: float, expected: bytes) -> None:
    assert canonical_json_bytes(value) == expected


def test_signed_64_and_depth_domains_are_exact() -> None:
    assert canonical_json_bytes(-(2**63)) == b"-9223372036854775808"
    assert canonical_json_bytes(2**63 - 1) == b"9223372036854775807"
    for value in (-(2**63) - 1, 2**63):
        with pytest.raises(PortableBundleContractError):
            canonical_json_bytes(value)

    accepted: object = {}
    for _ in range(63):
        accepted = {"a": accepted}
    canonical_json_bytes(accepted)
    rejected: object = {"a": accepted}
    with pytest.raises(PortableBundleContractError):
        canonical_json_bytes(rejected)


def test_frozen_frame_count_and_byte_limits_are_exact() -> None:
    assert MAX_FRAME_BYTES == 1024 * 1024
    assert PortableBundleLimits().maximum_total_bytes == DEFAULT_MAX_BUNDLE_BYTES
    assert DEFAULT_MAX_BUNDLE_BYTES == 256 * 1024 * 1024
    assert ABSOLUTE_MAX_BUNDLE_BYTES == 1024 * 1024 * 1024
    assert MAX_BUNDLE_FRAMES == 1_000_000
    PortableBundleLimits(ABSOLUTE_MAX_BUNDLE_BYTES)
    with pytest.raises(PortableBundleContractError):
        PortableBundleLimits(ABSOLUTE_MAX_BUNDLE_BYTES + 1)
    with pytest.raises(PortableBundleContractError):
        PortableBundleLimits(True)  # type: ignore[arg-type]

    PortableFrame(
        "record",
        MAX_BUNDLE_FRAMES - 1,
        PortableJsonObject.from_mapping({}),
        EMPTY_SHA256,
    )
    with pytest.raises(PortableBundleContractError):
        PortableFrame(
            "record",
            MAX_BUNDLE_FRAMES,
            PortableJsonObject.from_mapping({}),
            EMPTY_SHA256,
        )


@pytest.mark.parametrize(
    "value",
    [
        b'{"a":1,"a":1}',
        b'{"a": 1}',
        b'{"a":"\\u0061"}',
        b'{"a":-0}',
        b'{"a":1.0}',
        b'{"a":NaN}',
        b'{"a":9223372036854775808}',
        b'\xef\xbb\xbf{"a":1}',
        b'{"a":"\xed\xa0\x80"}',
    ],
)
def test_decoder_rejects_duplicate_noncanonical_or_invalid_json(value: bytes) -> None:
    with pytest.raises(PortableBundleContractError) as failure:
        decode_canonical_json_object(value)
    assert str(failure.value) == "The portable bundle is invalid."


def test_header_frame_has_independent_core_digest_and_exact_shape() -> None:
    line = encode_non_trailer_frame("header", 0, _header().payload())
    decoded = decode_canonical_json_object(line[:-1])
    frame = decode_non_trailer_frame(decoded)
    core = PortableJsonObject.from_mapping(
        {"kind": frame.kind, "ordinal": frame.ordinal, "payload": frame.payload}
    )
    assert frame.sha256 == hashlib.sha256(canonical_json_bytes(core)).hexdigest()
    assert frame.sha256 == "714f9d01002d256c6ac73cd57bca80b2d3f88c9c30bf7dd9248155973b940faf"
    assert len(line) == 395
    assert hashlib.sha256(line).hexdigest() == (
        "14678e7c175aa634218252c1c790537af95d424654291bcd692e788b24ad0c68"
    )
    assert frame.kind == "header"
    assert frame.ordinal == 0

    altered = decoded.to_dict()
    altered["extra"] = True
    with pytest.raises(PortableBundleContractError):
        decode_non_trailer_frame(PortableJsonObject.from_mapping(altered))

    newer = encode_non_trailer_frame(
        "header",
        0,
        {**_header().payload().to_dict(), "export_format_version": 2},
    )
    with pytest.raises(PortableBundleContractError):
        decode_non_trailer_frame(decode_canonical_json_object(newer[:-1]))


def test_encoder_enforces_order_singletons_and_reserved_zero_audit() -> None:
    encoder = PortableBundleEncoder()
    encoder.add("header", _header().payload())
    with pytest.raises(PortableBundleContractError):
        encoder.add("record", {"id": "before-service"})

    encoder = PortableBundleEncoder()
    encoder.add("header", _header().payload())
    encoder.add("service", {"id": "service"})
    encoder.add("record", {"id": "record"})
    encoder.add("history", {"event_sequence": 1})
    with pytest.raises(PortableBundleContractError):
        encoder.add("record", {"id": "late-record"})
    with pytest.raises(PortableBundleContractError):
        encoder.add("audit", {"action": "forbidden"})


def test_trailer_accounts_for_exact_newline_terminated_sections_and_root() -> None:
    bundle, summary = _minimal_bundle()
    lines = bundle.splitlines(keepends=True)
    trailer = decode_trailer(decode_canonical_json_object(lines[-1][:-1]))

    assert trailer.ordinal == 2
    assert trailer.pre_trailer_byte_count == len(lines[0]) + len(lines[1])
    assert trailer.root_sha256 == hashlib.sha256(lines[0] + lines[1]).hexdigest()
    assert trailer.sections[0].sha256 == hashlib.sha256(lines[0]).hexdigest()
    assert trailer.sections[1].sha256 == hashlib.sha256(lines[1]).hexdigest()
    assert all(section.sha256 == EMPTY_SHA256 for section in trailer.sections[2:])
    assert summary == summary


def test_frame_payload_depth_includes_the_frame_and_payload_objects() -> None:
    payload: object = {}
    for _ in range(62):
        payload = {"a": payload}
    encode_non_trailer_frame("record", 2, payload)  # type: ignore[arg-type]
    with pytest.raises(PortableBundleContractError):
        encode_non_trailer_frame("record", 2, {"a": payload})


def test_extreme_binary64_vectors_are_real_ieee_values() -> None:
    minimum = struct.unpack(">d", bytes.fromhex("0000000000000001"))[0]
    maximum = struct.unpack(">d", bytes.fromhex("7fefffffffffffff"))[0]
    assert canonical_json_bytes(minimum) == b"5e-324"
    assert canonical_json_bytes(maximum) == b"1.7976931348623157e+308"
