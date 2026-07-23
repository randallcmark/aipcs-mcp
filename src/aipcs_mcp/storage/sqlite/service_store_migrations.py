"""Immutable compiled SQLite service-store foundation descriptors through R2."""

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

R1 = MigrationDescriptor(
    1,
    R1_MIGRATION_ID,
    R1_DDL,
    R1_CHECKSUM,
    MappingProxyType(R1_EXPECTED_SQL),
    MappingProxyType(R1_TABLE_XINFO),
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
    MappingProxyType(R2_INDEX_LIST),
    MappingProxyType(R2_INDEX_XINFO),
)
MIGRATIONS = (R1, R2)
WAL_TARGET_REVISION = R2.revision

# The C5 migration engine now owns the complete R1 -> R2 physical-policy
# state machine, so all runtime inspection aliases describe the WAL target.
TARGET_REVISION = R2.revision
MIGRATION_ID = R2.migration_id
DDL = R2.ddl
CHECKSUM = R2.checksum
EXPECTED_SQL = R2_EXPECTED_SQL
TABLE_XINFO = R2_TABLE_XINFO
INDEX_LIST = R2_INDEX_LIST
INDEX_XINFO = R2_INDEX_XINFO
