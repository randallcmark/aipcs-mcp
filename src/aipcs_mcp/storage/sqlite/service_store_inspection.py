"""Exact, transaction-neutral inspection for the SQLite service-store foundation."""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from enum import StrEnum

from aipcs_mcp.storage.contracts import MigrationState
from aipcs_mcp.storage.errors import StorageMigrationError

from .result_codes import is_sqlite_busy
from .service_store_migrations import (
    META,
    MIGRATION,
    MIGRATIONS,
    R1,
    R2,
    R2_POLICY,
    R3,
    RESERVED_PREFIX,
    TARGET_REVISION,
)
from .wal_policy import (
    WALPolicyEvidence,
    WALPolicyState,
    classify_wal_policy,
    history_matches,
    journal_mode_matches,
    policy_row_matches,
    policy_table_matches,
)


class R3FoundationState(StrEnum):
    """Exact R3 data-foundation evidence layered over the immutable R2 WAL policy."""

    CLEAN_R2 = "clean_r2"
    PREPARED_R3 = "prepared_r3"
    READY_R3 = "ready_r3"
    INCOMPATIBLE = "incompatible"


def inspect_connection(
    connection: sqlite3.Connection, namespace: str, *, integrity: bool
) -> MigrationState:
    """Classify one open connection without changing its transaction ownership."""

    r3_state, revision = classify_r3_foundation(connection, namespace)
    if r3_state is R3FoundationState.READY_R3:
        if integrity:
            _quick_check(connection)
        return _state(TARGET_REVISION, "ready")
    if r3_state is R3FoundationState.PREPARED_R3:
        return _state(revision, "dirty")
    if r3_state is R3FoundationState.CLEAN_R2:
        return _state(revision, "outdated")

    state, revision = classify_connection(connection, namespace)
    if state is WALPolicyState.READY_WAL:
        if integrity:
            _quick_check(connection)
        return _state(TARGET_REVISION, "ready")
    if state in {WALPolicyState.PREPARED_DELETE, WALPolicyState.PREPARED_WAL}:
        return _state(revision, "dirty")
    if state is WALPolicyState.CLEAN_PREDECESSOR:
        return _state(revision, "incompatible")
    if _is_generic_historical_dirty(connection, namespace):
        return _state(revision, "dirty")
    if revision == 0 and _has_no_objects(connection):
        return _state(0, "uninitialised")
    return _state(revision, "incompatible")


def classify_r3_foundation(
    connection: sqlite3.Connection, namespace: str
) -> tuple[R3FoundationState, int]:
    """Classify only exact R2-to-R3 data-foundation states without changing storage."""

    try:
        objects = _objects(connection)
        meta = _bound_meta(connection, objects, namespace)
        revision = meta[0] if meta is not None else 0
        dirty = meta[1] if meta is not None else -1
        policy_sql, policy_rows = _policy_evidence(connection, objects)
        policy_ready = (
            policy_table_matches(policy_sql, R2_POLICY)
            and policy_row_matches(policy_rows, R2_POLICY, "ready")
            and journal_mode_matches(_journal_mode(connection), "wal")
        )
        if (
            revision == R3.revision
            and dirty == 0
            and policy_ready
            and _layout_matches(connection, objects, R3)
            and history_matches(_history_rows(connection), "service_store", MIGRATIONS, R3.revision)
        ):
            return R3FoundationState.READY_R3, revision
        if (
            revision == R2.revision
            and dirty == 1
            and policy_ready
            and _layout_matches(connection, objects, R3)
            and history_matches(_history_rows(connection), "service_store", MIGRATIONS, R2.revision)
        ):
            return R3FoundationState.PREPARED_R3, revision
        if (
            revision == R2.revision
            and dirty == 0
            and policy_ready
            and _layout_matches(connection, objects, R2)
            and history_matches(_history_rows(connection), "service_store", MIGRATIONS, R2.revision)
        ):
            return R3FoundationState.CLEAN_R2, revision
        return R3FoundationState.INCOMPATIBLE, revision
    except Exception as error:
        if is_sqlite_busy(error):
            raise
        return R3FoundationState.INCOMPATIBLE, 0


def classify_connection(
    connection: sqlite3.Connection, namespace: str
) -> tuple[WALPolicyState, int]:
    """Return finite WAL-policy evidence for a caller-owned read or write transaction."""

    try:
        objects = _objects(connection)
        if not objects:
            return WALPolicyState.INCOMPATIBLE, 0
        meta = _bound_meta(connection, objects, namespace)
        revision = meta[0] if meta is not None else 0
        dirty = meta[1] if meta is not None else -1
        policy_sql, policy_rows = _policy_evidence(connection, objects)
        marker_present = policy_sql is not None or bool(policy_rows)
        descriptor = R2 if marker_present else R1
        layout_matches = _layout_matches(connection, objects, descriptor)
        histories = _history_rows(connection)
        evidence = WALPolicyEvidence(
            revision=revision,
            dirty=dirty,
            journal_mode=_journal_mode(connection),
            layout_matches=layout_matches,
            history_matches=history_matches(histories, "service_store", MIGRATIONS, revision),
            policy_table_sql=policy_sql,
            policy_rows=policy_rows,
        )
        return (
            classify_wal_policy(
                evidence,
                predecessor_revision=R1.revision,
                target_revision=R2.revision,
                descriptor=R2_POLICY,
            ),
            revision,
        )
    except Exception as error:
        if is_sqlite_busy(error):
            raise
        return WALPolicyState.INCOMPATIBLE, 0


def _objects(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE substr(name,1,7) <> 'sqlite_'"
    ).fetchall()


def _has_no_objects(connection: sqlite3.Connection) -> bool:
    try:
        return not _objects(connection)
    except Exception as error:
        if is_sqlite_busy(error):
            raise
        return False


def _is_generic_historical_dirty(connection: sqlite3.Connection, namespace: str) -> bool:
    """Expose valid historical dirt without granting it recovery privileges."""

    try:
        objects = _objects(connection)
        meta = _bound_meta(connection, objects, namespace)
        if meta is None or meta[1] != 1:
            return False
        policy_sql, policy_rows = _policy_evidence(connection, objects)
        return (
            policy_sql is None
            and not policy_rows
            and _layout_matches(connection, objects, R1)
            and history_matches(_history_rows(connection), "service_store", MIGRATIONS, meta[0])
        )
    except Exception as error:
        if is_sqlite_busy(error):
            raise
        return False


def _bound_meta(
    connection: sqlite3.Connection, objects: list[sqlite3.Row], namespace: str
) -> tuple[int, int] | None:
    row = next((current for current in objects if current["name"] == META), None)
    if row is None or row["type"] != "table" or row["sql"] != R1.expected_sql[META]:
        return None
    try:
        if tuple(map(tuple, connection.execute(f'PRAGMA table_xinfo("{META}")'))) != R1.table_xinfo[
            META
        ]:
            return None
        rows = connection.execute(
            f'SELECT singleton,adapter_id,component,namespace,applied_revision,dirty FROM "{META}"'
        ).fetchall()
        if len(rows) != 1 or tuple(rows[0][:4]) != (
            1,
            "aipcs.sqlite.service_store",
            "service_store",
            namespace,
        ):
            return None
        revision, dirty = rows[0][4], rows[0][5]
        if type(revision) is not int or revision < 0 or type(dirty) is not int or dirty not in (0, 1):
            return None
        return revision, dirty
    except Exception as error:
        if is_sqlite_busy(error):
            raise
        return None


def _policy_evidence(
    connection: sqlite3.Connection, objects: list[sqlite3.Row]
) -> tuple[object | None, tuple[tuple[object, ...], ...]]:
    row = next((current for current in objects if current["name"] == R2_POLICY.table_name), None)
    if row is None:
        return None, ()
    try:
        rows = connection.execute(
            f'SELECT singleton,policy_id,policy_checksum,phase FROM "{R2_POLICY.table_name}"'
        ).fetchall()
        return row["sql"], tuple(tuple(current) for current in rows)
    except Exception as error:
        if is_sqlite_busy(error):
            raise
        return row["sql"], ()


def _layout_matches(
    connection: sqlite3.Connection, objects: list[sqlite3.Row], descriptor: object
) -> bool:
    try:
        expected_sql = descriptor.expected_sql  # type: ignore[attr-defined]
        table_xinfo = descriptor.table_xinfo  # type: ignore[attr-defined]
        index_list = descriptor.index_list  # type: ignore[attr-defined]
        index_xinfo = descriptor.index_xinfo  # type: ignore[attr-defined]
        foreign_keys = descriptor.foreign_keys  # type: ignore[attr-defined]
        reserved = {row["name"]: row for row in objects if _is_reserved(row["name"])}
        if set(reserved) != set(expected_sql):
            return False
        if any(
            row["type"] != "table" and name in table_xinfo
            or row["sql"] != expected_sql[name]
            for name, row in reserved.items()
        ):
            return False
        for table, expected in table_xinfo.items():
            if tuple(map(tuple, connection.execute(f'PRAGMA table_xinfo("{table}")'))) != expected:
                return False
        for table, expected in foreign_keys.items():
            if tuple(map(tuple, connection.execute(f'PRAGMA foreign_key_list("{table}")'))) != expected:
                return False
        for table, expected in index_list.items():
            rows = connection.execute(f'PRAGMA index_list("{table}")')
            actual = tuple(sorted((row[1], row[2], row[3], row[4]) for row in rows))
            if actual != expected:
                return False
        for index, expected in index_xinfo.items():
            if tuple(map(tuple, connection.execute(f'PRAGMA index_xinfo("{index}")'))) != expected:
                return False
        return not _has_forbidden_objects(connection, objects)
    except Exception as error:
        if is_sqlite_busy(error):
            raise
        return False


def _has_forbidden_objects(connection: sqlite3.Connection, objects: list[sqlite3.Row]) -> bool:
    try:
        for row in objects:
            name = row["name"]
            if _is_reserved(name):
                continue
            object_type = row["type"]
            if object_type in {"view", "trigger"}:
                return True
            if object_type == "index" and _is_reserved(row["tbl_name"]):
                return True
            if object_type == "table":
                quoted = name.replace('"', '""')
                foreign_keys = connection.execute(f'PRAGMA foreign_key_list("{quoted}")')
                if any(_is_reserved(foreign_key[2]) for foreign_key in foreign_keys):
                    return True
    except Exception as error:
        if is_sqlite_busy(error):
            raise
        return True
    return False


def _is_reserved(name: object) -> bool:
    return isinstance(name, str) and name.lower().startswith(RESERVED_PREFIX)


def _history_rows(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute(
            f'SELECT component,revision,migration_id,checksum,applied_at FROM "{MIGRATION}" '
            "ORDER BY revision"
        ).fetchall()
    )


def _journal_mode(connection: sqlite3.Connection) -> object:
    row = connection.execute("PRAGMA journal_mode").fetchone()
    return None if row is None else row[0]


def _quick_check(connection: sqlite3.Connection) -> None:
    budget = 100_000

    def progress() -> int:
        nonlocal budget
        budget -= 1_000
        return int(budget <= 0)

    try:
        connection.set_progress_handler(progress, 1_000)
        row = connection.execute("PRAGMA quick_check(1)").fetchone()
        if row is None or row[0] != "ok":
            raise ValueError
    except Exception as error:
        if is_sqlite_busy(error):
            raise
        raise StorageMigrationError() from None
    finally:
        with suppress(Exception):
            connection.set_progress_handler(None, 0)


def _state(revision: int, status: str) -> MigrationState:
    return MigrationState("service_store", revision, TARGET_REVISION, status)  # type: ignore[arg-type]
