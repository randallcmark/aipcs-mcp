"""SYNTHETIC_FIXTURE. PostgreSQL topology helper, transaction-owned by the caller."""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
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
from aipcs_mcp.storage.postgresql.connection import PostgreSQLDsn
from aipcs_mcp.storage.postgresql.service_store import PostgreSQLServiceStoreCatalog
from aipcs_mcp.storage.postgresql.service_store_migrations import HISTORY, RECORD_BRANCH, qualified
from aipcs_mcp.storage.postgresql.topology import PostgreSQLBranchTopology

from .container import PostgresTestTarget

_NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)
_PRINCIPAL = "principal"
_RECORD_ONE = UUID("00000000-0000-0000-0000-000000000101")
_RECORD_TWO = UUID("00000000-0000-0000-0000-000000000102")
_ROOT = UUID("00000000-0000-0000-0000-000000000201")
_CHILD = UUID("00000000-0000-0000-0000-000000000202")


def _specification():  # type: ignore[no-untyped-def]
    return compile_record_specification(ManifestV2.model_validate(valid_manifest()))


def _ready(target: PostgresTestTarget):  # type: ignore[no-untyped-def]
    catalog = PostgreSQLServiceStoreCatalog(PostgreSQLDsn(target.dsn))
    locator = catalog.allocate(UUID(int=31))
    assert catalog.migrate(locator).status == "ready"
    driver = import_module("psycopg")
    connection = driver.connect(target.dsn)
    connection.execute(
        f"CREATE TABLE {qualified(locator.namespace, 'project')} ("
        'id uuid PRIMARY KEY, owner_id text COLLATE "C" NOT NULL, '
        "created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL, "
        'created_via text COLLATE "C" NOT NULL, record_version bigint NOT NULL, '
        'title text COLLATE "C" NOT NULL)'
    )
    for record_id, title in ((_RECORD_ONE, "one"), (_RECORD_TWO, "two")):
        connection.execute(
            f"INSERT INTO {qualified(locator.namespace, 'project')} VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (record_id, _PRINCIPAL, _NOW, _NOW, "mcp", 1, title),
        )
    connection.commit()
    return connection, locator.namespace


def _topology() -> PostgreSQLBranchTopology:
    values = iter((_ROOT, _CHILD))
    return PostgreSQLBranchTopology(clock=lambda: _NOW, branch_ids=lambda: next(values))


def _create(topology: PostgreSQLBranchTopology, connection, namespace: str, slug: str, parent=None):  # type: ignore[no-untyped-def]
    return topology.create_branch(
        connection,
        namespace,
        _PRINCIPAL,
        "mcp",
        CreateBranchCommand(slug, slug.title(), "intent", None, parent, None, f"create-{slug}"),
    )


def test_topology_uses_the_callers_transaction_and_scopes_branches_to_principal(
    postgres_test_target: PostgresTestTarget,
) -> None:
    connection, namespace = _ready(postgres_test_target)
    try:
        topology = _topology()
        connection.execute("BEGIN")
        root = _create(topology, connection, namespace, "root")
        child = _create(topology, connection, namespace, "child", _ROOT)
        assert root.branch.branch_id == _ROOT
        assert child.branch.parent_branch_id == _ROOT
        assert connection.info.transaction_status.name == "INTRANS"
        connection.commit()

        connection.execute("BEGIN READ ONLY")
        first = topology.list_branches(connection, namespace, _PRINCIPAL, ListBranchesQuery(page=PageRequest(1)))
        assert [item.slug for item in first.branches] == ["root"]
        assert first.next_cursor is not None
        next_page = topology.list_branches(
            connection, namespace, _PRINCIPAL, ListBranchesQuery(page=PageRequest(1, first.next_cursor))
        )
        assert [item.slug for item in next_page.branches] == ["child"]
        assert topology.list_branches(connection, namespace, "other", ListBranchesQuery()).branches == ()
        connection.rollback()

        connection.execute("BEGIN")
        cycle = topology.update_branch(
            connection,
            namespace,
            _PRINCIPAL,
            "mcp",
            UpdateBranchCommand(_ROOT, FrozenJsonObject.from_mapping({"parent_branch_id": str(_CHILD)}), 1, "cycle"),
        )
        assert cycle.code == "constraint_violation"
        patched = topology.update_branch(
            connection,
            namespace,
            _PRINCIPAL,
            "mcp",
            UpdateBranchCommand(_ROOT, FrozenJsonObject.from_mapping({"status": "archived"}), 1, "archive"),
        )
        assert patched.branch.branch_revision == 2
        assert topology.update_branch(
            connection,
            namespace,
            _PRINCIPAL,
            "mcp",
            UpdateBranchCommand(_ROOT, FrozenJsonObject.from_mapping({"status": "active"}), 1, "stale"),
        ).code == "stale_branch_revision"
        connection.commit()
    finally:
        connection.close()


def test_topology_assignment_is_atomic_replay_bound_and_preserves_roles(
    postgres_test_target: PostgresTestTarget,
) -> None:
    connection, namespace = _ready(postgres_test_target)
    try:
        topology, specification = _topology(), _specification()
        connection.execute("BEGIN")
        _create(topology, connection, namespace, "root")
        _create(topology, connection, namespace, "child", _ROOT)
        first = AssignBranchRecordsCommand(_ROOT, "primary", "assign", (BranchAssignmentTarget("project", _RECORD_ONE, 1),), "first")
        assigned = topology.assign_records(connection, namespace, _PRINCIPAL, "mcp", specification, first)
        assert assigned.records[0].record_version == 2
        assert topology.assign_records(connection, namespace, _PRINCIPAL, "mcp", specification, first).replayed
        moved = topology.assign_records(connection, namespace, _PRINCIPAL, "mcp", specification, AssignBranchRecordsCommand(_CHILD, "primary", "assign", (BranchAssignmentTarget("project", _RECORD_ONE, 2),), "move"))
        related = topology.assign_records(connection, namespace, _PRINCIPAL, "mcp", specification, AssignBranchRecordsCommand(_ROOT, "related", "assign", (BranchAssignmentTarget("project", _RECORD_ONE, 3),), "related"))
        assert (moved.records[0].record_version, related.records[0].record_version) == (3, 4)
        removed = topology.assign_records(connection, namespace, _PRINCIPAL, "mcp", specification, AssignBranchRecordsCommand(_ROOT, "related", "unassign", (BranchAssignmentTarget("project", _RECORD_ONE, 4),), "remove"))
        assert removed.records[0].record_version == 5
        stale = topology.assign_records(connection, namespace, _PRINCIPAL, "mcp", specification, AssignBranchRecordsCommand(_ROOT, "related", "assign", (BranchAssignmentTarget("project", _RECORD_ONE, 4), BranchAssignmentTarget("project", _RECORD_TWO, 99)), "atomic"))
        assert stale.code == "stale_record_version"
        assert connection.execute(
            f"SELECT role FROM {qualified(namespace, RECORD_BRANCH)} WHERE record_id=%s ORDER BY role",
            (_RECORD_ONE,),
        ).fetchall() == [("primary",)]
        assert connection.execute(
            f"SELECT event_kind FROM {qualified(namespace, HISTORY)} WHERE record_id=%s ORDER BY event_sequence",
            (_RECORD_ONE,),
        ).fetchall() == [
            ("primary_assign",),
            ("primary_move",),
            ("related_assign",),
            ("related_unassign",),
        ]
        connection.commit()
    finally:
        connection.close()
