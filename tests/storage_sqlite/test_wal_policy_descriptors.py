"""Exact compiled evidence for the v1 SQLite WAL physical-policy descriptors."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from aipcs_mcp.storage.sqlite import migrations as registry
from aipcs_mcp.storage.sqlite import service_store_migrations as service_store
from aipcs_mcp.storage.sqlite.wal_policy import (
    WAL_JOURNAL_MODE_OPERATION,
    WAL_POLICY_ID,
    WALPolicyEvidence,
    WALPolicyState,
    classify_wal_policy,
    history_matches,
)


@pytest.mark.parametrize(
    ("module", "descriptor", "expected_checksum", "expected_table"),
    (
        (
            registry,
            registry.R3_POLICY,
            "661d993c71dfe09a237e8f8018a9636b154ae5f9d39deabcbb8a55b6e49baade",
            "aipcs_registry_storage_policy",
        ),
        (
            service_store,
            service_store.R2_POLICY,
            "d639ef1e1a897e6eaa5109676417f4ab1bc9bbd7a56ce798eee8e2816ce4d8f5",
            "__aipcs_service_store_policy",
        ),
    ),
)
def test_wal_policy_descriptor_is_exact_strict_singleton_evidence(
    module, descriptor, expected_checksum: str, expected_table: str
) -> None:
    assert descriptor.table_name == expected_table
    assert descriptor.checksum == expected_checksum
    assert expected_checksum == descriptor.checksum
    assert expected_checksum == hashlib.sha256(
        f"{descriptor.ddl}\n{WAL_JOURNAL_MODE_OPERATION}".encode()
    ).hexdigest()
    assert (descriptor.ddl,) == (
        registry.R3_DDL if module is registry else service_store.R2_DDL
    )
    assert (
        registry.R3_MIGRATION_ID if module is registry else service_store.R2_MIGRATION_ID
    ).endswith("-wal-policy")
    assert descriptor.ddl.endswith(") STRICT")
    assert f"{descriptor.ddl}\n{WAL_JOURNAL_MODE_OPERATION}".endswith(
        f"\n{WAL_JOURNAL_MODE_OPERATION}"
    )

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(descriptor.ddl)
        assert connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name=?", (expected_table,)
        ).fetchone() == (descriptor.ddl,)
        assert tuple(map(tuple, connection.execute(f'PRAGMA table_xinfo("{expected_table}")'))) == (
            (0, "singleton", "INTEGER", 1, None, 1, 0),
            (1, "policy_id", "TEXT", 1, None, 0, 0),
            (2, "policy_checksum", "TEXT", 1, None, 0, 0),
            (3, "phase", "TEXT", 1, None, 0, 0),
        )
        connection.execute(
            f'INSERT INTO "{expected_table}" VALUES (?, ?, ?, ?)',
            (1, WAL_POLICY_ID, descriptor.checksum, "prepared"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f'INSERT INTO "{expected_table}" VALUES (?, ?, ?, ?)',
                (2, WAL_POLICY_ID, descriptor.checksum, "ready"),
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("module", "descriptors", "predecessor", "target", "policy"),
    (
        # R3 is an immutable physical-policy epoch even when later logical
        # registry migrations are present.
        (registry, registry.MIGRATIONS[:3], 2, 3, registry.R3_POLICY),
        (service_store, service_store.MIGRATIONS[:2], 1, 2, service_store.R2_POLICY),
    ),
)
def test_wal_classifier_adopts_only_the_four_exact_states(
    module, descriptors, predecessor: int, target: int, policy
) -> None:
    def evidence(
        revision: int,
        dirty: int,
        mode: str,
        phase: str | None,
        *,
        matches: bool = True,
    ) -> WALPolicyEvidence:
        return WALPolicyEvidence(
            revision=revision,
            dirty=dirty,
            journal_mode=mode,
            layout_matches=matches,
            history_matches=matches,
            policy_table_sql=None if phase is None else policy.ddl,
            policy_rows=()
            if phase is None
            else ((1, WAL_POLICY_ID, policy.checksum, phase),),
        )

    assert classify_wal_policy(
        evidence(predecessor, 0, "delete", None),
        predecessor_revision=predecessor,
        target_revision=target,
        descriptor=policy,
    ) == WALPolicyState.CLEAN_PREDECESSOR
    assert classify_wal_policy(
        evidence(predecessor, 1, "delete", "prepared"),
        predecessor_revision=predecessor,
        target_revision=target,
        descriptor=policy,
    ) == WALPolicyState.PREPARED_DELETE
    assert classify_wal_policy(
        evidence(predecessor, 1, "wal", "prepared"),
        predecessor_revision=predecessor,
        target_revision=target,
        descriptor=policy,
    ) == WALPolicyState.PREPARED_WAL
    assert classify_wal_policy(
        evidence(target, 0, "wal", "ready"),
        predecessor_revision=predecessor,
        target_revision=target,
        descriptor=policy,
    ) == WALPolicyState.READY_WAL

    for altered in (
        evidence(predecessor, 0, "wal", None),
        evidence(target, 0, "delete", "ready"),
        evidence(predecessor, 1, "wal", "ready"),
        evidence(target, 1, "wal", "ready"),
        evidence(target, 0, "wal", "ready", matches=False),
        WALPolicyEvidence(
            target,
            0,
            "wal",
            True,
            True,
            policy.ddl,
            ((1, WAL_POLICY_ID, "0" * 64, "ready"),),
        ),
        WALPolicyEvidence(target, False, "wal", True, True, policy.ddl, ()),
    ):
        assert classify_wal_policy(
            altered,
            predecessor_revision=predecessor,
            target_revision=target,
            descriptor=policy,
        ) == WALPolicyState.INCOMPATIBLE

    component = "registry" if module is registry else "service_store"
    rows = tuple(
        (component, item.revision, item.migration_id, item.checksum, "2026-07-23T00:00:00.000000Z")
        for item in descriptors
    )
    assert history_matches(rows, component, descriptors, target)
    assert not history_matches(rows[:-1], component, descriptors, target)
    assert not history_matches(
        rows[:-1] + ((component, target, "altered", policy.checksum, rows[-1][4]),),
        component,
        descriptors,
        target,
    )


def test_historical_descriptor_checksums_remain_frozen() -> None:
    assert registry.R1_CHECKSUM == "d40691d8ae8e09b10767b262ac716bc1689c52f4887770d9f43cd84679d291bc"
    assert registry.R2_CHECKSUM == "b6190247d4f709728bab59cb4eb5fd149d4b7424472615f377b83f5191e0d8ea"
    assert service_store.R1_CHECKSUM == (
        "e56f32848bf4f5b762abe4a164e5869658360408d908da73a6b75d8f52c42f1b"
    )
