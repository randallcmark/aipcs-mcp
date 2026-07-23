"""Immutable compiled SQLite service-store foundation descriptors through R3."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import MappingProxyType

from .wal_policy import WALPolicyDescriptor, policy_checksum, policy_table_ddl

TARGET_REVISION = 1
MIGRATION_ID = "service-store-0001-foundation"
META = "__aipcs_service_store_meta"
MIGRATION = "__aipcs_service_store_migration"
RESERVED_PREFIX = "__aipcs_"
DDL = (
    f'''CREATE TABLE "{META}" (
    "singleton" INTEGER PRIMARY KEY NOT NULL CHECK ("singleton" = 1),
    "adapter_id" TEXT NOT NULL CHECK ("adapter_id" = 'aipcs.sqlite.service_store'),
    "component" TEXT NOT NULL CHECK ("component" = 'service_store'),
    "namespace" TEXT NOT NULL CHECK (
        length("namespace") = 36 AND substr("namespace", 1, 4) = 'svc_'
        AND "namespace" = lower("namespace")
        AND substr("namespace", 5) NOT GLOB '*[^0-9a-f]*'
    ),
    "applied_revision" INTEGER NOT NULL CHECK ("applied_revision" >= 0),
    "dirty" INTEGER NOT NULL CHECK ("dirty" IN (0, 1))
) STRICT''',
    f'''CREATE TABLE "{MIGRATION}" (
    "component" TEXT NOT NULL CHECK ("component" = 'service_store'),
    "revision" INTEGER NOT NULL CHECK ("revision" > 0),
    "migration_id" TEXT NOT NULL CHECK (length("migration_id") BETWEEN 1 AND 96),
    "checksum" TEXT NOT NULL CHECK (
        length("checksum") = 64 AND "checksum" = lower("checksum")
        AND "checksum" NOT GLOB '*[^0-9a-f]*'
    ),
    "applied_at" TEXT NOT NULL CHECK (
        length("applied_at") = 27 AND substr("applied_at", 11, 1) = 'T'
        AND substr("applied_at", 20, 1) = '.' AND substr("applied_at", 27, 1) = 'Z'
    ),
    PRIMARY KEY ("component", "revision"),
    UNIQUE ("component", "migration_id")
) STRICT''',
)
CHECKSUM = hashlib.sha256("\n".join(DDL).encode()).hexdigest()
EXPECTED_SQL = {META: DDL[0], MIGRATION: DDL[1]}
TABLE_XINFO = {
    META: (
        (0, "singleton", "INTEGER", 1, None, 1, 0),
        (1, "adapter_id", "TEXT", 1, None, 0, 0),
        (2, "component", "TEXT", 1, None, 0, 0),
        (3, "namespace", "TEXT", 1, None, 0, 0),
        (4, "applied_revision", "INTEGER", 1, None, 0, 0),
        (5, "dirty", "INTEGER", 1, None, 0, 0),
    ),
    MIGRATION: (
        (0, "component", "TEXT", 1, None, 1, 0),
        (1, "revision", "INTEGER", 1, None, 2, 0),
        (2, "migration_id", "TEXT", 1, None, 0, 0),
        (3, "checksum", "TEXT", 1, None, 0, 0),
        (4, "applied_at", "TEXT", 1, None, 0, 0),
    ),
}
INDEX_LIST = {
    META: (),
    MIGRATION: (
        ("sqlite_autoindex___aipcs_service_store_migration_1", 1, "pk", 0),
        ("sqlite_autoindex___aipcs_service_store_migration_2", 1, "u", 0),
    ),
}
INDEX_XINFO = {
    "sqlite_autoindex___aipcs_service_store_migration_1": (
        (0, 0, "component", 0, "BINARY", 1),
        (1, 1, "revision", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex___aipcs_service_store_migration_2": (
        (0, 0, "component", 0, "BINARY", 1),
        (1, 2, "migration_id", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
}


@dataclass(frozen=True)
class MigrationDescriptor:
    revision: int
    migration_id: str
    ddl: tuple[str, ...]
    checksum: str
    expected_sql: MappingProxyType
    table_xinfo: MappingProxyType
    foreign_keys: MappingProxyType
    index_list: MappingProxyType
    index_xinfo: MappingProxyType


# Preserve revision 1 as immutable historical evidence before constructing R2.
R1_MIGRATION_ID = MIGRATION_ID
R1_DDL = DDL
R1_CHECKSUM = CHECKSUM
R1_EXPECTED_SQL = EXPECTED_SQL
R1_TABLE_XINFO = TABLE_XINFO
R1_INDEX_LIST = INDEX_LIST
R1_INDEX_XINFO = INDEX_XINFO
R1_FOREIGN_KEYS = {META: (), MIGRATION: ()}

R2_MIGRATION_ID = "service-store-0002-wal-policy"
R2_POLICY_TABLE = "__aipcs_service_store_policy"
R2_POLICY_DDL = policy_table_ddl(R2_POLICY_TABLE)
R2_CHECKSUM = policy_checksum(R2_POLICY_DDL)
R2_POLICY = WALPolicyDescriptor(R2_POLICY_TABLE, R2_POLICY_DDL, R2_CHECKSUM)
R2_DDL = (R2_POLICY_DDL,)
R2_EXPECTED_SQL = {**R1_EXPECTED_SQL, R2_POLICY_TABLE: R2_POLICY_DDL}
R2_TABLE_XINFO = {
    **R1_TABLE_XINFO,
    R2_POLICY_TABLE: (
        (0, "singleton", "INTEGER", 1, None, 1, 0),
        (1, "policy_id", "TEXT", 1, None, 0, 0),
        (2, "policy_checksum", "TEXT", 1, None, 0, 0),
        (3, "phase", "TEXT", 1, None, 0, 0),
    ),
}
R2_INDEX_LIST = {**R1_INDEX_LIST, R2_POLICY_TABLE: ()}
R2_INDEX_XINFO = R1_INDEX_XINFO
R2_FOREIGN_KEYS = {**R1_FOREIGN_KEYS, R2_POLICY_TABLE: ()}

# R3 adds only service-local data-operation evidence and topology metadata.
# It deliberately does not add manifest, lifecycle, or schema-transition state.
R3_MIGRATION_ID = "service-store-0003-record-topology"
R3_MUTATION_TABLE = "__aipcs_record_mutation"
R3_HISTORY_TABLE = "__aipcs_record_history"
R3_BRANCH_TABLE = "__aipcs_memory_branch"
R3_RECORD_BRANCH_TABLE = "__aipcs_record_branch"
R3_DDL = (
    f'''CREATE TABLE "{R3_MUTATION_TABLE}" (
    "principal_id" TEXT NOT NULL,
    "idempotency_key" TEXT NOT NULL CHECK (length("idempotency_key") BETWEEN 1 AND 128),
    "operation_kind" TEXT NOT NULL CHECK (length("operation_kind") BETWEEN 1 AND 64),
    "fingerprint" TEXT NOT NULL CHECK (
        length("fingerprint") = 64 AND "fingerprint" = lower("fingerprint")
        AND "fingerprint" NOT GLOB '*[^0-9a-f]*'
    ),
    "result_json" TEXT NOT NULL CHECK (
        length(CAST("result_json" AS BLOB)) <= 1048576
    ),
    "completed_at" TEXT NOT NULL CHECK (
        length("completed_at") = 27 AND substr("completed_at", 11, 1) = 'T'
        AND substr("completed_at", 20, 1) = '.' AND substr("completed_at", 27, 1) = 'Z'
    ),
    PRIMARY KEY ("principal_id", "idempotency_key")
) STRICT''',
    f'''CREATE TABLE "{R3_HISTORY_TABLE}" (
    "event_sequence" INTEGER PRIMARY KEY NOT NULL,
    "principal_id" TEXT NOT NULL,
    "entity_name" TEXT NOT NULL,
    "record_id" TEXT NOT NULL CHECK (
        length("record_id") = 36 AND "record_id" = lower("record_id")
        AND substr("record_id", 9, 1) = '-' AND substr("record_id", 14, 1) = '-'
        AND substr("record_id", 19, 1) = '-' AND substr("record_id", 24, 1) = '-'
        AND replace("record_id", '-', '') NOT GLOB '*[^0-9a-f]*'
    ),
    "record_version" INTEGER NOT NULL CHECK ("record_version" > 0),
    "event_kind" TEXT NOT NULL CHECK (
        "event_kind" IN (
            'create', 'update', 'delete', 'primary_assign', 'primary_move',
            'primary_unassign', 'related_assign', 'related_unassign'
        )
    ),
    "before_json" TEXT CHECK (
        "before_json" IS NULL OR length(CAST("before_json" AS BLOB)) <= 65536
    ),
    "after_json" TEXT CHECK (
        "after_json" IS NULL OR length(CAST("after_json" AS BLOB)) <= 65536
    ),
    "occurred_at" TEXT NOT NULL CHECK (
        length("occurred_at") = 27 AND substr("occurred_at", 11, 1) = 'T'
        AND substr("occurred_at", 20, 1) = '.' AND substr("occurred_at", 27, 1) = 'Z'
    ),
    UNIQUE ("principal_id", "entity_name", "record_id", "record_version")
) STRICT''',
    f'''CREATE TABLE "{R3_BRANCH_TABLE}" (
    "id" TEXT PRIMARY KEY NOT NULL CHECK (
        length("id") = 36 AND "id" = lower("id")
        AND substr("id", 9, 1) = '-' AND substr("id", 14, 1) = '-'
        AND substr("id", 19, 1) = '-' AND substr("id", 24, 1) = '-'
        AND replace("id", '-', '') NOT GLOB '*[^0-9a-f]*'
    ),
    "principal_id" TEXT NOT NULL,
    "slug" TEXT NOT NULL,
    "title" TEXT NOT NULL,
    "intent" TEXT NOT NULL,
    "branch_type" TEXT,
    "parent_branch_id" TEXT REFERENCES "{R3_BRANCH_TABLE}" ("id") ON DELETE RESTRICT,
    "status" TEXT NOT NULL CHECK ("status" IN ('active', 'archived', 'superseded')),
    "retrieval_summary" TEXT,
    "branch_revision" INTEGER NOT NULL CHECK ("branch_revision" > 0),
    "created_at" TEXT NOT NULL CHECK (
        length("created_at") = 27 AND substr("created_at", 11, 1) = 'T'
        AND substr("created_at", 20, 1) = '.' AND substr("created_at", 27, 1) = 'Z'
    ),
    "updated_at" TEXT NOT NULL CHECK (
        length("updated_at") = 27 AND substr("updated_at", 11, 1) = 'T'
        AND substr("updated_at", 20, 1) = '.' AND substr("updated_at", 27, 1) = 'Z'
    ),
    UNIQUE ("principal_id", "slug")
) STRICT''',
    f'''CREATE TABLE "{R3_RECORD_BRANCH_TABLE}" (
    "principal_id" TEXT NOT NULL,
    "entity_name" TEXT NOT NULL,
    "record_id" TEXT NOT NULL CHECK (
        length("record_id") = 36 AND "record_id" = lower("record_id")
        AND substr("record_id", 9, 1) = '-' AND substr("record_id", 14, 1) = '-'
        AND substr("record_id", 19, 1) = '-' AND substr("record_id", 24, 1) = '-'
        AND replace("record_id", '-', '') NOT GLOB '*[^0-9a-f]*'
    ),
    "branch_id" TEXT NOT NULL REFERENCES "{R3_BRANCH_TABLE}" ("id") ON DELETE RESTRICT,
    "role" TEXT NOT NULL CHECK ("role" IN ('primary', 'related')),
    "assigned_at" TEXT NOT NULL CHECK (
        length("assigned_at") = 27 AND substr("assigned_at", 11, 1) = 'T'
        AND substr("assigned_at", 20, 1) = '.' AND substr("assigned_at", 27, 1) = 'Z'
    ),
    PRIMARY KEY ("principal_id", "entity_name", "record_id", "branch_id")
) STRICT''',
    f'''CREATE INDEX "__aipcs_record_history_entity_sequence"
ON "{R3_HISTORY_TABLE}" ("principal_id", "entity_name", "event_sequence")''',
    f'''CREATE INDEX "__aipcs_record_history_record_version"
ON "{R3_HISTORY_TABLE}" ("principal_id", "entity_name", "record_id", "record_version")''',
    f'''CREATE INDEX "__aipcs_memory_branch_principal_status_updated"
ON "{R3_BRANCH_TABLE}" ("principal_id", "status", "updated_at")''',
    f'''CREATE UNIQUE INDEX "__aipcs_record_branch_primary"
ON "{R3_RECORD_BRANCH_TABLE}" ("principal_id", "entity_name", "record_id")
WHERE "role" = 'primary' '''.rstrip(),
    f'''CREATE INDEX "__aipcs_record_branch_branch_lookup"
ON "{R3_RECORD_BRANCH_TABLE}" ("principal_id", "branch_id", "entity_name", "role", "record_id")''',
)
R3_CHECKSUM = hashlib.sha256("\n".join(R3_DDL).encode()).hexdigest()
R3_EXPECTED_SQL = {
    **R2_EXPECTED_SQL,
    R3_MUTATION_TABLE: R3_DDL[0],
    R3_HISTORY_TABLE: R3_DDL[1],
    R3_BRANCH_TABLE: R3_DDL[2],
    R3_RECORD_BRANCH_TABLE: R3_DDL[3],
    "__aipcs_record_history_entity_sequence": R3_DDL[4],
    "__aipcs_record_history_record_version": R3_DDL[5],
    "__aipcs_memory_branch_principal_status_updated": R3_DDL[6],
    "__aipcs_record_branch_primary": R3_DDL[7],
    "__aipcs_record_branch_branch_lookup": R3_DDL[8],
}
R3_TABLE_XINFO = {
    **R2_TABLE_XINFO,
    R3_MUTATION_TABLE: (
        (0, "principal_id", "TEXT", 1, None, 1, 0),
        (1, "idempotency_key", "TEXT", 1, None, 2, 0),
        (2, "operation_kind", "TEXT", 1, None, 0, 0),
        (3, "fingerprint", "TEXT", 1, None, 0, 0),
        (4, "result_json", "TEXT", 1, None, 0, 0),
        (5, "completed_at", "TEXT", 1, None, 0, 0),
    ),
    R3_HISTORY_TABLE: (
        (0, "event_sequence", "INTEGER", 1, None, 1, 0),
        (1, "principal_id", "TEXT", 1, None, 0, 0),
        (2, "entity_name", "TEXT", 1, None, 0, 0),
        (3, "record_id", "TEXT", 1, None, 0, 0),
        (4, "record_version", "INTEGER", 1, None, 0, 0),
        (5, "event_kind", "TEXT", 1, None, 0, 0),
        (6, "before_json", "TEXT", 0, None, 0, 0),
        (7, "after_json", "TEXT", 0, None, 0, 0),
        (8, "occurred_at", "TEXT", 1, None, 0, 0),
    ),
    R3_BRANCH_TABLE: (
        (0, "id", "TEXT", 1, None, 1, 0),
        (1, "principal_id", "TEXT", 1, None, 0, 0),
        (2, "slug", "TEXT", 1, None, 0, 0),
        (3, "title", "TEXT", 1, None, 0, 0),
        (4, "intent", "TEXT", 1, None, 0, 0),
        (5, "branch_type", "TEXT", 0, None, 0, 0),
        (6, "parent_branch_id", "TEXT", 0, None, 0, 0),
        (7, "status", "TEXT", 1, None, 0, 0),
        (8, "retrieval_summary", "TEXT", 0, None, 0, 0),
        (9, "branch_revision", "INTEGER", 1, None, 0, 0),
        (10, "created_at", "TEXT", 1, None, 0, 0),
        (11, "updated_at", "TEXT", 1, None, 0, 0),
    ),
    R3_RECORD_BRANCH_TABLE: (
        (0, "principal_id", "TEXT", 1, None, 1, 0),
        (1, "entity_name", "TEXT", 1, None, 2, 0),
        (2, "record_id", "TEXT", 1, None, 3, 0),
        (3, "branch_id", "TEXT", 1, None, 4, 0),
        (4, "role", "TEXT", 1, None, 0, 0),
        (5, "assigned_at", "TEXT", 1, None, 0, 0),
    ),
}
R3_FOREIGN_KEYS = {
    **R2_FOREIGN_KEYS,
    R3_MUTATION_TABLE: (),
    R3_HISTORY_TABLE: (),
    R3_BRANCH_TABLE: (
        (0, 0, R3_BRANCH_TABLE, "parent_branch_id", "id", "NO ACTION", "RESTRICT", "NONE"),
    ),
    R3_RECORD_BRANCH_TABLE: (
        (0, 0, R3_BRANCH_TABLE, "branch_id", "id", "NO ACTION", "RESTRICT", "NONE"),
    ),
}
R3_INDEX_LIST = {
    **R2_INDEX_LIST,
    R3_MUTATION_TABLE: (("sqlite_autoindex___aipcs_record_mutation_1", 1, "pk", 0),),
    R3_HISTORY_TABLE: (
        ("__aipcs_record_history_entity_sequence", 0, "c", 0),
        ("__aipcs_record_history_record_version", 0, "c", 0),
        ("sqlite_autoindex___aipcs_record_history_1", 1, "u", 0),
    ),
    R3_BRANCH_TABLE: (
        ("__aipcs_memory_branch_principal_status_updated", 0, "c", 0),
        ("sqlite_autoindex___aipcs_memory_branch_1", 1, "pk", 0),
        ("sqlite_autoindex___aipcs_memory_branch_2", 1, "u", 0),
    ),
    R3_RECORD_BRANCH_TABLE: (
        ("__aipcs_record_branch_branch_lookup", 0, "c", 0),
        ("__aipcs_record_branch_primary", 1, "c", 1),
        ("sqlite_autoindex___aipcs_record_branch_1", 1, "pk", 0),
    ),
}
R3_INDEX_XINFO = {
    **R2_INDEX_XINFO,
    "sqlite_autoindex___aipcs_record_mutation_1": (
        (0, 0, "principal_id", 0, "BINARY", 1),
        (1, 1, "idempotency_key", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex___aipcs_record_history_1": (
        (0, 1, "principal_id", 0, "BINARY", 1),
        (1, 2, "entity_name", 0, "BINARY", 1),
        (2, 3, "record_id", 0, "BINARY", 1),
        (3, 4, "record_version", 0, "BINARY", 1),
        (4, -1, None, 0, "BINARY", 0),
    ),
    "__aipcs_record_history_entity_sequence": (
        (0, 1, "principal_id", 0, "BINARY", 1),
        (1, 2, "entity_name", 0, "BINARY", 1),
        (2, 0, "event_sequence", 0, "BINARY", 1),
        (3, -1, None, 0, "BINARY", 0),
    ),
    "__aipcs_record_history_record_version": (
        (0, 1, "principal_id", 0, "BINARY", 1),
        (1, 2, "entity_name", 0, "BINARY", 1),
        (2, 3, "record_id", 0, "BINARY", 1),
        (3, 4, "record_version", 0, "BINARY", 1),
        (4, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex___aipcs_memory_branch_1": (
        (0, 0, "id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex___aipcs_memory_branch_2": (
        (0, 1, "principal_id", 0, "BINARY", 1),
        (1, 2, "slug", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
    "__aipcs_memory_branch_principal_status_updated": (
        (0, 1, "principal_id", 0, "BINARY", 1),
        (1, 7, "status", 0, "BINARY", 1),
        (2, 11, "updated_at", 0, "BINARY", 1),
        (3, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex___aipcs_record_branch_1": (
        (0, 0, "principal_id", 0, "BINARY", 1),
        (1, 1, "entity_name", 0, "BINARY", 1),
        (2, 2, "record_id", 0, "BINARY", 1),
        (3, 3, "branch_id", 0, "BINARY", 1),
        (4, -1, None, 0, "BINARY", 0),
    ),
    "__aipcs_record_branch_primary": (
        (0, 0, "principal_id", 0, "BINARY", 1),
        (1, 1, "entity_name", 0, "BINARY", 1),
        (2, 2, "record_id", 0, "BINARY", 1),
        (3, -1, None, 0, "BINARY", 0),
    ),
    "__aipcs_record_branch_branch_lookup": (
        (0, 0, "principal_id", 0, "BINARY", 1),
        (1, 3, "branch_id", 0, "BINARY", 1),
        (2, 1, "entity_name", 0, "BINARY", 1),
        (3, 4, "role", 0, "BINARY", 1),
        (4, 2, "record_id", 0, "BINARY", 1),
        (5, -1, None, 0, "BINARY", 0),
    ),
}

R1 = MigrationDescriptor(
    1,
    R1_MIGRATION_ID,
    R1_DDL,
    R1_CHECKSUM,
    MappingProxyType(R1_EXPECTED_SQL),
    MappingProxyType(R1_TABLE_XINFO),
    MappingProxyType(R1_FOREIGN_KEYS),
    MappingProxyType(R1_INDEX_LIST),
    MappingProxyType(R1_INDEX_XINFO),
)
R2 = MigrationDescriptor(
    2,
    R2_MIGRATION_ID,
    R2_DDL,
    R2_CHECKSUM,
    MappingProxyType(R2_EXPECTED_SQL),
    MappingProxyType(R2_TABLE_XINFO),
    MappingProxyType(R2_FOREIGN_KEYS),
    MappingProxyType(R2_INDEX_LIST),
    MappingProxyType(R2_INDEX_XINFO),
)
R3 = MigrationDescriptor(
    3,
    R3_MIGRATION_ID,
    R3_DDL,
    R3_CHECKSUM,
    MappingProxyType(R3_EXPECTED_SQL),
    MappingProxyType(R3_TABLE_XINFO),
    MappingProxyType(R3_FOREIGN_KEYS),
    MappingProxyType(R3_INDEX_LIST),
    MappingProxyType(R3_INDEX_XINFO),
)
MIGRATIONS = (R1, R2, R3)
WAL_TARGET_REVISION = R2.revision

# The migration engine preserves R2 as the WAL-policy predecessor while
# runtime inspection aliases describe the complete R3 service-store target.
TARGET_REVISION = R3.revision
MIGRATION_ID = R3.migration_id
DDL = R3.ddl
CHECKSUM = R3.checksum
EXPECTED_SQL = R3_EXPECTED_SQL
TABLE_XINFO = R3_TABLE_XINFO
INDEX_LIST = R3_INDEX_LIST
INDEX_XINFO = R3_INDEX_XINFO
INTERNAL_INDEX_XINFO = {
    name: signature for name, signature in INDEX_XINFO.items() if name.startswith("sqlite_")
}
