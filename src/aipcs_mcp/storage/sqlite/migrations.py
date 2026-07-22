"""Finite compiled SQLite registry migration revision 1."""

from __future__ import annotations

import hashlib

TARGET_REVISION = 1
MIGRATION_ID = "registry-0001-initial"
DDL = (
    """CREATE TABLE "aipcs_registry_meta" (
    "singleton" INTEGER PRIMARY KEY NOT NULL CHECK ("singleton" = 1),
    "adapter_id" TEXT NOT NULL CHECK ("adapter_id" = 'aipcs.sqlite.registry'),
    "component" TEXT NOT NULL CHECK ("component" = 'registry'),
    "applied_revision" INTEGER NOT NULL CHECK ("applied_revision" >= 0),
    "dirty" INTEGER NOT NULL CHECK ("dirty" IN (0, 1))
) STRICT""",
    """CREATE TABLE "aipcs_registry_migration" (
    "component" TEXT NOT NULL CHECK ("component" = 'registry'),
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
) STRICT""",
    """CREATE TABLE "aipcs_registry_service" (
    "service_id" TEXT PRIMARY KEY NOT NULL CHECK (
        length("service_id") = 36 AND "service_id" = lower("service_id")
        AND substr("service_id", 9, 1) = '-' AND substr("service_id", 14, 1) = '-'
        AND substr("service_id", 19, 1) = '-' AND substr("service_id", 24, 1) = '-'
        AND replace("service_id", '-', '') NOT GLOB '*[^0-9a-f]*'
        AND length(replace("service_id", '-', '')) = 32
        AND replace("service_id", '-', '') != '00000000000000000000000000000000'
    ),
    "principal_id" TEXT NOT NULL CHECK (length("principal_id") BETWEEN 1 AND 128),
    "domain_name" TEXT NOT NULL CHECK (length("domain_name") BETWEEN 1 AND 63),
    "domain_class" TEXT NOT NULL CHECK (length("domain_class") BETWEEN 1 AND 64),
    "intent_description" TEXT NOT NULL CHECK (length("intent_description") BETWEEN 1 AND 1000),
    "created_at" TEXT NOT NULL CHECK (length("created_at") = 27),
    "updated_at" TEXT NOT NULL CHECK (length("updated_at") = 27),
    "last_activity_at" TEXT NOT NULL CHECK (length("last_activity_at") = 27),
    "manifest_json" TEXT,
    "schema_version" INTEGER CHECK ("schema_version" IS NULL OR "schema_version" >= 1),
    "design_state" TEXT NOT NULL CHECK ("design_state" IN ('seeded', 'materialised')),
    "operational_status" TEXT NOT NULL CHECK (
        "operational_status" IN ('active', 'suspended', 'archived')
    ),
    CHECK (
        ("manifest_json" IS NULL AND "schema_version" IS NULL)
        OR ("manifest_json" IS NOT NULL AND "schema_version" IS NOT NULL)
    ),
    UNIQUE ("service_id", "principal_id"),
    UNIQUE ("principal_id", "domain_name")
) STRICT""",
    """CREATE TABLE "aipcs_registry_mutation" (
    "principal_id" TEXT NOT NULL CHECK (length("principal_id") BETWEEN 1 AND 128),
    "idempotency_key" TEXT NOT NULL CHECK (length("idempotency_key") BETWEEN 1 AND 128),
    "fingerprint" TEXT NOT NULL CHECK (
        length("fingerprint") = 64 AND "fingerprint" = lower("fingerprint")
        AND "fingerprint" NOT GLOB '*[^0-9a-f]*'
    ),
    "service_id" TEXT NOT NULL,
    "result_json" TEXT NOT NULL,
    PRIMARY KEY ("principal_id", "idempotency_key"),
    FOREIGN KEY ("service_id", "principal_id")
        REFERENCES "aipcs_registry_service" ("service_id", "principal_id")
        ON UPDATE RESTRICT ON DELETE RESTRICT
) STRICT""",
    """CREATE TABLE "aipcs_registry_audit" (
    "audit_id" INTEGER PRIMARY KEY,
    "action" TEXT NOT NULL CHECK ("action" IN ('seed', 'design')),
    "outcome" TEXT NOT NULL CHECK ("outcome" IN ('created', 'duplicate', 'accepted')),
    "service_id" TEXT NOT NULL,
    "principal_id" TEXT NOT NULL CHECK (length("principal_id") BETWEEN 1 AND 128),
    "created_via" TEXT NOT NULL CHECK (length("created_via") BETWEEN 1 AND 64),
    "at" TEXT NOT NULL CHECK (length("at") = 27),
    CHECK (
        ("action" = 'seed' AND "outcome" IN ('created', 'duplicate'))
        OR ("action" = 'design' AND "outcome" = 'accepted')
    ),
    FOREIGN KEY ("service_id", "principal_id")
        REFERENCES "aipcs_registry_service" ("service_id", "principal_id")
        ON UPDATE RESTRICT ON DELETE RESTRICT
) STRICT""",
    """CREATE INDEX "aipcs_registry_service_list"
    ON "aipcs_registry_service" ("principal_id", "created_at" ASC, "service_id" ASC)""",
)
CHECKSUM = hashlib.sha256("\n".join(DDL).encode()).hexdigest()
EXPECTED_SQL = dict(
    zip(
        (
            "aipcs_registry_meta",
            "aipcs_registry_migration",
            "aipcs_registry_service",
            "aipcs_registry_mutation",
            "aipcs_registry_audit",
            "aipcs_registry_service_list",
        ),
        DDL,
        strict=True,
    )
)

TABLE_XINFO = {
    "aipcs_registry_meta": (
        (0, "singleton", "INTEGER", 1, None, 1, 0),
        (1, "adapter_id", "TEXT", 1, None, 0, 0),
        (2, "component", "TEXT", 1, None, 0, 0),
        (3, "applied_revision", "INTEGER", 1, None, 0, 0),
        (4, "dirty", "INTEGER", 1, None, 0, 0),
    ),
    "aipcs_registry_migration": (
        (0, "component", "TEXT", 1, None, 1, 0),
        (1, "revision", "INTEGER", 1, None, 2, 0),
        (2, "migration_id", "TEXT", 1, None, 0, 0),
        (3, "checksum", "TEXT", 1, None, 0, 0),
        (4, "applied_at", "TEXT", 1, None, 0, 0),
    ),
    "aipcs_registry_service": (
        (0, "service_id", "TEXT", 1, None, 1, 0),
        (1, "principal_id", "TEXT", 1, None, 0, 0),
        (2, "domain_name", "TEXT", 1, None, 0, 0),
        (3, "domain_class", "TEXT", 1, None, 0, 0),
        (4, "intent_description", "TEXT", 1, None, 0, 0),
        (5, "created_at", "TEXT", 1, None, 0, 0),
        (6, "updated_at", "TEXT", 1, None, 0, 0),
        (7, "last_activity_at", "TEXT", 1, None, 0, 0),
        (8, "manifest_json", "TEXT", 0, None, 0, 0),
        (9, "schema_version", "INTEGER", 0, None, 0, 0),
        (10, "design_state", "TEXT", 1, None, 0, 0),
        (11, "operational_status", "TEXT", 1, None, 0, 0),
    ),
    "aipcs_registry_mutation": (
        (0, "principal_id", "TEXT", 1, None, 1, 0),
        (1, "idempotency_key", "TEXT", 1, None, 2, 0),
        (2, "fingerprint", "TEXT", 1, None, 0, 0),
        (3, "service_id", "TEXT", 1, None, 0, 0),
        (4, "result_json", "TEXT", 1, None, 0, 0),
    ),
    "aipcs_registry_audit": (
        (0, "audit_id", "INTEGER", 0, None, 1, 0),
        (1, "action", "TEXT", 1, None, 0, 0),
        (2, "outcome", "TEXT", 1, None, 0, 0),
        (3, "service_id", "TEXT", 1, None, 0, 0),
        (4, "principal_id", "TEXT", 1, None, 0, 0),
        (5, "created_via", "TEXT", 1, None, 0, 0),
        (6, "at", "TEXT", 1, None, 0, 0),
    ),
}
FOREIGN_KEYS = {
    "aipcs_registry_meta": (),
    "aipcs_registry_migration": (),
    "aipcs_registry_service": (),
    "aipcs_registry_mutation": (
        (
            0,
            0,
            "aipcs_registry_service",
            "service_id",
            "service_id",
            "RESTRICT",
            "RESTRICT",
            "NONE",
        ),
        (
            0,
            1,
            "aipcs_registry_service",
            "principal_id",
            "principal_id",
            "RESTRICT",
            "RESTRICT",
            "NONE",
        ),
    ),
    "aipcs_registry_audit": (
        (
            0,
            0,
            "aipcs_registry_service",
            "service_id",
            "service_id",
            "RESTRICT",
            "RESTRICT",
            "NONE",
        ),
        (
            0,
            1,
            "aipcs_registry_service",
            "principal_id",
            "principal_id",
            "RESTRICT",
            "RESTRICT",
            "NONE",
        ),
    ),
}
INDEX_LIST = {
    "aipcs_registry_meta": (),
    "aipcs_registry_migration": (
        ("sqlite_autoindex_aipcs_registry_migration_1", 1, "pk", 0),
        ("sqlite_autoindex_aipcs_registry_migration_2", 1, "u", 0),
    ),
    "aipcs_registry_service": (
        ("aipcs_registry_service_list", 0, "c", 0),
        ("sqlite_autoindex_aipcs_registry_service_1", 1, "pk", 0),
        ("sqlite_autoindex_aipcs_registry_service_2", 1, "u", 0),
        ("sqlite_autoindex_aipcs_registry_service_3", 1, "u", 0),
    ),
    "aipcs_registry_mutation": (("sqlite_autoindex_aipcs_registry_mutation_1", 1, "pk", 0),),
    "aipcs_registry_audit": (),
}
INDEX_XINFO = {
    "sqlite_autoindex_aipcs_registry_migration_2": (
        (0, 0, "component", 0, "BINARY", 1),
        (1, 2, "migration_id", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex_aipcs_registry_migration_1": (
        (0, 0, "component", 0, "BINARY", 1),
        (1, 1, "revision", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
    "aipcs_registry_service_list": (
        (0, 1, "principal_id", 0, "BINARY", 1),
        (1, 5, "created_at", 0, "BINARY", 1),
        (2, 0, "service_id", 0, "BINARY", 1),
        (3, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex_aipcs_registry_service_3": (
        (0, 1, "principal_id", 0, "BINARY", 1),
        (1, 2, "domain_name", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex_aipcs_registry_service_2": (
        (0, 0, "service_id", 0, "BINARY", 1),
        (1, 1, "principal_id", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex_aipcs_registry_service_1": (
        (0, 0, "service_id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex_aipcs_registry_mutation_1": (
        (0, 0, "principal_id", 0, "BINARY", 1),
        (1, 1, "idempotency_key", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
}
