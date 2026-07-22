"""SYNTHETIC_FIXTURE. Tests for fail-closed SQLite SQL token comparison."""

from __future__ import annotations

import pytest

from aipcs_mcp.storage.sqlite.sql_tokens import sql_tokens_equal


def test_sql_tokens_equal_ignores_only_ascii_whitespace_outside_quoted_tokens() -> None:
    expected = (
        'CREATE TABLE "project" (\n'
        '    "id" TEXT PRIMARY KEY NOT NULL,\n'
        '    "title" TEXT NOT NULL,\n'
        "    \"note\" TEXT DEFAULT 'a  b''c'\n"
        ') STRICT'
    )
    actual = (
        '\tCREATE\r\nTABLE "project"( "id" TEXT PRIMARY KEY NOT NULL ,'
        " \"title\" TEXT NOT NULL , \"note\" TEXT DEFAULT 'a  b''c' )\vSTRICT"
    )

    assert sql_tokens_equal(actual, expected)


def test_sql_tokens_equal_accepts_sqlite_add_column_whitespace_rewrite() -> None:
    expected = (
        'CREATE TABLE "project" (\n'
        '    "id" TEXT PRIMARY KEY NOT NULL,\n'
        '    "title" TEXT NOT NULL,\n'
        '    "summary" TEXT\n'
        ') STRICT'
    )
    actual = (
        'CREATE TABLE "project" (\n'
        '    "id" TEXT PRIMARY KEY NOT NULL,\n'
        '    "title" TEXT NOT NULL\n'
        ', "summary" TEXT) STRICT'
    )

    assert sql_tokens_equal(actual, expected)


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        ('CREATE TABLE "thing" ("id" TEXT PRIMARYKEY NOT NULL) STRICT',
         'CREATE TABLE "thing" ("id" TEXT PRIMARY KEY NOT NULL) STRICT'),
        ('CREATE TABLE "thing" ("id" TEXT NOT NULL) STRICT',
         'CREATE TABLE "thing" ("id" INTEGER NOT NULL) STRICT'),
        ('CREATE TABLE "thing" ("id" TEXT, "title" TEXT) STRICT',
         'CREATE TABLE "thing" ("title" TEXT, "id" TEXT) STRICT'),
        ('CREATE TABLE "thing name" ("id" TEXT) STRICT',
         'CREATE TABLE "thingname" ("id" TEXT) STRICT'),
        ("CREATE TABLE \"thing\" (\"note\" TEXT DEFAULT 'a b') STRICT",
         "CREATE TABLE \"thing\" (\"note\" TEXT DEFAULT 'ab') STRICT"),
        ('CREATE TABLE "a""b" ("id" TEXT) STRICT',
         'CREATE TABLE "a" ("id" TEXT) STRICT'),
    ],
)
def test_sql_tokens_equal_preserves_token_boundaries_and_quoted_contents(
    actual: str, expected: str
) -> None:
    assert not sql_tokens_equal(actual, expected)


@pytest.mark.parametrize(
    "actual",
    [
        'CREATE /* comment */ TABLE "thing" ("id" TEXT) STRICT',
        'CREATE -- comment\nTABLE "thing" ("id" TEXT) STRICT',
        'CREATE TABLE "thing" ("id" TEXT) STRICT;',
        'CREATE TABLE [thing] ("id" TEXT) STRICT',
        'CREATE TABLE `thing` ("id" TEXT) STRICT',
        'CREATE TABLE "thing" ("id" TEXT: NOT NULL) STRICT',
        'CREATE TABLE "thing" ("id" TEXT) STRICT\u00a0',
        "CREATE TABLE \"thing\" (\"note\" TEXT DEFAULT 'not; allowed') STRICT",
        'CREATE TABLE "thing ("id" TEXT) STRICT',
        "CREATE TABLE \"thing\" (\"note\" TEXT DEFAULT 'missing) STRICT",
    ],
)
def test_sql_tokens_equal_rejects_unsupported_or_malformed_sql(actual: str) -> None:
    expected = 'CREATE TABLE "thing" ("id" TEXT) STRICT'

    assert not sql_tokens_equal(actual, expected)
    assert not sql_tokens_equal(expected, actual)


@pytest.mark.parametrize("value", [None, b"CREATE TABLE", 1, object()])
def test_sql_tokens_equal_rejects_non_string_values(value: object) -> None:
    expected = 'CREATE TABLE "thing" ("id" TEXT) STRICT'

    assert not sql_tokens_equal(value, expected)
    assert not sql_tokens_equal(expected, value)


def test_sql_tokens_equal_rejects_string_subclasses() -> None:
    class StringSubclass(str):
        pass

    expected = 'CREATE TABLE "thing" ("id" TEXT) STRICT'

    assert not sql_tokens_equal(StringSubclass(expected), expected)
