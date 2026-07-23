"""SYNTHETIC_FIXTURE. Managed PostgreSQL discovery and maintenance proof."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fixtures import entity

from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.records import MaintenanceQuery, SummaryQuery, compile_record_specification
from aipcs_mcp.relational import compile_manifest
from aipcs_mcp.storage.postgresql.connection import (
    PostgreSQLConnectionPolicy,
    PostgreSQLDsn,
    connect_postgresql,
)
from aipcs_mcp.storage.postgresql.data_core import qualified
from aipcs_mcp.storage.postgresql.discovery import maintenance_scan, service_summary
from aipcs_mcp.storage.postgresql.domain_schema import PostgreSQLDomainSchemaStore
from aipcs_mcp.storage.postgresql.service_store import PostgreSQLServiceStoreCatalog
from aipcs_mcp.storage.postgresql.service_store_migrations import BRANCH, RECORD_BRANCH

from .container import PostgresTestTarget

PRINCIPAL = "discovery-principal"
OTHER = "other-principal"
NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)
BRANCH_ID = UUID(int=10)


class RecordingConnection:
    """Record only helper-issued statements while delegating driver behaviour."""

    def __init__(self, connection: object) -> None:
        self.connection = connection
        self.statements: list[str] = []

    def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> object:
        self.statements.append(statement)
        return self.connection.execute(statement, parameters)  # type: ignore[attr-defined,no-any-return]


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


def _connection(target: PostgresTestTarget, namespace: str) -> object:
    return connect_postgresql(
        PostgreSQLDsn(target.dsn),
        PostgreSQLConnectionPolicy(namespace, 10, 5_000, 30_000),
    )


def _insert_fixture(connection: object, namespace: str) -> None:
    old = NOW - timedelta(days=120)
    recent = NOW - timedelta(days=1)
    future = NOW + timedelta(days=30)
    expired = NOW - timedelta(days=1)
    rows = (
        (
            UUID(int=1),
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
            UUID(int=2),
            "private-body-" + ("x" * 1_100),
        ),
        (
            UUID(int=2),
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
            UUID(int=3),
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
            UUID(int=4),
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
    )
    for row in rows:
        connection.execute(  # type: ignore[attr-defined]
            f"INSERT INTO {qualified(namespace, 'memory')} "
            '("id","owner_id","created_at","updated_at","created_via","record_version",'
            '"title","tags","valid_until","confidence","status","source_ref",'
            '"provenance_source","superseded_by","annotation") '
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s)",
            row,
        )
    connection.execute(  # type: ignore[attr-defined]
        f"INSERT INTO {qualified(namespace, 'plain')} "
        '("id","owner_id","created_at","updated_at","created_via","record_version","title") '
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (UUID(int=20), PRINCIPAL, old, old, "mcp", 1, "plain"),
    )
    connection.execute(  # type: ignore[attr-defined]
        f"INSERT INTO {qualified(namespace, BRANCH)} "
        '("id","principal_id","slug","title","intent","branch_type","parent_branch_id",'
        '"status","retrieval_summary","branch_revision","created_at","updated_at") '
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            BRANCH_ID,
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
    )
    connection.execute(  # type: ignore[attr-defined]
        f"INSERT INTO {qualified(namespace, BRANCH)} "
        '("id","principal_id","slug","title","intent","branch_type","parent_branch_id",'
        '"status","retrieval_summary","branch_revision","created_at","updated_at") '
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            UUID(int=11),
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
    )
    for assignment in (
        (PRINCIPAL, "memory", UUID(int=1), BRANCH_ID, "related", recent),
        (PRINCIPAL, "memory", UUID(int=2), BRANCH_ID, "primary", recent),
        (OTHER, "memory", UUID(int=4), UUID(int=11), "primary", recent),
    ):
        connection.execute(  # type: ignore[attr-defined]
            f"INSERT INTO {qualified(namespace, RECORD_BRANCH)} "
            '("principal_id","entity_name","record_id","branch_id","role","assigned_at") '
            "VALUES (%s,%s,%s,%s,%s,%s)",
            assignment,
        )


def test_discovery_and_maintenance_are_scoped_deterministic_and_transaction_neutral(
    postgres_test_target: PostgresTestTarget,
) -> None:
    dsn = PostgreSQLDsn(postgres_test_target.dsn)
    catalog = PostgreSQLServiceStoreCatalog(dsn)
    locator = catalog.allocate(UUID(int=1))
    assert catalog.migrate(locator).status == "ready"
    manifest = _manifest()
    assert (
        PostgreSQLDomainSchemaStore(dsn, service_store_catalog=catalog)
        .materialise(locator, compile_manifest(manifest))
        .status
        == "ready"
    )
    specification = compile_record_specification(manifest)
    connection = _connection(postgres_test_target, locator.namespace)
    _insert_fixture(connection, locator.namespace)
    connection.commit()  # type: ignore[attr-defined]
    connection.execute("BEGIN READ ONLY")  # type: ignore[attr-defined]
    recording = RecordingConnection(connection)

    query = SummaryQuery((("memory", 2), ("plain", 0)))
    first = service_summary(recording, locator.namespace, PRINCIPAL, specification, query)
    second = service_summary(recording, locator.namespace, PRINCIPAL, specification, query)

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
    assert samples["memory"][0].fields["valid_until"] == "2026-07-22T12:00:00.000000Z"
    assert samples["memory"][0].fields["superseded_by"] == str(UUID(int=2))
    assert all("owner_id" not in record.fields for record in samples["memory"])

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
        recording,
        locator.namespace,
        PRINCIPAL,
        specification,
        MaintenanceQuery(scans, entity_name="memory"),
        now=NOW,
    )
    assert [(candidate.scan_type, candidate.record_id) for candidate in result.candidates] == [
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
    assert "private-body" not in repr(result)
    assert "duplicate-source" not in repr(result)

    bounded = maintenance_scan(
        recording,
        locator.namespace,
        PRINCIPAL,
        specification,
        MaintenanceQuery(scans, entity_name="memory", max_candidates=4),
        now=NOW,
    )
    assert [(candidate.scan_type, candidate.record_id) for candidate in bounded.candidates] == [
        ("blob_candidate", UUID(int=1)),
        ("duplicate_authority", UUID(int=1)),
        ("duplicate_authority", UUID(int=2)),
        ("expired", UUID(int=1)),
    ]
    scoped = maintenance_scan(
        recording,
        locator.namespace,
        PRINCIPAL,
        specification,
        MaintenanceQuery(scans, entity_name="memory", branch_id=BRANCH_ID),
        now=NOW,
    )
    assert all(candidate.record_id in {UUID(int=1), UUID(int=2)} for candidate in scoped.candidates)

    unavailable = maintenance_scan(
        recording,
        locator.namespace,
        PRINCIPAL,
        specification,
        MaintenanceQuery(scans, entity_name="plain"),
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
    assert [(candidate.scan_type, candidate.record_id) for candidate in unavailable.candidates] == [
        ("stale", UUID(int=20)),
        ("unbranched", UUID(int=20)),
    ]

    assert connection.info.transaction_status.name == "INTRANS"  # type: ignore[attr-defined]
    assert connection.execute("SHOW transaction_read_only").fetchone() == ("on",)  # type: ignore[attr-defined]
    assert recording.statements
    assert all(
        statement.lstrip().upper().startswith("SELECT") for statement in recording.statements
    )
    assert all(f'"{locator.namespace}".' in statement for statement in recording.statements)
    rendered_sql = "\n".join(recording.statements).lower()
    assert all(
        token not in rendered_sql for token in ("begin", "commit", "rollback", "search_path")
    )
    connection.rollback()  # type: ignore[attr-defined]
    connection.close()  # type: ignore[attr-defined]
