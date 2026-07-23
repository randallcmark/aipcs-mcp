"""Focused transactional semantics for the SQLite branch-topology helper."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from uuid import UUID

from fixtures import valid_manifest

from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.records import (
    AssignBranchRecordsCommand,
    BranchAssignmentTarget,
    CreateBranchCommand,
    FrozenJsonObject,
    ListBranchesQuery,
    PageRequest,
    UpdateBranchCommand,
    compile_record_specification,
)
from aipcs_mcp.storage.sqlite.codecs import encode_time
from aipcs_mcp.storage.sqlite.service_store_migrations import (
    R3_DDL,
    R3_HISTORY_TABLE,
    R3_RECORD_BRANCH_TABLE,
)
from aipcs_mcp.storage.sqlite.topology import SQLiteBranchTopology

_PRINCIPAL = "principal"
_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
_RECORD_ONE = UUID("00000000-0000-0000-0000-000000000101")
_RECORD_TWO = UUID("00000000-0000-0000-0000-000000000102")
_ROOT = UUID("00000000-0000-0000-0000-000000000201")
_CHILD = UUID("00000000-0000-0000-0000-000000000202")


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    for ddl in R3_DDL:
        connection.execute(ddl)
    connection.execute(
        'CREATE TABLE "project" ('
        '"id" TEXT PRIMARY KEY, "owner_id" TEXT NOT NULL, "created_at" TEXT NOT NULL, '
        '"updated_at" TEXT NOT NULL, "created_via" TEXT NOT NULL, '
        '"record_version" INTEGER NOT NULL, "title" TEXT NOT NULL)'
    )
    for identifier, title in ((_RECORD_ONE, "one"), (_RECORD_TWO, "two")):
        connection.execute(
            'INSERT INTO "project" VALUES (?,?,?,?,?,?,?)',
            (str(identifier), _PRINCIPAL, encode_time(_NOW), encode_time(_NOW), "mcp", 1, title),
        )
    return connection


def _topology() -> SQLiteBranchTopology:
    branch_ids = iter((_ROOT, _CHILD))
    return SQLiteBranchTopology(clock=lambda: _NOW, branch_ids=lambda: next(branch_ids))


def _specification():  # type: ignore[no-untyped-def]
    return compile_record_specification(ManifestV2.model_validate(valid_manifest()))


def _create(topology: SQLiteBranchTopology, connection: sqlite3.Connection, slug: str, parent=None):  # type: ignore[no-untyped-def]
    return topology.create_branch(
        connection,
        _PRINCIPAL,
        "mcp",
        CreateBranchCommand(slug, slug.title(), "intent", None, parent, None, f"create-{slug}"),
    )


def test_branch_create_list_patch_and_cycle_rejection_are_principal_scoped() -> None:
    connection = _connection()
    topology = _topology()
    root = _create(topology, connection, "root")
    child = _create(topology, connection, "child", _ROOT)

    assert root.branch.branch_id == _ROOT
    assert child.branch.parent_branch_id == _ROOT
    page = topology.list_branches(connection, _PRINCIPAL, ListBranchesQuery(page=PageRequest(limit=1)))
    assert [item.slug for item in page.branches] == ["root"]
    assert page.next_cursor is not None
    following = topology.list_branches(
        connection, _PRINCIPAL, ListBranchesQuery(page=PageRequest(limit=1, cursor=page.next_cursor))
    )
    assert [item.slug for item in following.branches] == ["child"]

    cycle = topology.update_branch(
        connection,
        _PRINCIPAL,
        "mcp",
        UpdateBranchCommand(
            _ROOT,
            FrozenJsonObject.from_mapping({"parent_branch_id": str(_CHILD)}),
            1,
            "cycle",
        ),
    )
    assert cycle.code == "constraint_violation"
    patched = topology.update_branch(
        connection,
        _PRINCIPAL,
        "mcp",
        UpdateBranchCommand(_ROOT, FrozenJsonObject.from_mapping({"branch_type": None}), 1, "patch"),
    )
    assert patched.branch.branch_revision == 1  # explicit null no-op stays a no-op


def test_assignment_is_atomic_idempotent_and_preserves_one_primary_many_related() -> None:
    connection = _connection()
    topology = _topology()
    _create(topology, connection, "root")
    _create(topology, connection, "child", _ROOT)
    specification = _specification()

    first = AssignBranchRecordsCommand(
        _ROOT,
        "primary",
        "assign",
        (BranchAssignmentTarget("project", _RECORD_ONE, 1),),
        "first",
    )
    assigned = topology.assign_records(connection, _PRINCIPAL, "mcp", specification, first)
    assert assigned.records[0].record_version == 2
    replay = topology.assign_records(connection, _PRINCIPAL, "mcp", specification, first)
    assert replay.replayed is True
    assert replay.records == assigned.records

    moved = topology.assign_records(
        connection,
        _PRINCIPAL,
        "mcp",
        specification,
        AssignBranchRecordsCommand(
            _CHILD,
            "primary",
            "assign",
            (BranchAssignmentTarget("project", _RECORD_ONE, 2),),
            "move",
        ),
    )
    assert moved.records[0].record_version == 3
    related = topology.assign_records(
        connection,
        _PRINCIPAL,
        "mcp",
        specification,
        AssignBranchRecordsCommand(
            _ROOT,
            "related",
            "assign",
            (BranchAssignmentTarget("project", _RECORD_ONE, 3),),
            "related",
        ),
    )
    assert related.records[0].record_version == 4
    assert [row["role"] for row in connection.execute(
        f'SELECT "role" FROM "{R3_RECORD_BRANCH_TABLE}" WHERE "record_id"=? ORDER BY "role"',
        (str(_RECORD_ONE),),
    )] == ["primary", "related"]

    removed = topology.assign_records(
        connection,
        _PRINCIPAL,
        "mcp",
        specification,
        AssignBranchRecordsCommand(
            _ROOT,
            "related",
            "unassign",
            (BranchAssignmentTarget("project", _RECORD_ONE, 4),),
            "remove",
        ),
    )
    assert removed.records[0].record_version == 5
    no_effect = topology.assign_records(
        connection,
        _PRINCIPAL,
        "mcp",
        specification,
        AssignBranchRecordsCommand(
            _ROOT,
            "related",
            "unassign",
            (BranchAssignmentTarget("project", _RECORD_ONE, 5),),
            "remove-noop",
        ),
    )
    assert no_effect.records[0].record_version == 5
    assert [item[0] for item in connection.execute(
        f'SELECT "event_kind" FROM "{R3_HISTORY_TABLE}" ORDER BY "event_sequence"'
    )] == ["primary_assign", "primary_move", "related_assign", "related_unassign"]

    stale_batch = topology.assign_records(
        connection,
        _PRINCIPAL,
        "mcp",
        specification,
        AssignBranchRecordsCommand(
            _ROOT,
            "related",
            "assign",
            (
                BranchAssignmentTarget("project", _RECORD_ONE, 5),
                BranchAssignmentTarget("project", _RECORD_TWO, 99),
            ),
            "atomic",
        ),
    )
    assert stale_batch.code == "stale_record_version"
    assert connection.execute(
        f'SELECT COUNT(*) FROM "{R3_RECORD_BRANCH_TABLE}" WHERE "record_id"=? AND "branch_id"=?',
        (str(_RECORD_ONE), str(_ROOT)),
    ).fetchone()[0] == 0


def test_inactive_branch_rejects_new_assignment_and_idempotency_is_fingerprint_bound() -> None:
    connection = _connection()
    topology = _topology()
    _create(topology, connection, "root")
    archived = topology.update_branch(
        connection,
        _PRINCIPAL,
        "mcp",
        UpdateBranchCommand(_ROOT, FrozenJsonObject.from_mapping({"status": "archived"}), 1, "archive"),
    )
    assert archived.branch.status == "archived"
    outcome = topology.assign_records(
        connection,
        _PRINCIPAL,
        "mcp",
        _specification(),
        AssignBranchRecordsCommand(
            _ROOT,
            "related",
            "assign",
            (BranchAssignmentTarget("project", _RECORD_ONE, 1),),
            "cannot-assign",
        ),
    )
    assert outcome.code == "constraint_violation"
    changed = topology.create_branch(
        connection,
        _PRINCIPAL,
        "other-client",
        CreateBranchCommand("root", "Root", "intent", None, None, None, "create-root"),
    )
    assert changed.code == "changed_fingerprint"
