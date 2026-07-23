"""Private PostgreSQL R1 service-store foundation descriptors.

PostgreSQL R1 intentionally starts at the complete current record/history/
topology foundation.  It is not a translation of the SQLite R1/R2/R3 ledger.
The descriptor is consumed by structured catalogue inspection; it is never
rendered as public migration evidence.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

ADAPTER_ID = "aipcs.postgresql.service_store"
COMPONENT = "service_store"
TARGET_REVISION = 1
MIGRATION_ID = "postgres-service-store-0001-foundation"
RESERVED_PREFIX = "__aipcs_"

META = "__aipcs_service_store_meta"
MIGRATION = "__aipcs_service_store_migration"
MUTATION = "__aipcs_record_mutation"
HISTORY = "__aipcs_record_history"
BRANCH = "__aipcs_memory_branch"
RECORD_BRANCH = "__aipcs_record_branch"

TABLE_COLUMNS = {
    META: (
        ("singleton", "boolean", True, ""),
        ("adapter_id", "text", True, ""),
        ("component", "text", True, ""),
        ("namespace", "text", True, ""),
        ("applied_revision", "bigint", True, ""),
    ),
    MIGRATION: (
        ("component", "text", True, ""),
        ("revision", "bigint", True, ""),
        ("migration_id", "text", True, ""),
        ("checksum", "text", True, ""),
        ("applied_at", "timestamp with time zone", True, ""),
    ),
    MUTATION: (
        ("principal_id", "text", True, ""),
        ("idempotency_key", "text", True, ""),
        ("operation_kind", "text", True, ""),
        ("fingerprint", "text", True, ""),
        ("result_json", "jsonb", True, ""),
        ("completed_at", "timestamp with time zone", True, ""),
    ),
    HISTORY: (
        ("event_sequence", "bigint", True, "a"),
        ("principal_id", "text", True, ""),
        ("entity_name", "text", True, ""),
        ("record_id", "uuid", True, ""),
        ("record_version", "bigint", True, ""),
        ("event_kind", "text", True, ""),
        ("before_json", "jsonb", False, ""),
        ("after_json", "jsonb", False, ""),
        ("occurred_at", "timestamp with time zone", True, ""),
    ),
    BRANCH: (
        ("id", "uuid", True, ""),
        ("principal_id", "text", True, ""),
        ("slug", "text", True, ""),
        ("title", "text", True, ""),
        ("intent", "text", True, ""),
        ("branch_type", "text", False, ""),
        ("parent_branch_id", "uuid", False, ""),
        ("status", "text", True, ""),
        ("retrieval_summary", "text", False, ""),
        ("branch_revision", "bigint", True, ""),
        ("created_at", "timestamp with time zone", True, ""),
        ("updated_at", "timestamp with time zone", True, ""),
    ),
    RECORD_BRANCH: (
        ("principal_id", "text", True, ""),
        ("entity_name", "text", True, ""),
        ("record_id", "uuid", True, ""),
        ("branch_id", "uuid", True, ""),
        ("role", "text", True, ""),
        ("assigned_at", "timestamp with time zone", True, ""),
    ),
}

CONSTRAINTS = frozenset(
    {
        "__aipcs_service_store_meta_pkey",
        "__aipcs_service_store_meta_singleton",
        "__aipcs_service_store_meta_adapter",
        "__aipcs_service_store_meta_component",
        "__aipcs_service_store_meta_namespace",
        "__aipcs_service_store_meta_revision",
        "__aipcs_service_store_migration_pkey",
        "__aipcs_service_store_migration_id_unique",
        "__aipcs_service_store_migration_component",
        "__aipcs_service_store_migration_revision",
        "__aipcs_service_store_migration_checksum",
        "__aipcs_record_mutation_pkey",
        "__aipcs_record_mutation_principal",
        "__aipcs_record_mutation_key",
        "__aipcs_record_mutation_kind",
        "__aipcs_record_mutation_fingerprint",
        "__aipcs_record_mutation_result",
        "__aipcs_record_history_pkey",
        "__aipcs_record_history_unique_version",
        "__aipcs_record_history_principal",
        "__aipcs_record_history_entity",
        "__aipcs_record_history_version",
        "__aipcs_record_history_kind",
        "__aipcs_record_history_before",
        "__aipcs_record_history_after",
        "__aipcs_memory_branch_pkey",
        "__aipcs_memory_branch_principal_slug",
        "__aipcs_memory_branch_parent",
        "__aipcs_memory_branch_status",
        "__aipcs_memory_branch_principal",
        "__aipcs_memory_branch_slug",
        "__aipcs_memory_branch_title",
        "__aipcs_memory_branch_intent",
        "__aipcs_memory_branch_revision",
        "__aipcs_record_branch_pkey",
        "__aipcs_record_branch_branch",
        "__aipcs_record_branch_role",
        "__aipcs_record_branch_principal",
        "__aipcs_record_branch_entity",
    }
)

INDEXES = {
    "__aipcs_service_store_meta_pkey": (META, True, True, False),
    "__aipcs_service_store_migration_pkey": (MIGRATION, True, True, False),
    "__aipcs_service_store_migration_id_unique": (MIGRATION, True, False, False),
    "__aipcs_record_mutation_pkey": (MUTATION, True, True, False),
    "__aipcs_record_history_pkey": (HISTORY, True, True, False),
    "__aipcs_record_history_entity_sequence": (HISTORY, False, False, False),
    "__aipcs_record_history_record_version": (HISTORY, False, False, False),
    "__aipcs_record_history_unique_version": (HISTORY, True, False, False),
    "__aipcs_memory_branch_pkey": (BRANCH, True, True, False),
    "__aipcs_memory_branch_principal_slug": (BRANCH, True, False, False),
    "__aipcs_memory_branch_principal_status_updated": (BRANCH, False, False, False),
    "__aipcs_record_branch_pkey": (RECORD_BRANCH, True, True, False),
    "__aipcs_record_branch_primary": (RECORD_BRANCH, True, False, True),
    "__aipcs_record_branch_branch_lookup": (RECORD_BRANCH, False, False, False),
}

CONSTRAINT_KEYS = {
    "__aipcs_service_store_meta_pkey": (META, "p", ("singleton",), None, ()),
    "__aipcs_service_store_migration_pkey": (
        MIGRATION,
        "p",
        ("component", "revision"),
        None,
        (),
    ),
    "__aipcs_service_store_migration_id_unique": (
        MIGRATION,
        "u",
        ("component", "migration_id"),
        None,
        (),
    ),
    "__aipcs_record_mutation_pkey": (
        MUTATION,
        "p",
        ("principal_id", "idempotency_key"),
        None,
        (),
    ),
    "__aipcs_record_history_pkey": (HISTORY, "p", ("event_sequence",), None, ()),
    "__aipcs_record_history_unique_version": (
        HISTORY,
        "u",
        ("principal_id", "entity_name", "record_id", "record_version"),
        None,
        (),
    ),
    "__aipcs_memory_branch_pkey": (BRANCH, "p", ("id",), None, ()),
    "__aipcs_memory_branch_principal_slug": (
        BRANCH,
        "u",
        ("principal_id", "slug"),
        None,
        (),
    ),
    "__aipcs_memory_branch_parent": (
        BRANCH,
        "f",
        ("parent_branch_id",),
        BRANCH,
        ("id",),
    ),
    "__aipcs_record_branch_pkey": (
        RECORD_BRANCH,
        "p",
        ("principal_id", "entity_name", "record_id", "branch_id"),
        None,
        (),
    ),
    "__aipcs_record_branch_branch": (
        RECORD_BRANCH,
        "f",
        ("branch_id",),
        BRANCH,
        ("id",),
    ),
}

INDEX_COLUMNS = {
    "__aipcs_service_store_meta_pkey": ("singleton",),
    "__aipcs_service_store_migration_pkey": ("component", "revision"),
    "__aipcs_service_store_migration_id_unique": ("component", "migration_id"),
    "__aipcs_record_mutation_pkey": ("principal_id", "idempotency_key"),
    "__aipcs_record_history_pkey": ("event_sequence",),
    "__aipcs_record_history_entity_sequence": ("principal_id", "entity_name", "event_sequence"),
    "__aipcs_record_history_record_version": (
        "principal_id",
        "entity_name",
        "record_id",
        "record_version",
    ),
    "__aipcs_record_history_unique_version": (
        "principal_id",
        "entity_name",
        "record_id",
        "record_version",
    ),
    "__aipcs_memory_branch_pkey": ("id",),
    "__aipcs_memory_branch_principal_slug": ("principal_id", "slug"),
    "__aipcs_memory_branch_principal_status_updated": ("principal_id", "status", "updated_at"),
    "__aipcs_record_branch_pkey": ("principal_id", "entity_name", "record_id", "branch_id"),
    "__aipcs_record_branch_primary": ("principal_id", "entity_name", "record_id"),
    "__aipcs_record_branch_branch_lookup": (
        "principal_id",
        "branch_id",
        "entity_name",
        "role",
        "record_id",
    ),
}

CHECK_EXPRESSIONS = {
    "__aipcs_service_store_meta_singleton": "singleton",
    "__aipcs_service_store_meta_adapter": "(adapter_id = 'aipcs.postgresql.service_store'::text)",
    "__aipcs_service_store_meta_component": "(component = 'service_store'::text)",
    "__aipcs_service_store_meta_namespace": "(namespace ~ '^svc_[0-9a-f]{32}$'::text)",
    "__aipcs_service_store_meta_revision": "(applied_revision >= 0)",
    "__aipcs_service_store_migration_component": "(component = 'service_store'::text)",
    "__aipcs_service_store_migration_revision": "(revision > 0)",
    "__aipcs_service_store_migration_checksum": "(checksum ~ '^[0-9a-f]{64}$'::text)",
    "__aipcs_record_mutation_principal": "((length(principal_id) >= 1) AND (length(principal_id) <= 128))",
    "__aipcs_record_mutation_key": "((length(idempotency_key) >= 1) AND (length(idempotency_key) <= 128))",
    "__aipcs_record_mutation_kind": "((length(operation_kind) >= 1) AND (length(operation_kind) <= 64))",
    "__aipcs_record_mutation_fingerprint": "(fingerprint ~ '^[0-9a-f]{64}$'::text)",
    "__aipcs_record_mutation_result": "(octet_length((result_json)::text) <= 1048576)",
    "__aipcs_record_history_principal": "((length(principal_id) >= 1) AND (length(principal_id) <= 128))",
    "__aipcs_record_history_entity": "((length(entity_name) >= 1) AND (length(entity_name) <= 64))",
    "__aipcs_record_history_version": "(record_version > 0)",
    "__aipcs_record_history_kind": "(event_kind = ANY (ARRAY['create'::text, 'update'::text, 'delete'::text, 'primary_assign'::text, 'primary_move'::text, 'primary_unassign'::text, 'related_assign'::text, 'related_unassign'::text]))",
    "__aipcs_record_history_before": "((before_json IS NULL) OR (octet_length((before_json)::text) <= 65536))",
    "__aipcs_record_history_after": "((after_json IS NULL) OR (octet_length((after_json)::text) <= 65536))",
    "__aipcs_memory_branch_status": "(status = ANY (ARRAY['active'::text, 'archived'::text, 'superseded'::text]))",
    "__aipcs_memory_branch_principal": "((length(principal_id) >= 1) AND (length(principal_id) <= 128))",
    "__aipcs_memory_branch_slug": "(slug ~ '^[a-z][a-z0-9-]{1,62}[a-z0-9]$'::text)",
    "__aipcs_memory_branch_title": "((length(title) >= 1) AND (length(title) <= 256))",
    "__aipcs_memory_branch_intent": "((length(intent) >= 1) AND (length(intent) <= 1000))",
    "__aipcs_memory_branch_revision": "(branch_revision > 0)",
    "__aipcs_record_branch_role": "(role = ANY (ARRAY['primary'::text, 'related'::text]))",
    "__aipcs_record_branch_principal": "((length(principal_id) >= 1) AND (length(principal_id) <= 128))",
    "__aipcs_record_branch_entity": "((length(entity_name) >= 1) AND (length(entity_name) <= 64))",
}

_INDEX_TYPE_TO_OPCLASS = {
    "boolean": "bool_ops",
    "bigint": "int8_ops",
    "text": "text_ops",
    "timestamp with time zone": "timestamptz_ops",
    "uuid": "uuid_ops",
}
_COLUMN_TYPES = {
    table: {name: data_type for name, data_type, _not_null, _identity in columns}
    for table, columns in TABLE_COLUMNS.items()
}
COLUMN_COLLATIONS = {
    table: tuple("C" if data_type == "text" else None for _name, data_type, _not_null, _identity in columns)
    for table, columns in TABLE_COLUMNS.items()
}
INDEX_OPCLASSES = {
    name: tuple(_INDEX_TYPE_TO_OPCLASS[_COLUMN_TYPES[table][column]] for column in columns)
    for name, (table, _unique, _primary, _partial) in INDEXES.items()
    for columns in (INDEX_COLUMNS[name],)
}
INDEX_COLLATIONS = {
    name: tuple(
        "C" if _COLUMN_TYPES[table][column] == "text" else None for column in INDEX_COLUMNS[name]
    )
    for name, (table, _unique, _primary, _partial) in INDEXES.items()
}
INDEX_OPTIONS = {name: (0,) * len(columns) for name, columns in INDEX_COLUMNS.items()}
INDEX_PREDICATES = {
    name: "(role = 'primary'::text)" if name == "__aipcs_record_branch_primary" else None
    for name in INDEXES
}

SEQUENCES = frozenset({"__aipcs_record_history_event_sequence_seq"})


def quote_identifier(value: str) -> str:
    """Quote only static or already-validated private PostgreSQL identifiers."""

    return '"' + value.replace('"', '""') + '"'


def qualified(schema: str, name: str) -> str:
    return f"{quote_identifier(schema)}.{quote_identifier(name)}"


def ddl(schema: str) -> tuple[str, ...]:
    """Return the finite R1 DDL for one validated service namespace."""

    def q(name: str) -> str:
        return qualified(schema, name)
    return (
        f"""CREATE TABLE {q(META)} (
            singleton boolean CONSTRAINT {quote_identifier('__aipcs_service_store_meta_pkey')} PRIMARY KEY,
            adapter_id text COLLATE "C" NOT NULL,
            component text COLLATE "C" NOT NULL,
            namespace text COLLATE "C" NOT NULL,
            applied_revision bigint NOT NULL,
            CONSTRAINT {quote_identifier('__aipcs_service_store_meta_singleton')}
                CHECK (singleton),
            CONSTRAINT {quote_identifier('__aipcs_service_store_meta_adapter')}
                CHECK (adapter_id = '{ADAPTER_ID}'),
            CONSTRAINT {quote_identifier('__aipcs_service_store_meta_component')}
                CHECK (component = '{COMPONENT}'),
            CONSTRAINT {quote_identifier('__aipcs_service_store_meta_namespace')}
                CHECK (namespace ~ '^svc_[0-9a-f]{{32}}$'),
            CONSTRAINT {quote_identifier('__aipcs_service_store_meta_revision')}
                CHECK (applied_revision >= 0)
        )""",
        f"""CREATE TABLE {q(MIGRATION)} (
            component text COLLATE "C" NOT NULL,
            revision bigint NOT NULL,
            migration_id text COLLATE "C" NOT NULL,
            checksum text COLLATE "C" NOT NULL,
            applied_at timestamptz NOT NULL,
            CONSTRAINT {quote_identifier('__aipcs_service_store_migration_pkey')}
                PRIMARY KEY (component, revision),
            CONSTRAINT {quote_identifier('__aipcs_service_store_migration_id_unique')}
                UNIQUE (component, migration_id),
            CONSTRAINT {quote_identifier('__aipcs_service_store_migration_component')}
                CHECK (component = '{COMPONENT}'),
            CONSTRAINT {quote_identifier('__aipcs_service_store_migration_revision')}
                CHECK (revision > 0),
            CONSTRAINT {quote_identifier('__aipcs_service_store_migration_checksum')}
                CHECK (checksum ~ '^[0-9a-f]{{64}}$')
        )""",
        f"""CREATE TABLE {q(MUTATION)} (
            principal_id text COLLATE "C" NOT NULL,
            idempotency_key text COLLATE "C" NOT NULL,
            operation_kind text COLLATE "C" NOT NULL,
            fingerprint text COLLATE "C" NOT NULL,
            result_json jsonb NOT NULL,
            completed_at timestamptz NOT NULL,
            CONSTRAINT {quote_identifier('__aipcs_record_mutation_pkey')}
                PRIMARY KEY (principal_id, idempotency_key),
            CONSTRAINT {quote_identifier('__aipcs_record_mutation_principal')}
                CHECK (length(principal_id) BETWEEN 1 AND 128),
            CONSTRAINT {quote_identifier('__aipcs_record_mutation_key')}
                CHECK (length(idempotency_key) BETWEEN 1 AND 128),
            CONSTRAINT {quote_identifier('__aipcs_record_mutation_kind')}
                CHECK (length(operation_kind) BETWEEN 1 AND 64),
            CONSTRAINT {quote_identifier('__aipcs_record_mutation_fingerprint')}
                CHECK (fingerprint ~ '^[0-9a-f]{{64}}$'),
            CONSTRAINT {quote_identifier('__aipcs_record_mutation_result')}
                CHECK (octet_length(result_json::text) <= 1048576)
        )""",
        f"""CREATE TABLE {q(HISTORY)} (
            event_sequence bigint GENERATED ALWAYS AS IDENTITY
                CONSTRAINT {quote_identifier('__aipcs_record_history_pkey')} PRIMARY KEY,
            principal_id text COLLATE "C" NOT NULL,
            entity_name text COLLATE "C" NOT NULL,
            record_id uuid NOT NULL,
            record_version bigint NOT NULL,
            event_kind text COLLATE "C" NOT NULL,
            before_json jsonb,
            after_json jsonb,
            occurred_at timestamptz NOT NULL,
            CONSTRAINT {quote_identifier('__aipcs_record_history_unique_version')}
                UNIQUE (principal_id, entity_name, record_id, record_version),
            CONSTRAINT {quote_identifier('__aipcs_record_history_principal')}
                CHECK (length(principal_id) BETWEEN 1 AND 128),
            CONSTRAINT {quote_identifier('__aipcs_record_history_entity')}
                CHECK (length(entity_name) BETWEEN 1 AND 64),
            CONSTRAINT {quote_identifier('__aipcs_record_history_version')}
                CHECK (record_version > 0),
            CONSTRAINT {quote_identifier('__aipcs_record_history_kind')}
                CHECK (event_kind IN (
                    'create', 'update', 'delete', 'primary_assign', 'primary_move',
                    'primary_unassign', 'related_assign', 'related_unassign'
                )),
            CONSTRAINT {quote_identifier('__aipcs_record_history_before')}
                CHECK (before_json IS NULL OR octet_length(before_json::text) <= 65536),
            CONSTRAINT {quote_identifier('__aipcs_record_history_after')}
                CHECK (after_json IS NULL OR octet_length(after_json::text) <= 65536)
        )""",
        f"""CREATE TABLE {q(BRANCH)} (
            id uuid CONSTRAINT {quote_identifier('__aipcs_memory_branch_pkey')} PRIMARY KEY,
            principal_id text COLLATE "C" NOT NULL,
            slug text COLLATE "C" NOT NULL,
            title text COLLATE "C" NOT NULL,
            intent text COLLATE "C" NOT NULL,
            branch_type text COLLATE "C",
            parent_branch_id uuid,
            status text COLLATE "C" NOT NULL,
            retrieval_summary text COLLATE "C",
            branch_revision bigint NOT NULL,
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            CONSTRAINT {quote_identifier('__aipcs_memory_branch_principal_slug')}
                UNIQUE (principal_id, slug),
            CONSTRAINT {quote_identifier('__aipcs_memory_branch_parent')}
                FOREIGN KEY (parent_branch_id) REFERENCES {q(BRANCH)} (id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            CONSTRAINT {quote_identifier('__aipcs_memory_branch_status')}
                CHECK (status IN ('active', 'archived', 'superseded')),
            CONSTRAINT {quote_identifier('__aipcs_memory_branch_principal')}
                CHECK (length(principal_id) BETWEEN 1 AND 128),
            CONSTRAINT {quote_identifier('__aipcs_memory_branch_slug')}
                CHECK (slug ~ '^[a-z][a-z0-9-]{{1,62}}[a-z0-9]$'),
            CONSTRAINT {quote_identifier('__aipcs_memory_branch_title')}
                CHECK (length(title) BETWEEN 1 AND 256),
            CONSTRAINT {quote_identifier('__aipcs_memory_branch_intent')}
                CHECK (length(intent) BETWEEN 1 AND 1000),
            CONSTRAINT {quote_identifier('__aipcs_memory_branch_revision')}
                CHECK (branch_revision > 0)
        )""",
        f"""CREATE TABLE {q(RECORD_BRANCH)} (
            principal_id text COLLATE "C" NOT NULL,
            entity_name text COLLATE "C" NOT NULL,
            record_id uuid NOT NULL,
            branch_id uuid NOT NULL,
            role text COLLATE "C" NOT NULL,
            assigned_at timestamptz NOT NULL,
            CONSTRAINT {quote_identifier('__aipcs_record_branch_pkey')}
                PRIMARY KEY (principal_id, entity_name, record_id, branch_id),
            CONSTRAINT {quote_identifier('__aipcs_record_branch_branch')}
                FOREIGN KEY (branch_id) REFERENCES {q(BRANCH)} (id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            CONSTRAINT {quote_identifier('__aipcs_record_branch_role')}
                CHECK (role IN ('primary', 'related')),
            CONSTRAINT {quote_identifier('__aipcs_record_branch_principal')}
                CHECK (length(principal_id) BETWEEN 1 AND 128),
            CONSTRAINT {quote_identifier('__aipcs_record_branch_entity')}
                CHECK (length(entity_name) BETWEEN 1 AND 64)
        )""",
        f"""CREATE INDEX {quote_identifier('__aipcs_record_history_entity_sequence')}
            ON {q(HISTORY)} (principal_id COLLATE "C", entity_name COLLATE "C", event_sequence)""",
        f"""CREATE INDEX {quote_identifier('__aipcs_record_history_record_version')}
            ON {q(HISTORY)} (principal_id COLLATE "C", entity_name COLLATE "C", record_id, record_version)""",
        f"""CREATE INDEX {quote_identifier('__aipcs_memory_branch_principal_status_updated')}
            ON {q(BRANCH)} (principal_id COLLATE "C", status COLLATE "C", updated_at)""",
        f"""CREATE UNIQUE INDEX {quote_identifier('__aipcs_record_branch_primary')}
            ON {q(RECORD_BRANCH)} (principal_id COLLATE "C", entity_name COLLATE "C", record_id)
            WHERE role = 'primary'""",
        f"""CREATE INDEX {quote_identifier('__aipcs_record_branch_branch_lookup')}
            ON {q(RECORD_BRANCH)} (principal_id COLLATE "C", branch_id, entity_name COLLATE "C", role COLLATE "C", record_id)""",
    )


def _canonical_ddl() -> Iterable[str]:
    return (statement.replace('svc_00000000000000000000000000000001', '<namespace>') for statement in ddl('svc_00000000000000000000000000000001'))


CHECKSUM = hashlib.sha256("\n".join(_canonical_ddl()).encode("utf-8")).hexdigest()
