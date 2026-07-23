"""Immutable evidence rules for the v1 SQLite WAL physical policy."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

WAL_POLICY_ID = "aipcs.sqlite.wal.v1"
WAL_JOURNAL_MODE_OPERATION = "PRAGMA journal_mode=WAL"


class _MigrationDescriptor(Protocol):
    revision: int
    migration_id: str
    checksum: str


class WALPolicyState(StrEnum):
    """The only physical states a DELETE-to-WAL migration may adopt."""

    CLEAN_PREDECESSOR = "clean_predecessor"
    PREPARED_DELETE = "prepared_delete"
    PREPARED_WAL = "prepared_wal"
    READY_WAL = "ready_wal"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True)
class WALPolicyDescriptor:
    """The permanent marker and checksum for one component's WAL migration."""

    table_name: str
    ddl: str
    checksum: str


@dataclass(frozen=True)
class WALPolicyEvidence:
    """Transaction-neutral evidence assembled by a component-specific inspector."""

    revision: int
    dirty: int
    journal_mode: object
    layout_matches: bool
    history_matches: bool
    policy_table_sql: object | None
    policy_rows: tuple[tuple[object, ...], ...]


def policy_table_ddl(table_name: str) -> str:
    """Return the frozen WAL marker DDL for one controlled table name."""

    return f'''CREATE TABLE "{table_name}" (
    "singleton" INTEGER PRIMARY KEY NOT NULL CHECK ("singleton" = 1),
    "policy_id" TEXT NOT NULL CHECK ("policy_id" = 'aipcs.sqlite.wal.v1'),
    "policy_checksum" TEXT NOT NULL CHECK (
        length("policy_checksum") = 64
        AND "policy_checksum" = lower("policy_checksum")
        AND "policy_checksum" NOT GLOB '*[^0-9a-f]*'
    ),
    "phase" TEXT NOT NULL CHECK ("phase" IN ('prepared', 'ready'))
) STRICT'''


def policy_checksum(ddl: str) -> str:
    """Checksum exact DDL plus the literal persistent WAL operation."""

    return hashlib.sha256(f"{ddl}\n{WAL_JOURNAL_MODE_OPERATION}".encode()).hexdigest()


def policy_table_matches(sql: object, descriptor: WALPolicyDescriptor) -> bool:
    return sql == descriptor.ddl


def journal_mode_matches(mode: object, expected: str) -> bool:
    """Require SQLite's canonical lower-case persistent journal-mode result."""

    return type(mode) is str and mode == expected


def policy_row_matches(
    rows: tuple[tuple[object, ...], ...], descriptor: WALPolicyDescriptor, phase: str
) -> bool:
    """Require the one exact policy evidence row, without coercing SQLite values."""

    if len(rows) != 1 or len(rows[0]) != 4:
        return False
    singleton, policy_id, checksum, observed_phase = rows[0]
    return (
        type(singleton) is int
        and singleton == 1
        and type(policy_id) is str
        and policy_id == WAL_POLICY_ID
        and type(checksum) is str
        and checksum == descriptor.checksum
        and type(observed_phase) is str
        and observed_phase == phase
    )


def history_matches(
    rows: tuple[tuple[object, ...], ...],
    component: str,
    descriptors: tuple[_MigrationDescriptor, ...],
    revision: int,
) -> bool:
    """Validate an exact migration-history prefix including canonical UTC timestamps."""

    if type(revision) is not int or revision < 0 or revision > len(descriptors):
        return False
    if len(rows) != revision:
        return False
    for row, descriptor in zip(rows, descriptors[:revision], strict=True):
        if (
            len(row) != 5
            or type(row[0]) is not str
            or row[0] != component
            or type(row[1]) is not int
            or row[1] != descriptor.revision
            or type(row[2]) is not str
            or row[2] != descriptor.migration_id
            or type(row[3]) is not str
            or row[3] != descriptor.checksum
        ):
            return False
        if not _canonical_timestamp(row[4]):
            return False
    return True


def classify_wal_policy(
    evidence: WALPolicyEvidence,
    *,
    predecessor_revision: int,
    target_revision: int,
    descriptor: WALPolicyDescriptor,
) -> WALPolicyState:
    """Classify only exact versioned physical states; all altered evidence fails closed."""

    mode = evidence.journal_mode
    if type(evidence.revision) is not int or type(evidence.dirty) is not int or evidence.dirty not in (
        0,
        1,
    ):
        return WALPolicyState.INCOMPATIBLE
    no_marker = evidence.policy_table_sql is None and not evidence.policy_rows
    marker_matches = policy_table_matches(evidence.policy_table_sql, descriptor)

    if (
        evidence.revision == predecessor_revision
        and evidence.dirty == 0
        and journal_mode_matches(mode, "delete")
        and evidence.layout_matches
        and evidence.history_matches
        and no_marker
    ):
        return WALPolicyState.CLEAN_PREDECESSOR
    if (
        evidence.revision == predecessor_revision
        and evidence.dirty == 1
        and evidence.layout_matches
        and evidence.history_matches
        and marker_matches
        and policy_row_matches(evidence.policy_rows, descriptor, "prepared")
    ):
        if journal_mode_matches(mode, "delete"):
            return WALPolicyState.PREPARED_DELETE
        if journal_mode_matches(mode, "wal"):
            return WALPolicyState.PREPARED_WAL
    if (
        evidence.revision == target_revision
        and evidence.dirty == 0
        and journal_mode_matches(mode, "wal")
        and evidence.layout_matches
        and evidence.history_matches
        and marker_matches
        and policy_row_matches(evidence.policy_rows, descriptor, "ready")
    ):
        return WALPolicyState.READY_WAL
    return WALPolicyState.INCOMPATIBLE


def _canonical_timestamp(value: object) -> bool:
    if type(value) is not str or len(value) != 27:
        return False
    try:
        from datetime import UTC, datetime

        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") == value
