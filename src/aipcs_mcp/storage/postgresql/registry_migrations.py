"""Immutable PostgreSQL registry R1 descriptor.

PostgreSQL R1 is the behavioural counterpart of the current SQLite registry.
It intentionally starts at one native PostgreSQL revision instead of replaying
SQLite's private historical migrations.
"""

from __future__ import annotations

import hashlib
import re

SCHEMA = "aipcs_registry"
TARGET_REVISION = 1
MIGRATION_ID = "postgresql-registry-0001-current-foundation"
ADAPTER_ID = "aipcs.postgresql.registry"

DDL = (
    'CREATE SCHEMA "aipcs_registry" AUTHORIZATION CURRENT_USER',
    'REVOKE ALL PRIVILEGES ON SCHEMA "aipcs_registry" FROM PUBLIC',
    """CREATE TABLE "aipcs_registry"."aipcs_registry_meta" (
    "singleton" smallint NOT NULL,
    "adapter_id" text COLLATE "C" NOT NULL,
    "component" text COLLATE "C" NOT NULL,
    "applied_revision" bigint NOT NULL,
    "dirty" boolean NOT NULL,
    CONSTRAINT "aipcs_registry_meta_pkey" PRIMARY KEY ("singleton"),
    CONSTRAINT "aipcs_registry_meta_singleton_check" CHECK ("singleton" = 1),
    CONSTRAINT "aipcs_registry_meta_adapter_check"
        CHECK ("adapter_id" = 'aipcs.postgresql.registry'),
    CONSTRAINT "aipcs_registry_meta_component_check" CHECK ("component" = 'registry'),
    CONSTRAINT "aipcs_registry_meta_revision_check" CHECK ("applied_revision" >= 0)
)""",
    """CREATE TABLE "aipcs_registry"."aipcs_registry_migration" (
    "component" text COLLATE "C" NOT NULL,
    "revision" bigint NOT NULL,
    "migration_id" text COLLATE "C" NOT NULL,
    "checksum" text COLLATE "C" NOT NULL,
    "applied_at" timestamp(6) with time zone NOT NULL,
    CONSTRAINT "aipcs_registry_migration_pkey" PRIMARY KEY ("component", "revision"),
    CONSTRAINT "aipcs_registry_migration_id_key" UNIQUE ("component", "migration_id"),
    CONSTRAINT "aipcs_registry_migration_component_check" CHECK ("component" = 'registry'),
    CONSTRAINT "aipcs_registry_migration_revision_check" CHECK ("revision" > 0),
    CONSTRAINT "aipcs_registry_migration_id_check"
        CHECK (char_length("migration_id") BETWEEN 1 AND 96),
    CONSTRAINT "aipcs_registry_migration_checksum_check"
        CHECK ("checksum" ~ '^[0-9a-f]{64}$')
)""",
    """CREATE TABLE "aipcs_registry"."aipcs_registry_service" (
    "service_id" uuid NOT NULL,
    "principal_id" text COLLATE "C" NOT NULL,
    "domain_name" text COLLATE "C" NOT NULL,
    "domain_class" text COLLATE "C" NOT NULL,
    "intent_description" text COLLATE "C" NOT NULL,
    "created_at" timestamp(6) with time zone NOT NULL,
    "updated_at" timestamp(6) with time zone NOT NULL,
    "last_activity_at" timestamp(6) with time zone NOT NULL,
    "manifest_json" jsonb,
    "schema_version" smallint,
    "design_state" text COLLATE "C" NOT NULL,
    "operational_status" text COLLATE "C" NOT NULL,
    "service_revision" bigint NOT NULL,
    "materialised_at" timestamp(6) with time zone,
    "storage_backend" text COLLATE "C",
    "storage_namespace" text COLLATE "C",
    CONSTRAINT "aipcs_registry_service_pkey" PRIMARY KEY ("service_id"),
    CONSTRAINT "aipcs_registry_service_identity_key"
        UNIQUE ("service_id", "principal_id"),
    CONSTRAINT "aipcs_registry_service_domain_key"
        UNIQUE ("principal_id", "domain_name"),
    CONSTRAINT "aipcs_registry_service_id_check"
        CHECK ("service_id" <> '00000000-0000-0000-0000-000000000000'::uuid),
    CONSTRAINT "aipcs_registry_service_principal_check" CHECK (
        char_length("principal_id") BETWEEN 1 AND 128
        AND "principal_id" = btrim("principal_id")
    ),
    CONSTRAINT "aipcs_registry_service_domain_check" CHECK (
        char_length("domain_name") BETWEEN 1 AND 63
        AND "domain_name" ~ '^[a-z][a-z0-9_]{0,62}$'
    ),
    CONSTRAINT "aipcs_registry_service_class_check" CHECK (
        char_length("domain_class") BETWEEN 1 AND 64
        AND "domain_class" = btrim("domain_class")
    ),
    CONSTRAINT "aipcs_registry_service_intent_check" CHECK (
        char_length("intent_description") BETWEEN 1 AND 1000
        AND "intent_description" = btrim("intent_description")
    ),
    CONSTRAINT "aipcs_registry_service_schema_check"
        CHECK ("schema_version" IS NULL OR "schema_version" BETWEEN 1 AND 65),
    CONSTRAINT "aipcs_registry_service_design_check"
        CHECK ("design_state" IN ('seeded', 'materialised')),
    CONSTRAINT "aipcs_registry_service_status_check"
        CHECK ("operational_status" IN ('active', 'suspended', 'archived')),
    CONSTRAINT "aipcs_registry_service_revision_check"
        CHECK ("service_revision" BETWEEN 1 AND 9223372036854775807),
    CONSTRAINT "aipcs_registry_service_manifest_pair_check" CHECK (
        ("manifest_json" IS NULL AND "schema_version" IS NULL)
        OR ("manifest_json" IS NOT NULL AND "schema_version" IS NOT NULL)
    ),
    CONSTRAINT "aipcs_registry_service_materialisation_check" CHECK (
        ("design_state" = 'seeded' AND "materialised_at" IS NULL
            AND "storage_backend" IS NULL AND "storage_namespace" IS NULL)
        OR ("design_state" = 'materialised' AND "manifest_json" IS NOT NULL
            AND "materialised_at" IS NOT NULL
            AND "storage_backend" IN ('sqlite', 'postgresql')
            AND "storage_namespace" ~ '^svc_[0-9a-f]{32}$'
            AND "storage_namespace" <> 'svc_00000000000000000000000000000000'
            AND substr("storage_namespace", 5) = replace("service_id"::text, '-', ''))
    )
)""",
    """CREATE TABLE "aipcs_registry"."aipcs_registry_mutation" (
    "principal_id" text COLLATE "C" NOT NULL,
    "idempotency_key" text COLLATE "C" NOT NULL,
    "fingerprint" text COLLATE "C" NOT NULL,
    "service_id" uuid NOT NULL,
    "operation_kind" text COLLATE "C" NOT NULL,
    "phase" text COLLATE "C" NOT NULL,
    "created_via" text COLLATE "C",
    "expected_service_revision" bigint,
    "expected_schema_version" smallint,
    "target_manifest_json" jsonb,
    "result_json" jsonb,
    "recovery_category" text COLLATE "C",
    CONSTRAINT "aipcs_registry_mutation_pkey"
        PRIMARY KEY ("principal_id", "idempotency_key"),
    CONSTRAINT "aipcs_registry_mutation_service_fkey"
        FOREIGN KEY ("service_id", "principal_id")
        REFERENCES "aipcs_registry"."aipcs_registry_service"
            ("service_id", "principal_id")
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT "aipcs_registry_mutation_principal_check" CHECK (
        char_length("principal_id") BETWEEN 1 AND 128
        AND "principal_id" = btrim("principal_id")
    ),
    CONSTRAINT "aipcs_registry_mutation_key_check" CHECK (
        char_length("idempotency_key") BETWEEN 1 AND 128
        AND "idempotency_key" = btrim("idempotency_key")
    ),
    CONSTRAINT "aipcs_registry_mutation_fingerprint_check"
        CHECK ("fingerprint" ~ '^[0-9a-f]{64}$'),
    CONSTRAINT "aipcs_registry_mutation_kind_check" CHECK (
        "operation_kind" IN ('legacy', 'seed', 'design', 'materialise', 'evolve')
    ),
    CONSTRAINT "aipcs_registry_mutation_phase_check"
        CHECK ("phase" IN ('prepared', 'completed', 'recovery_required')),
    CONSTRAINT "aipcs_registry_mutation_created_via_check" CHECK (
        "created_via" IS NULL OR (
            char_length("created_via") BETWEEN 1 AND 64
            AND "created_via" = btrim("created_via")
        )
    ),
    CONSTRAINT "aipcs_registry_mutation_service_revision_check" CHECK (
        "expected_service_revision" IS NULL
        OR "expected_service_revision" BETWEEN 1 AND 9223372036854775807
    ),
    CONSTRAINT "aipcs_registry_mutation_schema_version_check" CHECK (
        "expected_schema_version" IS NULL OR "expected_schema_version" BETWEEN 1 AND 65
    ),
    CONSTRAINT "aipcs_registry_mutation_recovery_check" CHECK (
        "recovery_category" IS NULL OR "recovery_category" = 'recovery_required'
    ),
    CONSTRAINT "aipcs_registry_mutation_shape_check" CHECK (
        (
            "operation_kind" IN ('legacy', 'seed', 'design')
            AND "phase" = 'completed' AND "created_via" IS NULL
            AND "expected_service_revision" IS NULL
            AND "expected_schema_version" IS NULL
            AND "target_manifest_json" IS NULL
            AND "result_json" IS NOT NULL AND "recovery_category" IS NULL
        )
        OR (
            "operation_kind" = 'materialise' AND "created_via" IS NOT NULL
            AND "expected_service_revision" IS NOT NULL
            AND "expected_schema_version" = 1
            AND "target_manifest_json" IS NOT NULL
            AND (
                ("phase" = 'prepared' AND "result_json" IS NULL
                    AND "recovery_category" IS NULL)
                OR ("phase" = 'completed' AND "result_json" IS NOT NULL
                    AND "recovery_category" IS NULL)
                OR ("phase" = 'recovery_required' AND "result_json" IS NULL
                    AND "recovery_category" = 'recovery_required')
            )
        )
        OR (
            "operation_kind" = 'evolve' AND "created_via" IS NOT NULL
            AND "expected_service_revision" IS NOT NULL
            AND "expected_schema_version" BETWEEN 1 AND 64
            AND "target_manifest_json" IS NOT NULL
            AND (
                ("phase" = 'prepared' AND "result_json" IS NULL
                    AND "recovery_category" IS NULL)
                OR ("phase" = 'completed' AND "result_json" IS NOT NULL
                    AND "recovery_category" IS NULL)
                OR ("phase" = 'recovery_required' AND "result_json" IS NULL
                    AND "recovery_category" = 'recovery_required')
            )
        )
    )
)""",
    """CREATE TABLE "aipcs_registry"."aipcs_registry_audit" (
    "audit_id" bigint GENERATED ALWAYS AS IDENTITY,
    "action" text COLLATE "C" NOT NULL,
    "outcome" text COLLATE "C" NOT NULL,
    "service_id" uuid NOT NULL,
    "principal_id" text COLLATE "C" NOT NULL,
    "created_via" text COLLATE "C" NOT NULL,
    "at" timestamp(6) with time zone NOT NULL,
    CONSTRAINT "aipcs_registry_audit_pkey" PRIMARY KEY ("audit_id"),
    CONSTRAINT "aipcs_registry_audit_service_fkey"
        FOREIGN KEY ("service_id", "principal_id")
        REFERENCES "aipcs_registry"."aipcs_registry_service"
            ("service_id", "principal_id")
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT "aipcs_registry_audit_action_check"
        CHECK ("action" IN ('seed', 'design', 'materialise', 'evolve')),
    CONSTRAINT "aipcs_registry_audit_outcome_check" CHECK (
        ("action" = 'seed' AND "outcome" IN ('created', 'duplicate'))
        OR ("action" = 'design' AND "outcome" = 'accepted')
        OR ("action" IN ('materialise', 'evolve')
            AND "outcome" IN ('completed', 'recovery_required'))
    ),
    CONSTRAINT "aipcs_registry_audit_principal_check" CHECK (
        char_length("principal_id") BETWEEN 1 AND 128
        AND "principal_id" = btrim("principal_id")
    ),
    CONSTRAINT "aipcs_registry_audit_created_via_check" CHECK (
        char_length("created_via") BETWEEN 1 AND 64
        AND "created_via" = btrim("created_via")
    )
)""",
    """CREATE INDEX "aipcs_registry_service_list"
    ON "aipcs_registry"."aipcs_registry_service"
        ("principal_id", "created_at" ASC, "service_id" ASC)""",
    """CREATE UNIQUE INDEX "aipcs_registry_mutation_active_lifecycle"
    ON "aipcs_registry"."aipcs_registry_mutation" ("principal_id", "service_id")
    WHERE "operation_kind" IN ('materialise', 'evolve')
      AND "phase" IN ('prepared', 'recovery_required')""",
    """CREATE INDEX "aipcs_registry_audit_principal"
    ON "aipcs_registry"."aipcs_registry_audit" ("principal_id", "audit_id" DESC)""",
    'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA "aipcs_registry" FROM PUBLIC',
    'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA "aipcs_registry" FROM PUBLIC',
)

CHECKSUM = hashlib.sha256("\n".join(DDL).encode()).hexdigest()

TABLE_COLUMNS = {
    "aipcs_registry_meta": (
        ("singleton", "smallint", True, "", False),
        ("adapter_id", "text", True, "", False),
        ("component", "text", True, "", False),
        ("applied_revision", "bigint", True, "", False),
        ("dirty", "boolean", True, "", False),
    ),
    "aipcs_registry_migration": (
        ("component", "text", True, "", False),
        ("revision", "bigint", True, "", False),
        ("migration_id", "text", True, "", False),
        ("checksum", "text", True, "", False),
        ("applied_at", "timestamp(6) with time zone", True, "", False),
    ),
    "aipcs_registry_service": (
        ("service_id", "uuid", True, "", False),
        ("principal_id", "text", True, "", False),
        ("domain_name", "text", True, "", False),
        ("domain_class", "text", True, "", False),
        ("intent_description", "text", True, "", False),
        ("created_at", "timestamp(6) with time zone", True, "", False),
        ("updated_at", "timestamp(6) with time zone", True, "", False),
        ("last_activity_at", "timestamp(6) with time zone", True, "", False),
        ("manifest_json", "jsonb", False, "", False),
        ("schema_version", "smallint", False, "", False),
        ("design_state", "text", True, "", False),
        ("operational_status", "text", True, "", False),
        ("service_revision", "bigint", True, "", False),
        ("materialised_at", "timestamp(6) with time zone", False, "", False),
        ("storage_backend", "text", False, "", False),
        ("storage_namespace", "text", False, "", False),
    ),
    "aipcs_registry_mutation": (
        ("principal_id", "text", True, "", False),
        ("idempotency_key", "text", True, "", False),
        ("fingerprint", "text", True, "", False),
        ("service_id", "uuid", True, "", False),
        ("operation_kind", "text", True, "", False),
        ("phase", "text", True, "", False),
        ("created_via", "text", False, "", False),
        ("expected_service_revision", "bigint", False, "", False),
        ("expected_schema_version", "smallint", False, "", False),
        ("target_manifest_json", "jsonb", False, "", False),
        ("result_json", "jsonb", False, "", False),
        ("recovery_category", "text", False, "", False),
    ),
    "aipcs_registry_audit": (
        ("audit_id", "bigint", True, "a", False),
        ("action", "text", True, "", False),
        ("outcome", "text", True, "", False),
        ("service_id", "uuid", True, "", False),
        ("principal_id", "text", True, "", False),
        ("created_via", "text", True, "", False),
        ("at", "timestamp(6) with time zone", True, "", False),
    ),
}

CONSTRAINT_TYPES = {
    "aipcs_registry_meta": {
        "aipcs_registry_meta_pkey": "p",
        "aipcs_registry_meta_singleton_check": "c",
        "aipcs_registry_meta_adapter_check": "c",
        "aipcs_registry_meta_component_check": "c",
        "aipcs_registry_meta_revision_check": "c",
    },
    "aipcs_registry_migration": {
        "aipcs_registry_migration_pkey": "p",
        "aipcs_registry_migration_id_key": "u",
        "aipcs_registry_migration_component_check": "c",
        "aipcs_registry_migration_revision_check": "c",
        "aipcs_registry_migration_id_check": "c",
        "aipcs_registry_migration_checksum_check": "c",
    },
    "aipcs_registry_service": {
        "aipcs_registry_service_pkey": "p",
        "aipcs_registry_service_identity_key": "u",
        "aipcs_registry_service_domain_key": "u",
        "aipcs_registry_service_id_check": "c",
        "aipcs_registry_service_principal_check": "c",
        "aipcs_registry_service_domain_check": "c",
        "aipcs_registry_service_class_check": "c",
        "aipcs_registry_service_intent_check": "c",
        "aipcs_registry_service_schema_check": "c",
        "aipcs_registry_service_design_check": "c",
        "aipcs_registry_service_status_check": "c",
        "aipcs_registry_service_revision_check": "c",
        "aipcs_registry_service_manifest_pair_check": "c",
        "aipcs_registry_service_materialisation_check": "c",
    },
    "aipcs_registry_mutation": {
        "aipcs_registry_mutation_pkey": "p",
        "aipcs_registry_mutation_service_fkey": "f",
        "aipcs_registry_mutation_principal_check": "c",
        "aipcs_registry_mutation_key_check": "c",
        "aipcs_registry_mutation_fingerprint_check": "c",
        "aipcs_registry_mutation_kind_check": "c",
        "aipcs_registry_mutation_phase_check": "c",
        "aipcs_registry_mutation_created_via_check": "c",
        "aipcs_registry_mutation_service_revision_check": "c",
        "aipcs_registry_mutation_schema_version_check": "c",
        "aipcs_registry_mutation_recovery_check": "c",
        "aipcs_registry_mutation_shape_check": "c",
    },
    "aipcs_registry_audit": {
        "aipcs_registry_audit_pkey": "p",
        "aipcs_registry_audit_service_fkey": "f",
        "aipcs_registry_audit_action_check": "c",
        "aipcs_registry_audit_outcome_check": "c",
        "aipcs_registry_audit_principal_check": "c",
        "aipcs_registry_audit_created_via_check": "c",
    },
}

INDEX_SIGNATURES = {
    "aipcs_registry_meta_pkey": ("aipcs_registry_meta", True, True, False),
    "aipcs_registry_migration_pkey": ("aipcs_registry_migration", True, True, False),
    "aipcs_registry_migration_id_key": ("aipcs_registry_migration", True, False, False),
    "aipcs_registry_service_pkey": ("aipcs_registry_service", True, True, False),
    "aipcs_registry_service_identity_key": ("aipcs_registry_service", True, False, False),
    "aipcs_registry_service_domain_key": ("aipcs_registry_service", True, False, False),
    "aipcs_registry_service_list": ("aipcs_registry_service", False, False, False),
    "aipcs_registry_mutation_pkey": ("aipcs_registry_mutation", True, True, False),
    "aipcs_registry_mutation_active_lifecycle": (
        "aipcs_registry_mutation",
        True,
        False,
        True,
    ),
    "aipcs_registry_audit_pkey": ("aipcs_registry_audit", True, True, False),
    "aipcs_registry_audit_principal": ("aipcs_registry_audit", False, False, False),
}

SEQUENCES = frozenset({"aipcs_registry_audit_audit_id_seq"})

INDEX_COLUMNS = {
    "aipcs_registry_meta_pkey": ("singleton",),
    "aipcs_registry_migration_pkey": ("component", "revision"),
    "aipcs_registry_migration_id_key": ("component", "migration_id"),
    "aipcs_registry_service_pkey": ("service_id",),
    "aipcs_registry_service_identity_key": ("service_id", "principal_id"),
    "aipcs_registry_service_domain_key": ("principal_id", "domain_name"),
    "aipcs_registry_service_list": ("principal_id", "created_at", "service_id"),
    "aipcs_registry_mutation_pkey": ("principal_id", "idempotency_key"),
    "aipcs_registry_mutation_active_lifecycle": ("principal_id", "service_id"),
    "aipcs_registry_audit_pkey": ("audit_id",),
    "aipcs_registry_audit_principal": ("principal_id", "audit_id"),
}

INDEX_OPTIONS = {
    **{name: (0,) * len(columns) for name, columns in INDEX_COLUMNS.items()},
    "aipcs_registry_audit_principal": (0, 3),
}

INDEX_OPCLASSES = {
    "aipcs_registry_meta_pkey": ("int2_ops",),
    "aipcs_registry_migration_pkey": ("text_ops", "int8_ops"),
    "aipcs_registry_migration_id_key": ("text_ops", "text_ops"),
    "aipcs_registry_service_pkey": ("uuid_ops",),
    "aipcs_registry_service_identity_key": ("uuid_ops", "text_ops"),
    "aipcs_registry_service_domain_key": ("text_ops", "text_ops"),
    "aipcs_registry_service_list": ("text_ops", "timestamptz_ops", "uuid_ops"),
    "aipcs_registry_mutation_pkey": ("text_ops", "text_ops"),
    "aipcs_registry_mutation_active_lifecycle": ("text_ops", "uuid_ops"),
    "aipcs_registry_audit_pkey": ("int8_ops",),
    "aipcs_registry_audit_principal": ("text_ops", "int8_ops"),
}

INDEX_COLLATIONS = {
    "aipcs_registry_meta_pkey": ("",),
    "aipcs_registry_migration_pkey": ("C", ""),
    "aipcs_registry_migration_id_key": ("C", "C"),
    "aipcs_registry_service_pkey": ("",),
    "aipcs_registry_service_identity_key": ("", "C"),
    "aipcs_registry_service_domain_key": ("C", "C"),
    "aipcs_registry_service_list": ("C", "", ""),
    "aipcs_registry_mutation_pkey": ("C", "C"),
    "aipcs_registry_mutation_active_lifecycle": ("C", ""),
    "aipcs_registry_audit_pkey": ("",),
    "aipcs_registry_audit_principal": ("C", ""),
}

CONSTRAINT_KEYS = {
    "aipcs_registry_meta_pkey": (
        "aipcs_registry_meta",
        "p",
        ("singleton",),
        None,
        (),
    ),
    "aipcs_registry_migration_pkey": (
        "aipcs_registry_migration",
        "p",
        ("component", "revision"),
        None,
        (),
    ),
    "aipcs_registry_migration_id_key": (
        "aipcs_registry_migration",
        "u",
        ("component", "migration_id"),
        None,
        (),
    ),
    "aipcs_registry_service_pkey": (
        "aipcs_registry_service",
        "p",
        ("service_id",),
        None,
        (),
    ),
    "aipcs_registry_service_identity_key": (
        "aipcs_registry_service",
        "u",
        ("service_id", "principal_id"),
        None,
        (),
    ),
    "aipcs_registry_service_domain_key": (
        "aipcs_registry_service",
        "u",
        ("principal_id", "domain_name"),
        None,
        (),
    ),
    "aipcs_registry_mutation_pkey": (
        "aipcs_registry_mutation",
        "p",
        ("principal_id", "idempotency_key"),
        None,
        (),
    ),
    "aipcs_registry_mutation_service_fkey": (
        "aipcs_registry_mutation",
        "f",
        ("service_id", "principal_id"),
        "aipcs_registry_service",
        ("service_id", "principal_id"),
    ),
    "aipcs_registry_audit_pkey": (
        "aipcs_registry_audit",
        "p",
        ("audit_id",),
        None,
        (),
    ),
    "aipcs_registry_audit_service_fkey": (
        "aipcs_registry_audit",
        "f",
        ("service_id", "principal_id"),
        "aipcs_registry_service",
        ("service_id", "principal_id"),
    ),
}

CHECK_TOKENS = {
    "aipcs_registry_meta_singleton_check": ('"singleton"', "= 1"),
    "aipcs_registry_meta_adapter_check": (
        '"adapter_id"',
        "aipcs.postgresql.registry",
    ),
    "aipcs_registry_meta_component_check": ('"component"', "registry"),
    "aipcs_registry_meta_revision_check": ('"applied_revision"', ">= 0"),
    "aipcs_registry_migration_component_check": ('"component"', "registry"),
    "aipcs_registry_migration_revision_check": ('"revision"', "> 0"),
    "aipcs_registry_migration_id_check": (
        "char_length",
        '"migration_id"',
        ">= 1",
        "<= 96",
    ),
    "aipcs_registry_migration_checksum_check": (
        '"checksum"',
        "[0-9a-f]{64}",
    ),
    "aipcs_registry_service_id_check": (
        '"service_id"',
        "00000000-0000-0000-0000-000000000000",
    ),
    "aipcs_registry_service_principal_check": (
        "char_length",
        '"principal_id"',
        "btrim",
        "128",
    ),
    "aipcs_registry_service_domain_check": (
        '"domain_name"',
        "[a-z][a-z0-9_]{0,62}",
    ),
    "aipcs_registry_service_class_check": (
        "char_length",
        '"domain_class"',
        "btrim",
        "64",
    ),
    "aipcs_registry_service_intent_check": (
        "char_length",
        '"intent_description"',
        "btrim",
        "1000",
    ),
    "aipcs_registry_service_schema_check": ('"schema_version"', ">= 1", "<= 65"),
    "aipcs_registry_service_design_check": (
        '"design_state"',
        "seeded",
        "materialised",
    ),
    "aipcs_registry_service_status_check": (
        '"operational_status"',
        "active",
        "suspended",
        "archived",
    ),
    "aipcs_registry_service_revision_check": (
        '"service_revision"',
        "9223372036854775807",
    ),
    "aipcs_registry_service_manifest_pair_check": (
        '"manifest_json"',
        '"schema_version"',
        "IS NULL",
        "IS NOT NULL",
    ),
    "aipcs_registry_service_materialisation_check": (
        '"design_state"',
        '"materialised_at"',
        '"storage_backend"',
        '"storage_namespace"',
        '"manifest_json"',
        "seeded",
        "materialised",
        "postgresql",
        "sqlite",
        "svc_[0-9a-f]{32}",
        "replace",
        '"service_id"',
    ),
    "aipcs_registry_mutation_principal_check": (
        "char_length",
        '"principal_id"',
        "btrim",
        "128",
    ),
    "aipcs_registry_mutation_key_check": (
        "char_length",
        '"idempotency_key"',
        "btrim",
        "128",
    ),
    "aipcs_registry_mutation_fingerprint_check": (
        '"fingerprint"',
        "[0-9a-f]{64}",
    ),
    "aipcs_registry_mutation_kind_check": (
        '"operation_kind"',
        "legacy",
        "seed",
        "design",
        "materialise",
        "evolve",
    ),
    "aipcs_registry_mutation_phase_check": (
        '"phase"',
        "prepared",
        "completed",
        "recovery_required",
    ),
    "aipcs_registry_mutation_created_via_check": (
        "char_length",
        '"created_via"',
        "btrim",
        "64",
    ),
    "aipcs_registry_mutation_service_revision_check": (
        '"expected_service_revision"',
        "9223372036854775807",
    ),
    "aipcs_registry_mutation_schema_version_check": (
        '"expected_schema_version"',
        ">= 1",
        "<= 65",
    ),
    "aipcs_registry_mutation_recovery_check": (
        '"recovery_category"',
        "recovery_required",
    ),
    "aipcs_registry_mutation_shape_check": (
        '"operation_kind"',
        '"phase"',
        '"created_via"',
        '"expected_service_revision"',
        '"expected_schema_version"',
        '"target_manifest_json"',
        '"result_json"',
        '"recovery_category"',
        "legacy",
        "seed",
        "design",
        "materialise",
        "evolve",
        "prepared",
        "completed",
        "recovery_required",
    ),
    "aipcs_registry_audit_action_check": (
        '"action"',
        "seed",
        "design",
        "materialise",
        "evolve",
    ),
    "aipcs_registry_audit_outcome_check": (
        '"action"',
        '"outcome"',
        "created",
        "duplicate",
        "accepted",
        "completed",
        "recovery_required",
    ),
    "aipcs_registry_audit_principal_check": (
        "char_length",
        '"principal_id"',
        "btrim",
        "128",
    ),
    "aipcs_registry_audit_created_via_check": (
        "char_length",
        '"created_via"',
        "btrim",
        "64",
    ),
}

CHECK_EXPRESSION_DIGEST = "c6f65f1c329b5310be94ca856801affbea43818387be0d7ec662649b892972d7"

INDEX_PREDICATE_TOKENS = {
    "aipcs_registry_mutation_active_lifecycle": (
        '"operation_kind"',
        "materialise",
        "evolve",
        '"phase"',
        "prepared",
        "recovery_required",
    )
}

INDEX_PREDICATES = {
    "aipcs_registry_mutation_active_lifecycle": (
        "((operation_kind = ANY (ARRAY['materialise'::text, 'evolve'::text])) "
        "AND (phase = ANY (ARRAY['prepared'::text, 'recovery_required'::text])))"
    )
}

# PostgreSQL R1 remains immutable historical evidence.  R2 deliberately uses
# separate durable claim and identity ledgers so that an imported service can
# be reserved before it is visible and a purged service can leave minimal
# conflict evidence without a foreign key to a live service row.
R1_TARGET_REVISION = TARGET_REVISION
R1_MIGRATION_ID = MIGRATION_ID
R1_DDL = DDL
R1_CHECKSUM = CHECKSUM
R1_TABLE_COLUMNS = TABLE_COLUMNS
R1_CONSTRAINT_TYPES = CONSTRAINT_TYPES
R1_INDEX_SIGNATURES = INDEX_SIGNATURES
R1_SEQUENCES = SEQUENCES
R1_INDEX_COLUMNS = INDEX_COLUMNS
R1_INDEX_OPTIONS = INDEX_OPTIONS
R1_INDEX_OPCLASSES = INDEX_OPCLASSES
R1_INDEX_COLLATIONS = INDEX_COLLATIONS
R1_CONSTRAINT_KEYS = CONSTRAINT_KEYS
R1_CHECK_TOKENS = CHECK_TOKENS
R1_INDEX_PREDICATE_TOKENS = INDEX_PREDICATE_TOKENS
R1_INDEX_PREDICATES = INDEX_PREDICATES
R1_CHECK_EXPRESSION_DIGEST = CHECK_EXPRESSION_DIGEST

TARGET_REVISION = 2
MIGRATION_ID = "postgresql-registry-0002-portable-lifecycle"

R2_DDL = (
    """CREATE TABLE "aipcs_registry"."aipcs_registry_claim" (
    "principal_id" text COLLATE "C" NOT NULL,
    "idempotency_key" text COLLATE "C" NOT NULL,
    "fingerprint" text COLLATE "C" NOT NULL,
    "service_id" uuid NOT NULL,
    "operation_kind" text COLLATE "C" NOT NULL,
    "phase" text COLLATE "C" NOT NULL,
    "created_via" text COLLATE "C",
    "intent_json" jsonb,
    "result_json" jsonb,
    "recovery_category" text COLLATE "C",
    CONSTRAINT "aipcs_registry_claim_pkey" PRIMARY KEY ("principal_id", "idempotency_key"),
    CONSTRAINT "aipcs_registry_claim_identity_key"
        UNIQUE ("principal_id", "idempotency_key", "service_id"),
    CONSTRAINT "aipcs_registry_claim_principal_check" CHECK (
        char_length("principal_id") BETWEEN 1 AND 128
        AND "principal_id" = btrim("principal_id")
    ),
    CONSTRAINT "aipcs_registry_claim_key_check" CHECK (
        char_length("idempotency_key") BETWEEN 1 AND 128
        AND "idempotency_key" = btrim("idempotency_key")
    ),
    CONSTRAINT "aipcs_registry_claim_fingerprint_check"
        CHECK ("fingerprint" ~ '^[0-9a-f]{64}$'),
    CONSTRAINT "aipcs_registry_claim_service_check"
        CHECK ("service_id" <> '00000000-0000-0000-0000-000000000000'::uuid),
    CONSTRAINT "aipcs_registry_claim_kind_check" CHECK (
        "operation_kind" IN (
            'legacy', 'seed', 'design', 'materialise', 'evolve', 'suspend',
            'resume', 'archive', 'restore', 'export', 'import', 'purge'
        )
    ),
    CONSTRAINT "aipcs_registry_claim_phase_check" CHECK (
        "phase" IN ('prepared', 'completed', 'recovery_required', 'tombstoned')
    ),
    CONSTRAINT "aipcs_registry_claim_created_via_check" CHECK (
        "created_via" IS NULL OR (
            char_length("created_via") BETWEEN 1 AND 64
            AND "created_via" = btrim("created_via")
        )
    ),
    CONSTRAINT "aipcs_registry_claim_intent_check" CHECK (
        "intent_json" IS NULL OR jsonb_typeof("intent_json") = 'object'
    ),
    CONSTRAINT "aipcs_registry_claim_result_check" CHECK (
        "result_json" IS NULL OR jsonb_typeof("result_json") = 'object'
    ),
    CONSTRAINT "aipcs_registry_claim_recovery_check" CHECK (
        "recovery_category" IS NULL OR "recovery_category" = 'recovery_required'
    ),
    CONSTRAINT "aipcs_registry_claim_shape_check" CHECK (
        ("phase" = 'tombstoned' AND "operation_kind" <> 'purge'
            AND "created_via" IS NULL AND "intent_json" IS NULL
            AND "result_json" IS NULL AND "recovery_category" IS NULL)
        OR ("phase" = 'prepared' AND "operation_kind" NOT IN ('legacy', 'seed', 'design')
            AND "created_via" IS NOT NULL AND "intent_json" IS NOT NULL
            AND "result_json" IS NULL AND "recovery_category" IS NULL)
        OR ("phase" = 'completed' AND (
                ("operation_kind" IN ('legacy', 'seed', 'design')
                    AND "created_via" IS NULL AND "intent_json" IS NULL)
                OR ("operation_kind" NOT IN ('legacy', 'seed', 'design')
                    AND "created_via" IS NOT NULL AND "intent_json" IS NOT NULL)
            ) AND "result_json" IS NOT NULL AND "recovery_category" IS NULL)
        OR ("phase" = 'recovery_required'
            AND "operation_kind" NOT IN ('legacy', 'seed', 'design')
            AND "created_via" IS NOT NULL AND "intent_json" IS NOT NULL
            AND "result_json" IS NULL AND "recovery_category" = 'recovery_required')
    )
)""",
    """INSERT INTO "aipcs_registry"."aipcs_registry_claim" (
    "principal_id","idempotency_key","fingerprint","service_id","operation_kind",
    "phase","created_via","intent_json","result_json","recovery_category"
)
SELECT "principal_id","idempotency_key","fingerprint","service_id","operation_kind",
    "phase","created_via",
    CASE WHEN "operation_kind" IN ('legacy', 'seed', 'design') THEN NULL
    ELSE jsonb_build_object(
        'created_via',"created_via",
        'expected_service_revision',"expected_service_revision",
        'expected_schema_version',"expected_schema_version",
        'kind',"operation_kind",'principal',"principal_id",'service_id',"service_id",
        'target_manifest',"target_manifest_json"
    ) END,
    "result_json","recovery_category"
FROM "aipcs_registry"."aipcs_registry_mutation" """,
    '''DROP TABLE "aipcs_registry"."aipcs_registry_mutation"''',
    """CREATE TABLE "aipcs_registry"."aipcs_registry_identity" (
    "service_id" uuid NOT NULL,
    "principal_id" text COLLATE "C" NOT NULL,
    "domain_name" text COLLATE "C",
    "storage_backend" text COLLATE "C",
    "storage_namespace" text COLLATE "C" NOT NULL,
    "identity_state" text COLLATE "C" NOT NULL,
    "claim_idempotency_key" text COLLATE "C",
    "lifecycle_at" timestamp(6) with time zone,
    CONSTRAINT "aipcs_registry_identity_pkey" PRIMARY KEY ("service_id"),
    CONSTRAINT "aipcs_registry_identity_principal_check" CHECK (
        char_length("principal_id") BETWEEN 1 AND 128
        AND "principal_id" = btrim("principal_id")
    ),
    CONSTRAINT "aipcs_registry_identity_domain_check" CHECK (
        "domain_name" IS NULL OR (
            char_length("domain_name") BETWEEN 1 AND 63
            AND "domain_name" ~ '^[a-z][a-z0-9_]{0,62}$'
        )
    ),
    CONSTRAINT "aipcs_registry_identity_namespace_check" CHECK (
        "storage_namespace" ~ '^svc_[0-9a-f]{32}$'
        AND "storage_namespace" <> 'svc_00000000000000000000000000000000'
        AND substr("storage_namespace", 5) = replace("service_id"::text, '-', '')
    ),
    CONSTRAINT "aipcs_registry_identity_backend_check" CHECK (
        "storage_backend" IS NULL OR "storage_backend" IN ('sqlite', 'postgresql')
    ),
    CONSTRAINT "aipcs_registry_identity_state_check" CHECK (
        "identity_state" IN ('live', 'import_prepared', 'tombstoned')
    ),
    CONSTRAINT "aipcs_registry_identity_claim_key_check" CHECK (
        "claim_idempotency_key" IS NULL OR (
            char_length("claim_idempotency_key") BETWEEN 1 AND 128
            AND "claim_idempotency_key" = btrim("claim_idempotency_key")
        )
    ),
    CONSTRAINT "aipcs_registry_identity_claim_fkey"
        FOREIGN KEY ("principal_id", "claim_idempotency_key", "service_id")
        REFERENCES "aipcs_registry"."aipcs_registry_claim"
            ("principal_id", "idempotency_key", "service_id")
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT "aipcs_registry_identity_shape_check" CHECK (
        ("identity_state" = 'live' AND "domain_name" IS NOT NULL
            AND "claim_idempotency_key" IS NULL AND "lifecycle_at" IS NULL)
        OR ("identity_state" = 'import_prepared' AND "domain_name" IS NOT NULL
            AND "storage_backend" IS NOT NULL AND "claim_idempotency_key" IS NOT NULL
            AND "lifecycle_at" IS NOT NULL)
        OR ("identity_state" = 'tombstoned' AND "domain_name" IS NULL
            AND "storage_backend" IS NULL AND "claim_idempotency_key" IS NOT NULL
            AND "lifecycle_at" IS NOT NULL)
    )
)""",
    """INSERT INTO "aipcs_registry"."aipcs_registry_identity" (
    "service_id","principal_id","domain_name","storage_backend","storage_namespace","identity_state"
)
SELECT "service_id","principal_id","domain_name",
    "storage_backend",'svc_' || replace("service_id"::text, '-', ''),'live'
FROM "aipcs_registry"."aipcs_registry_service" """,
    """CREATE TABLE "aipcs_registry"."aipcs_registry_receipt" (
    "receipt_id" uuid NOT NULL,
    "principal_id" text COLLATE "C" NOT NULL,
    "idempotency_key" text COLLATE "C" NOT NULL,
    "service_id" uuid NOT NULL,
    "bundle_root_sha256" text COLLATE "C" NOT NULL,
    "receipt_kind" text COLLATE "C" NOT NULL,
    "export_format_version" smallint NOT NULL,
    "storage_backend" text COLLATE "C" NOT NULL,
    "service_revision" bigint NOT NULL,
    "operational_status" text COLLATE "C" NOT NULL,
    "verification" text COLLATE "C" NOT NULL,
    "created_at" timestamp(6) with time zone NOT NULL,
    CONSTRAINT "aipcs_registry_receipt_pkey" PRIMARY KEY ("receipt_id"),
    CONSTRAINT "aipcs_registry_receipt_claim_key"
        UNIQUE ("principal_id", "idempotency_key"),
    CONSTRAINT "aipcs_registry_receipt_claim_fkey"
        FOREIGN KEY ("principal_id", "idempotency_key", "service_id")
        REFERENCES "aipcs_registry"."aipcs_registry_claim"
            ("principal_id", "idempotency_key", "service_id")
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT "aipcs_registry_receipt_id_check"
        CHECK ("receipt_id" <> '00000000-0000-0000-0000-000000000000'::uuid),
    CONSTRAINT "aipcs_registry_receipt_principal_check" CHECK (
        char_length("principal_id") BETWEEN 1 AND 128
        AND "principal_id" = btrim("principal_id")
    ),
    CONSTRAINT "aipcs_registry_receipt_key_check" CHECK (
        char_length("idempotency_key") BETWEEN 1 AND 128
        AND "idempotency_key" = btrim("idempotency_key")
    ),
    CONSTRAINT "aipcs_registry_receipt_service_check"
        CHECK ("service_id" <> '00000000-0000-0000-0000-000000000000'::uuid),
    CONSTRAINT "aipcs_registry_receipt_root_check"
        CHECK ("bundle_root_sha256" ~ '^[0-9a-f]{64}$'),
    CONSTRAINT "aipcs_registry_receipt_kind_check"
        CHECK ("receipt_kind" IN ('export', 'import')),
    CONSTRAINT "aipcs_registry_receipt_format_check"
        CHECK ("export_format_version" = 1),
    CONSTRAINT "aipcs_registry_receipt_backend_check"
        CHECK ("storage_backend" IN ('sqlite', 'postgresql')),
    CONSTRAINT "aipcs_registry_receipt_revision_check"
        CHECK ("service_revision" BETWEEN 1 AND 9223372036854775807),
    CONSTRAINT "aipcs_registry_receipt_status_check"
        CHECK ("operational_status" IN ('active', 'suspended', 'archived')),
    CONSTRAINT "aipcs_registry_receipt_verification_check"
        CHECK ("verification" IN ('unverified', 'verified'))
)""",
    '''ALTER TABLE "aipcs_registry"."aipcs_registry_audit"
    DROP CONSTRAINT "aipcs_registry_audit_service_fkey"''',
    '''ALTER TABLE "aipcs_registry"."aipcs_registry_service"
    DROP CONSTRAINT "aipcs_registry_service_identity_key"''',
    '''ALTER TABLE "aipcs_registry"."aipcs_registry_service"
    DROP CONSTRAINT "aipcs_registry_service_domain_key"''',
    '''ALTER TABLE "aipcs_registry"."aipcs_registry_audit"
    DROP CONSTRAINT "aipcs_registry_audit_action_check"''',
    '''ALTER TABLE "aipcs_registry"."aipcs_registry_audit"
    DROP CONSTRAINT "aipcs_registry_audit_outcome_check"''',
    """ALTER TABLE "aipcs_registry"."aipcs_registry_audit"
    ADD CONSTRAINT "aipcs_registry_audit_action_check" CHECK (
        "action" IN ('seed', 'design', 'materialise', 'evolve', 'suspend', 'resume',
            'archive', 'restore', 'export', 'import', 'purge')
    )""",
    """ALTER TABLE "aipcs_registry"."aipcs_registry_audit"
    ADD CONSTRAINT "aipcs_registry_audit_outcome_check" CHECK (
        ("action" = 'seed' AND "outcome" IN ('created', 'duplicate'))
        OR ("action" = 'design' AND "outcome" = 'accepted')
        OR ("action" IN ('materialise', 'evolve', 'suspend', 'resume', 'archive',
            'restore', 'export', 'import', 'purge')
            AND "outcome" IN ('completed', 'recovery_required'))
    )""",
    """CREATE UNIQUE INDEX "aipcs_registry_claim_active_lifecycle"
    ON "aipcs_registry"."aipcs_registry_claim" ("principal_id", "service_id")
    WHERE "operation_kind" IN ('materialise', 'evolve', 'suspend', 'resume', 'archive',
        'restore', 'export', 'import', 'purge')
      AND "phase" IN ('prepared', 'recovery_required')""",
    """CREATE UNIQUE INDEX "aipcs_registry_identity_live_domain"
    ON "aipcs_registry"."aipcs_registry_identity" ("principal_id", "domain_name")
    WHERE "identity_state" IN ('live', 'import_prepared')""",
    """CREATE INDEX "aipcs_registry_receipt_service"
    ON "aipcs_registry"."aipcs_registry_receipt"
        ("principal_id", "service_id", "created_at" DESC)""",
    """CREATE UNIQUE INDEX "aipcs_registry_identity_namespace_key"
    ON "aipcs_registry"."aipcs_registry_identity" ("storage_namespace")""",
    'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA "aipcs_registry" FROM PUBLIC',
    'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA "aipcs_registry" FROM PUBLIC',
)
R2_CHECKSUM = hashlib.sha256("\n".join(R2_DDL).encode()).hexdigest()

# A new install creates R1 and upgrades within the same transaction.  The
# migration history still records both immutable revisions.
DDL = R1_DDL + R2_DDL
CHECKSUM = R2_CHECKSUM

TABLE_COLUMNS = {
    **R1_TABLE_COLUMNS,
    "aipcs_registry_claim": (
        ("principal_id", "text", True, "", False),
        ("idempotency_key", "text", True, "", False),
        ("fingerprint", "text", True, "", False),
        ("service_id", "uuid", True, "", False),
        ("operation_kind", "text", True, "", False),
        ("phase", "text", True, "", False),
        ("created_via", "text", False, "", False),
        ("intent_json", "jsonb", False, "", False),
        ("result_json", "jsonb", False, "", False),
        ("recovery_category", "text", False, "", False),
    ),
    "aipcs_registry_identity": (
        ("service_id", "uuid", True, "", False),
        ("principal_id", "text", True, "", False),
        ("domain_name", "text", False, "", False),
        ("storage_backend", "text", False, "", False),
        ("storage_namespace", "text", True, "", False),
        ("identity_state", "text", True, "", False),
        ("claim_idempotency_key", "text", False, "", False),
        ("lifecycle_at", "timestamp(6) with time zone", False, "", False),
    ),
    "aipcs_registry_receipt": (
        ("receipt_id", "uuid", True, "", False),
        ("principal_id", "text", True, "", False),
        ("idempotency_key", "text", True, "", False),
        ("service_id", "uuid", True, "", False),
        ("bundle_root_sha256", "text", True, "", False),
        ("receipt_kind", "text", True, "", False),
        ("export_format_version", "smallint", True, "", False),
        ("storage_backend", "text", True, "", False),
        ("service_revision", "bigint", True, "", False),
        ("operational_status", "text", True, "", False),
        ("verification", "text", True, "", False),
        ("created_at", "timestamp(6) with time zone", True, "", False),
    ),
}
del TABLE_COLUMNS["aipcs_registry_mutation"]

CONSTRAINT_TYPES = {
    **R1_CONSTRAINT_TYPES,
    "aipcs_registry_service": {
        name: kind
        for name, kind in R1_CONSTRAINT_TYPES["aipcs_registry_service"].items()
        if name not in {
            "aipcs_registry_service_identity_key",
            "aipcs_registry_service_domain_key",
        }
    },
    "aipcs_registry_claim": {
        "aipcs_registry_claim_pkey": "p",
        "aipcs_registry_claim_identity_key": "u",
        "aipcs_registry_claim_principal_check": "c",
        "aipcs_registry_claim_key_check": "c",
        "aipcs_registry_claim_fingerprint_check": "c",
        "aipcs_registry_claim_service_check": "c",
        "aipcs_registry_claim_kind_check": "c",
        "aipcs_registry_claim_phase_check": "c",
        "aipcs_registry_claim_created_via_check": "c",
        "aipcs_registry_claim_intent_check": "c",
        "aipcs_registry_claim_result_check": "c",
        "aipcs_registry_claim_recovery_check": "c",
        "aipcs_registry_claim_shape_check": "c",
    },
    "aipcs_registry_identity": {
        "aipcs_registry_identity_pkey": "p",
        "aipcs_registry_identity_principal_check": "c",
        "aipcs_registry_identity_domain_check": "c",
        "aipcs_registry_identity_namespace_check": "c",
        "aipcs_registry_identity_backend_check": "c",
        "aipcs_registry_identity_state_check": "c",
        "aipcs_registry_identity_claim_key_check": "c",
        "aipcs_registry_identity_claim_fkey": "f",
        "aipcs_registry_identity_shape_check": "c",
    },
    "aipcs_registry_receipt": {
        "aipcs_registry_receipt_pkey": "p",
        "aipcs_registry_receipt_claim_key": "u",
        "aipcs_registry_receipt_claim_fkey": "f",
        "aipcs_registry_receipt_id_check": "c",
        "aipcs_registry_receipt_principal_check": "c",
        "aipcs_registry_receipt_key_check": "c",
        "aipcs_registry_receipt_service_check": "c",
        "aipcs_registry_receipt_root_check": "c",
        "aipcs_registry_receipt_kind_check": "c",
        "aipcs_registry_receipt_format_check": "c",
        "aipcs_registry_receipt_backend_check": "c",
        "aipcs_registry_receipt_revision_check": "c",
        "aipcs_registry_receipt_status_check": "c",
        "aipcs_registry_receipt_verification_check": "c",
    },
    "aipcs_registry_audit": {
        name: kind
        for name, kind in R1_CONSTRAINT_TYPES["aipcs_registry_audit"].items()
        if name != "aipcs_registry_audit_service_fkey"
    },
}
del CONSTRAINT_TYPES["aipcs_registry_mutation"]

INDEX_SIGNATURES = {
    name: value
    for name, value in R1_INDEX_SIGNATURES.items()
    if name
    not in {
        "aipcs_registry_service_identity_key",
        "aipcs_registry_service_domain_key",
        "aipcs_registry_mutation_pkey",
        "aipcs_registry_mutation_active_lifecycle",
    }
}
INDEX_SIGNATURES.update(
    {
        "aipcs_registry_claim_pkey": ("aipcs_registry_claim", True, True, False),
        "aipcs_registry_claim_identity_key": (
            "aipcs_registry_claim",
            True,
            False,
            False,
        ),
        "aipcs_registry_claim_active_lifecycle": (
            "aipcs_registry_claim",
            True,
            False,
            True,
        ),
        "aipcs_registry_identity_pkey": ("aipcs_registry_identity", True, True, False),
        "aipcs_registry_identity_live_domain": (
            "aipcs_registry_identity",
            True,
            False,
            True,
        ),
        "aipcs_registry_identity_namespace_key": (
            "aipcs_registry_identity",
            True,
            False,
            False,
        ),
        "aipcs_registry_receipt_pkey": ("aipcs_registry_receipt", True, True, False),
        "aipcs_registry_receipt_claim_key": ("aipcs_registry_receipt", True, False, False),
        "aipcs_registry_receipt_service": ("aipcs_registry_receipt", False, False, False),
    }
)

INDEX_COLUMNS = {
    name: value
    for name, value in R1_INDEX_COLUMNS.items()
    if name in INDEX_SIGNATURES
}
INDEX_COLUMNS.update(
    {
        "aipcs_registry_claim_pkey": ("principal_id", "idempotency_key"),
        "aipcs_registry_claim_identity_key": (
            "principal_id",
            "idempotency_key",
            "service_id",
        ),
        "aipcs_registry_claim_active_lifecycle": ("principal_id", "service_id"),
        "aipcs_registry_identity_pkey": ("service_id",),
        "aipcs_registry_identity_live_domain": ("principal_id", "domain_name"),
        "aipcs_registry_identity_namespace_key": ("storage_namespace",),
        "aipcs_registry_receipt_pkey": ("receipt_id",),
        "aipcs_registry_receipt_claim_key": ("principal_id", "idempotency_key"),
        "aipcs_registry_receipt_service": ("principal_id", "service_id", "created_at"),
    }
)
INDEX_OPTIONS = {
    **{name: (0,) * len(columns) for name, columns in INDEX_COLUMNS.items()},
    "aipcs_registry_audit_principal": (0, 3),
    "aipcs_registry_receipt_service": (0, 0, 3),
}
INDEX_OPCLASSES = {
    name: R1_INDEX_OPCLASSES[name]
    for name in INDEX_SIGNATURES
    if name in R1_INDEX_OPCLASSES
}
INDEX_OPCLASSES.update(
    {
        "aipcs_registry_claim_pkey": ("text_ops", "text_ops"),
        "aipcs_registry_claim_identity_key": ("text_ops", "text_ops", "uuid_ops"),
        "aipcs_registry_claim_active_lifecycle": ("text_ops", "uuid_ops"),
        "aipcs_registry_identity_pkey": ("uuid_ops",),
        "aipcs_registry_identity_live_domain": ("text_ops", "text_ops"),
        "aipcs_registry_identity_namespace_key": ("text_ops",),
        "aipcs_registry_receipt_pkey": ("uuid_ops",),
        "aipcs_registry_receipt_claim_key": ("text_ops", "text_ops"),
        "aipcs_registry_receipt_service": ("text_ops", "uuid_ops", "timestamptz_ops"),
    }
)
INDEX_COLLATIONS = {
    name: R1_INDEX_COLLATIONS[name]
    for name in INDEX_SIGNATURES
    if name in R1_INDEX_COLLATIONS
}
INDEX_COLLATIONS.update(
    {
        "aipcs_registry_claim_pkey": ("C", "C"),
        "aipcs_registry_claim_identity_key": ("C", "C", ""),
        "aipcs_registry_claim_active_lifecycle": ("C", ""),
        "aipcs_registry_identity_pkey": ("",),
        "aipcs_registry_identity_live_domain": ("C", "C"),
        "aipcs_registry_identity_namespace_key": ("C",),
        "aipcs_registry_receipt_pkey": ("",),
        "aipcs_registry_receipt_claim_key": ("C", "C"),
        "aipcs_registry_receipt_service": ("C", "", ""),
    }
)

CONSTRAINT_KEYS = {
    name: value
    for name, value in R1_CONSTRAINT_KEYS.items()
    if name
    not in {
        "aipcs_registry_service_identity_key",
        "aipcs_registry_service_domain_key",
        "aipcs_registry_mutation_pkey",
        "aipcs_registry_mutation_service_fkey",
        "aipcs_registry_audit_service_fkey",
    }
}
CONSTRAINT_KEYS.update(
    {
        "aipcs_registry_claim_pkey": (
            "aipcs_registry_claim",
            "p",
            ("principal_id", "idempotency_key"),
            None,
            (),
        ),
        "aipcs_registry_claim_identity_key": (
            "aipcs_registry_claim",
            "u",
            ("principal_id", "idempotency_key", "service_id"),
            None,
            (),
        ),
        "aipcs_registry_identity_pkey": (
            "aipcs_registry_identity",
            "p",
            ("service_id",),
            None,
            (),
        ),
        "aipcs_registry_identity_claim_fkey": (
            "aipcs_registry_identity",
            "f",
            ("principal_id", "claim_idempotency_key", "service_id"),
            "aipcs_registry_claim",
            ("principal_id", "idempotency_key", "service_id"),
        ),
        "aipcs_registry_receipt_pkey": (
            "aipcs_registry_receipt",
            "p",
            ("receipt_id",),
            None,
            (),
        ),
        "aipcs_registry_receipt_claim_key": (
            "aipcs_registry_receipt",
            "u",
            ("principal_id", "idempotency_key"),
            None,
            (),
        ),
        "aipcs_registry_receipt_claim_fkey": (
            "aipcs_registry_receipt",
            "f",
            ("principal_id", "idempotency_key", "service_id"),
            "aipcs_registry_claim",
            ("principal_id", "idempotency_key", "service_id"),
        ),
    }
)

CHECK_TOKENS = {
    name: tokens
    for name, tokens in R1_CHECK_TOKENS.items()
    if name
    not in {
        "aipcs_registry_mutation_principal_check",
        "aipcs_registry_mutation_key_check",
        "aipcs_registry_mutation_fingerprint_check",
        "aipcs_registry_mutation_kind_check",
        "aipcs_registry_mutation_phase_check",
        "aipcs_registry_mutation_created_via_check",
        "aipcs_registry_mutation_service_revision_check",
        "aipcs_registry_mutation_schema_version_check",
        "aipcs_registry_mutation_recovery_check",
        "aipcs_registry_mutation_shape_check",
    }
}
CHECK_TOKENS.update(
    {
        "aipcs_registry_claim_principal_check": ("principal_id", "char_length", "btrim", "128"),
        "aipcs_registry_claim_key_check": ("idempotency_key", "char_length", "btrim", "128"),
        "aipcs_registry_claim_fingerprint_check": ("fingerprint", "[0-9a-f]{64}"),
        "aipcs_registry_claim_service_check": ("service_id", "00000000-0000"),
        "aipcs_registry_claim_kind_check": ("operation_kind", "export", "import", "purge"),
        "aipcs_registry_claim_phase_check": ("phase", "prepared", "completed", "recovery_required", "tombstoned"),
        "aipcs_registry_claim_created_via_check": ("created_via", "char_length", "btrim", "64"),
        "aipcs_registry_claim_intent_check": ("intent_json", "jsonb_typeof", "object"),
        "aipcs_registry_claim_result_check": ("result_json", "jsonb_typeof", "object"),
        "aipcs_registry_claim_recovery_check": ("recovery_category", "recovery_required"),
        "aipcs_registry_claim_shape_check": ("phase", "intent_json", "result_json", "tombstoned"),
        "aipcs_registry_identity_principal_check": ("principal_id", "char_length", "btrim", "128"),
        "aipcs_registry_identity_domain_check": ("domain_name", "[a-z][a-z0-9_]{0,62}"),
        "aipcs_registry_identity_namespace_check": ("storage_namespace", "svc_[0-9a-f]{32}", "replace", "service_id"),
        "aipcs_registry_identity_backend_check": ("storage_backend", "sqlite", "postgresql"),
        "aipcs_registry_identity_state_check": ("identity_state", "live", "import_prepared", "tombstoned"),
        "aipcs_registry_identity_claim_key_check": ("claim_idempotency_key", "char_length", "btrim", "128"),
        "aipcs_registry_identity_shape_check": ("identity_state", "domain_name", "lifecycle_at", "claim_idempotency_key"),
        "aipcs_registry_receipt_id_check": ("receipt_id", "00000000-0000"),
        "aipcs_registry_receipt_principal_check": ("principal_id", "char_length", "btrim", "128"),
        "aipcs_registry_receipt_key_check": ("idempotency_key", "char_length", "btrim", "128"),
        "aipcs_registry_receipt_service_check": ("service_id", "00000000-0000"),
        "aipcs_registry_receipt_root_check": ("bundle_root_sha256", "[0-9a-f]{64}"),
        "aipcs_registry_receipt_kind_check": ("receipt_kind", "export", "import"),
        "aipcs_registry_receipt_format_check": ("export_format_version", "1"),
        "aipcs_registry_receipt_backend_check": ("storage_backend", "sqlite", "postgresql"),
        "aipcs_registry_receipt_revision_check": ("service_revision", "9223372036854775807"),
        "aipcs_registry_receipt_status_check": ("operational_status", "active", "suspended", "archived"),
        "aipcs_registry_receipt_verification_check": ("verification", "unverified", "verified"),
        "aipcs_registry_audit_action_check": ("action", "export", "import", "purge"),
        "aipcs_registry_audit_outcome_check": ("outcome", "completed", "recovery_required", "purge"),
    }
)
INDEX_PREDICATE_TOKENS = {
    "aipcs_registry_claim_active_lifecycle": (
        "operation_kind", "materialise", "evolve", "suspend", "resume", "archive",
        "restore", "export", "import", "purge", "phase", "prepared", "recovery_required",
    ),
    "aipcs_registry_identity_live_domain": ("identity_state", "live", "import_prepared"),
}
INDEX_PREDICATES = {
    "aipcs_registry_claim_active_lifecycle": (
        "((operation_kind = ANY (ARRAY['materialise'::text, 'evolve'::text, "
        "'suspend'::text, 'resume'::text, 'archive'::text, 'restore'::text, "
        "'export'::text, 'import'::text, 'purge'::text])) AND "
        "(phase = ANY (ARRAY['prepared'::text, 'recovery_required'::text])))"
    ),
    "aipcs_registry_identity_live_domain": (
        "(identity_state = ANY (ARRAY['live'::text, 'import_prepared'::text]))"
    ),
}

# Exact constraint text is evidence, not a hint.  The descriptor stores one
# expected expression per named CHECK; inspection hashes the complete named
# set.  It must never fall back to token-only acceptance because a rewrite can
# retain every expected token while changing what the database permits.
def _check_expressions(statements: tuple[str, ...]) -> dict[str, str]:
    """Extract each full named CHECK body from immutable migration DDL."""

    values: dict[str, str] = {}
    marker = re.compile(r'CONSTRAINT\s+"([^"]+)"\s+CHECK\s*\(', re.IGNORECASE)
    for statement in statements:
        for found in marker.finditer(statement):
            depth = 1
            position = found.end()
            start = position
            while position < len(statement) and depth:
                character = statement[position]
                if character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                position += 1
            if depth:
                raise RuntimeError("Invalid immutable registry CHECK descriptor")
            values[found.group(1)] = statement[start : position - 1]
    return values


CHECK_EXPRESSIONS = {
    name: expression
    for name, expression in _check_expressions(R1_DDL + R2_DDL).items()
    if name in CHECK_TOKENS
}
if set(CHECK_EXPRESSIONS) != set(CHECK_TOKENS):
    raise RuntimeError("Incomplete immutable registry CHECK descriptor")
# PostgreSQL 16 and 18 render this exact named CHECK set identically through
# ``pg_get_expr``.  This digest is intentionally server-rendered rather than a
# digest of the source DDL: PostgreSQL rewrites BETWEEN, IN, casts, and
# parentheses while storing constraint expressions.
CHECK_EXPRESSION_DIGEST = "d53c1e062fdedff5776701a651d6afe3814ce57234e7ed999b2deca6847c2e1f"
SEQUENCES = R1_SEQUENCES
