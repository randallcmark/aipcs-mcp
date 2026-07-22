"""Fail-closed comparison of the SQLite SQL used by domain tables.

SQLite rewrites the whitespace around a newly added column in
``sqlite_schema.sql``.  This module accepts only that insignificant ASCII
whitespace variation; every token and quoted value remains exact.
"""

from __future__ import annotations

_ASCII_WHITESPACE = " \t\n\r\f\v"
_PUNCTUATION = frozenset("(),")


def sql_tokens_equal(actual: object, expected: object) -> bool:
    """Return whether two supported SQLite SQL strings have equal tokens.

    The lexer deliberately supports only the table DDL vocabulary emitted by
    the layout compiler: ASCII identifier tokens, double- or single-quoted
    tokens, and parentheses and commas.  It ignores ASCII whitespace only
    outside quoted tokens.  Any other syntax is rejected rather than guessed.
    """

    actual_tokens = _tokens(actual)
    expected_tokens = _tokens(expected)
    return actual_tokens is not None and actual_tokens == expected_tokens


def _tokens(value: object) -> tuple[str, ...] | None:
    if type(value) is not str:
        return None

    tokens: list[str] = []
    position = 0
    while position < len(value):
        character = value[position]
        if character in _ASCII_WHITESPACE:
            position += 1
        elif character in ('"', "'"):
            quoted, position = _quoted_token(value, position)
            if quoted is None:
                return None
            tokens.append(quoted)
        elif _is_identifier_start(character):
            end = position + 1
            while end < len(value) and _is_identifier_continuation(value[end]):
                end += 1
            tokens.append(value[position:end])
            position = end
        elif character in _PUNCTUATION:
            tokens.append(character)
            position += 1
        else:
            return None
    return tuple(tokens)


def _quoted_token(value: str, start: int) -> tuple[str | None, int]:
    delimiter = value[start]
    position = start + 1
    while position < len(value):
        if value[position] == ";":
            return None, position
        if value[position] != delimiter:
            position += 1
            continue
        if position + 1 < len(value) and value[position + 1] == delimiter:
            position += 2
            continue
        return value[start : position + 1], position + 1
    return None, position


def _is_identifier_start(character: str) -> bool:
    return "A" <= character <= "Z" or "a" <= character <= "z" or character == "_"


def _is_identifier_continuation(character: str) -> bool:
    return _is_identifier_start(character) or "0" <= character <= "9"
