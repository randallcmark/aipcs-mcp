"""Shared numeric domains for public JSON, pure records, and storage codecs."""

from __future__ import annotations

import math

SIGNED_64_MIN = -(2**63)
SIGNED_64_MAX = 2**63 - 1
IEEE_754_EXACT_INTEGER_MAX = 2**53


def is_signed_64_integer(value: object) -> bool:
    """Return whether ``value`` is a non-boolean signed-64 integer."""

    return type(value) is int and SIGNED_64_MIN <= value <= SIGNED_64_MAX


def is_finite_real(value: object) -> bool:
    """Return whether ``value`` is one finite binary64 value."""

    return type(value) is float and math.isfinite(value)


def is_exact_number(value: object) -> bool:
    """Return whether ``value`` can enter SQLite REAL without integer rounding."""

    if type(value) is int:
        return -IEEE_754_EXACT_INTEGER_MAX <= value <= IEEE_754_EXACT_INTEGER_MAX
    return is_finite_real(value)
