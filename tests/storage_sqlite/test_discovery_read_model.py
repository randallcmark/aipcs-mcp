"""SYNTHETIC_FIXTURE. Read-only SQLite discovery and maintenance proof."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from fixtures import entity

from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.records import (
    MaintenanceQuery,
    SummaryQuery,
    compile_record_specification,
)
from aipcs_mcp.relational import compile_manifest
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteServiceStoreCatalog
from aipcs_mcp.storage.sqlite.codecs import encode_time
from aipcs_mcp.storage.sqlite.discovery import maintenance_scan, service_summary
from aipcs_mcp.storage.sqlite.domain_schema import SQLiteDomainSchemaStore
from aipcs_mcp.storage.sqlite.service_store_migrations import (
    R3_BRANCH_TABLE,
    R3_RECORD_BRANCH_TABLE,
)

PRINCIPAL = "discovery-principal"
OTHER = "other-principal"
NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)
BRANCH_ID = UUID(int=10)


def _manifest() -> ManifestV2:
    return ManifestV2.model_validate(
        {
            "manifest_version": 2,
            "schema_version": 1,
            "entities": [
                entity(
                    "memory",
                    [
                        {"name": "title", "type": "string", "required": True},
                        {
                            "name": "tags",
                            "type": "string_list",
                            "retrieval_mode": "membership",
                        },
                        {"name": "valid_until", "type": "datetime"},
                        {"name": "confidence", "type": "number"},
                        {"name": "status", "type": "string"},
                        {"name": "source_ref", "type": "string"},
                        {"name": "provenance_source", "type": "string"},
                        {"name": "superseded_by", "type": "uuid"},
                        {
                            "name": "annotation",
                            "type": "string",
                            "retrieval_mode": "annotation",
                        },
                    ],
                ),
                entity("plain", [{"name": "title", "type": "string", "required": True}]),
            ],
            "relationships": [],
            "indices": [
                {"name": "memory_owner_idx", "entity": "memory", "fields": ["owner_id"]},
                {"name": "plain_owner_idx", "entity": "plain", "fields": ["owner_id"]},
            ],
            "query_patterns": ["Find memory by status or tag."],
            "discovery_facets": [
                {"entity": "memory", "field": "owner_id"},
                {"entity": "memory", "field": "status"},
                {"entity": "memory", "field": "tags"},
            ],
            "retrieval_guidance": "Use declared facets before bounded samples.",
            "migration_history": [],
        }
    )


def _fixture(tmp_path: Path):
    root = tmp_path / "root"
    location = SQLiteLocationPolicy(root)
    catalog = SQLiteServiceStoreCatalog(location)
    locator = catalog.allocate(UUID(int=1))
    assert catalog.migrate(locator).status == "ready"
    manifest = _manifest()
    assert (
        SQLiteDomainSchemaStore(location).materialise(locator, compile_manifest(manifest)).status
        == "ready"
    )
    database = root / "service-stores" / f"{locator.namespace}.sqlite"
    connection = sqlite3.connect(database)
    _insert_fixture_rows(connection)
    connection.commit()
    connection.execute("PRAGMA query_only=ON")
    return connection, compile_record_specification(manifest)


def _insert_fixture_rows(connection: sqlite3.Connection) -> None:
    old = encode_time(NOW - timedelta(days=120))
    recent = encode_time(NOW - timedelta(days=1))
    future = encode_time(NOW + timedelta(days=30))
    expired = encode_time(NOW - timedelta(days=1))
    rows = [
        (
            str(UUID(int=1)),
            PRINCIPAL,
            old,
            old,
            "mcp",
            1,
            "first",
            '["alpha","shared"]',
            expired,
            0.1,
            "superseded",
            "duplicate-source",
            None,
            str(UUID(int=2)),
            "private-body-" + ("x" * 1_100),
        ),
        (
            str(UUID(int=2)),
            PRINCIPAL,
            recent,
            recent,
            "mcp",
            1,
            "second",
            '["beta","shared"]',
            future,
            0.9,
            "active",
            "duplicate-source",
            None,
            None,
            "short",
        ),
        (
            str(UUID(int=3)),
            PRINCIPAL,
            recent,
            recent,
            "mcp",
            1,
            "third",
            "[]",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        (
            str(UUID(int=4)),
            OTHER,
            old,
            old,
            "mcp",
            1,
            "other",
            '["shared"]',
            expired,
            0.0,
            "superseded",
            "duplicate-source",
            None,
            None,
            "other-private-body",
        ),
    ]
    connection.executemany(
        'INSERT INTO "memory" ("id","owner_id","created_at","updated_at","created_via",'
        '"record_version","title","tags","valid_until","confidence","status","source_ref",'
        '"provenance_source","superseded_by","annotation") '
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    connection.execute(
        'INSERT INTO "plain" ("id","owner_id","created_at","updated_at","created_via",'
        '"record_version","title") VALUES (?,?,?,?,?,?,?)',
        (str(UUID(int=20)), PRINCIPAL, old, old, "mcp", 1, "plain"),
    )
    branches = [
        (
            str(BRANCH_ID),
            PRINCIPAL,
            "current",
            "Current",
            "Current decisions",
            "topic",
            None,
            "active",
            "Current branch",
            1,
            recent,
            recent,
        ),
        (
            str(UUID(int=11)),
            OTHER,
            "other",
            "Other",
            "Other principal branch",
            None,
            None,
            "active",
            None,
            1,
            recent,
            recent,
        ),
    ]
    connection.executemany(
        f'INSERT INTO "{R3_BRANCH_TABLE}" '
        '("id","principal_id","slug","title","intent","branch_type","parent_branch_id",'
        '"status","retrieval_summary","branch_revision","created_at","updated_at") '
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        branches,
    )
    connection.executemany(
        f'INSERT INTO "{R3_RECORD_BRANCH_TABLE}" '
        '("principal_id","entity_name","record_id","branch_id","role","assigned_at") '
        "VALUES (?,?,?,?,?,?)",
        [
            (PRINCIPAL, "memory", str(UUID(int=1)), str(BRANCH_ID), "related", recent),
            (PRINCIPAL, "memory", str(UUID(int=2)), str(BRANCH_ID), "primary", recent),
            (OTHER, "memory", str(UUID(int=4)), str(UUID(int=11)), "primary", recent),
        ],
    )


def _schema(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
        ).fetchall()
    )


def test_summary_is_principal_scoped_bounded_deterministic_and_read_only(
    tmp_path: Path,
) -> None:
    connection, specification = _fixture(tmp_path)
    before_schema = _schema(connection)
    before_changes = connection.total_changes

    first = service_summary(
        connection,
        PRINCIPAL,
        specification,
        SummaryQuery((("memory", 2), ("plain", 0))),
    )
    second = service_summary(
        connection,
        PRINCIPAL,
        specification,
        SummaryQuery((("memory", 2), ("plain", 0))),
    )

    assert first == second
    assert first.data_status == "ready"
    assert first.record_counts == (("memory", 3), ("plain", 1))
    assert first.unbranched_record_count == 3
    assert [
        (facet.field_name, [(value.value, value.count) for value in facet.values])
        for facet in first.facets
    ] == [
        ("status", [("active", 1), ("superseded", 1)]),
        ("tags", [("shared", 2), ("alpha", 1), ("beta", 1)]),
    ]
    assert len(first.branches) == 1
    assert first.branches[0].branch.branch_id == BRANCH_ID
    assert first.branches[0].primary_record_count == 1
    assert first.branches[0].related_record_count == 1
    samples = dict(first.samples)
    assert [record.record_id for record in samples["memory"]] == [UUID(int=1), UUID(int=2)]
    assert samples["plain"] == ()
    assert all("owner_id" not in record.fields for record in samples["memory"])
    assert connection.total_changes == before_changes
    assert _schema(connection) == before_schema
    connection.close()


def test_summary_hard_bounds_facet_values_and_branch_cards(tmp_path: Path) -> None:
    connection, specification = _fixture(tmp_path)
    connection.execute("PRAGMA query_only=OFF")
    recent = encode_time(NOW - timedelta(days=1))
    connection.executemany(
        'INSERT INTO "memory" ("id","owner_id","created_at","updated_at","created_via",'
        '"record_version","title","tags","status") VALUES (?,?,?,?,?,?,?,?,?)',
        [
            (
                str(UUID(int=100 + index)),
                PRINCIPAL,
                recent,
                recent,
                "mcp",
                1,
                f"bounded-{index}",
                "[]",
                f"status-{index:02d}",
            )
            for index in range(22)
        ],
    )
    connection.executemany(
        f'INSERT INTO "{R3_BRANCH_TABLE}" '
        '("id","principal_id","slug","title","intent","branch_type","parent_branch_id",'
        '"status","retrieval_summary","branch_revision","created_at","updated_at") '
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                str(UUID(int=1_000 + index)),
                PRINCIPAL,
                f"branch-{index:03d}",
                f"Branch {index}",
                "Bounded branch fixture",
                None,
                None,
                "active",
                None,
                1,
                recent,
                recent,
            )
            for index in range(105)
        ],
    )
    connection.commit()
    connection.execute("PRAGMA query_only=ON")

    summary = service_summary(connection, PRINCIPAL, specification, SummaryQuery())

    status = next(facet for facet in summary.facets if facet.field_name == "status")
    assert len(status.values) == 20
    assert len(summary.branches) == 100
    connection.close()


def test_maintenance_returns_only_declared_mechanical_signals_and_no_content(
    tmp_path: Path,
) -> None:
    connection, specification = _fixture(tmp_path)
    before_schema = _schema(connection)
    before_changes = connection.total_changes
    scans = (
        "expired",
        "stale",
        "low_confidence",
        "superseded",
        "missing_authority",
        "unbranched",
        "duplicate_authority",
        "blob_candidate",
    )

    result = maintenance_scan(
        connection,
        PRINCIPAL,
        specification,
        MaintenanceQuery(scans, entity_name="memory"),
        now=NOW,
    )

    observed = [(candidate.scan_type, candidate.record_id) for candidate in result.candidates]
    assert observed == [
        ("blob_candidate", UUID(int=1)),
        ("duplicate_authority", UUID(int=1)),
        ("duplicate_authority", UUID(int=2)),
        ("expired", UUID(int=1)),
        ("low_confidence", UUID(int=1)),
        ("missing_authority", UUID(int=3)),
        ("stale", UUID(int=1)),
        ("superseded", UUID(int=1)),
        ("unbranched", UUID(int=1)),
        ("unbranched", UUID(int=3)),
    ]
    assert result.unavailable_scan_types == ()
    rendered = repr(result)
    assert "private-body" not in rendered
    assert "duplicate-source" not in rendered
    assert connection.total_changes == before_changes
    assert _schema(connection) == before_schema
    connection.close()


def test_maintenance_bounds_candidates_scopes_branch_and_reports_unavailable_signals(
    tmp_path: Path,
) -> None:
    connection, specification = _fixture(tmp_path)
    all_scans = (
        "expired",
        "stale",
        "low_confidence",
        "superseded",
        "missing_authority",
        "unbranched",
        "duplicate_authority",
        "blob_candidate",
    )
    bounded = maintenance_scan(
        connection,
        PRINCIPAL,
        specification,
        MaintenanceQuery(all_scans, entity_name="memory", max_candidates=4),
        now=NOW,
    )
    assert [(item.scan_type, item.record_id) for item in bounded.candidates] == [
        ("blob_candidate", UUID(int=1)),
        ("duplicate_authority", UUID(int=1)),
        ("duplicate_authority", UUID(int=2)),
        ("expired", UUID(int=1)),
    ]
    scoped = maintenance_scan(
        connection,
        PRINCIPAL,
        specification,
        MaintenanceQuery(all_scans, entity_name="memory", branch_id=BRANCH_ID),
        now=NOW,
    )
    assert all(item.record_id in {UUID(int=1), UUID(int=2)} for item in scoped.candidates)
    assert ("missing_authority", UUID(int=3)) not in {
        (item.scan_type, item.record_id) for item in scoped.candidates
    }

    unavailable = maintenance_scan(
        connection,
        PRINCIPAL,
        specification,
        MaintenanceQuery(all_scans, entity_name="plain"),
        now=NOW,
    )
    assert unavailable.unavailable_scan_types == (
        "blob_candidate",
        "duplicate_authority",
        "expired",
        "low_confidence",
        "missing_authority",
        "superseded",
    )
    assert [(item.scan_type, item.record_id) for item in unavailable.candidates] == [
        ("stale", UUID(int=20)),
        ("unbranched", UUID(int=20)),
    ]
    connection.close()
