"""Private SQLite registry adapter and migration state machine."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from functools import wraps

from aipcs_mcp.application.models import Service
from aipcs_mcp.application.operational_lifecycle import OperationalLifecyclePhase
from aipcs_mcp.application.ports import RegistryUnitOfWork
from aipcs_mcp.application.registry_authority import (
    PortableIntent,
    PortableOperationKind,
    TransferReceipt,
)
from aipcs_mcp.storage.codecs import (
    decode_r1_service,
    decode_service,
    decode_time,
    encode_time,
    validate_audit_row,
    validate_r1_audit_row,
    validate_r1_mutation_row,
    validate_r2_mutation_row,
)
from aipcs_mcp.storage.contracts import MigrationState, StorageAdapterInfo
from aipcs_mcp.storage.errors import StorageBusy, StorageMigrationError, StorageUnavailable
from aipcs_mcp.storage.registry_authority_codecs import (
    decode_registry_authority_intent,
    validate_registry_authority_claim_row,
    validate_transfer_receipt_row,
)

from .connection import (
    SQLiteConnectionPolicy,
    SQLiteConnectionPurpose,
    connect,
    require_supported_sqlite_runtime,
)
from .location import AnchoredLocation, SQLiteLocationPolicy, create_database
from .migrations import (
    R1,
    R2,
    R3,
    R3_POLICY,
    R4,
    TARGET_REVISION,
)
from .result_codes import is_sqlite_busy
from .uow import SQLiteRegistryUnitOfWork
from .wal_policy import (
    WAL_POLICY_ID,
    WALPolicyEvidence,
    WALPolicyState,
    classify_wal_policy,
    journal_mode_matches,
    policy_row_matches,
    policy_table_matches,
)


def _bounded_adapter[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return method(*args, **kwargs)
        except StorageBusy:
            failure: Exception = StorageBusy()
        except StorageUnavailable:
            failure = StorageUnavailable()
        except StorageMigrationError:
            failure = StorageMigrationError()
        except Exception as error:
            failure = StorageBusy() if is_sqlite_busy(error) else StorageMigrationError()
        raise failure from None

    return wrapped


class SQLiteRegistryAdapter:
    def __init__(self, location: SQLiteLocationPolicy, *, busy_timeout_ms: int = 5_000) -> None:
        require_supported_sqlite_runtime()
        if not isinstance(location, SQLiteLocationPolicy):
            raise StorageUnavailable()
        self._location = location
        self._connection_policy = SQLiteConnectionPolicy(busy_timeout_ms)

    def __repr__(self) -> str:
        return "SQLiteRegistryAdapter(<redacted>)"

    def info(self) -> StorageAdapterInfo:
        return StorageAdapterInfo("sqlite", frozenset({"registry"}))

    @_bounded_adapter
    def inspect_migration(self) -> MigrationState:
        anchored: AnchoredLocation | None = None
        connection: sqlite3.Connection | None = None
        try:
            anchored = self._location.acquire(create_root=False, allow_journal=False)
            if anchored is None or anchored._database_stat is None:
                return _state(0, "uninitialised")
            if anchored._database_stat.st_size == 0:
                return _state(0, "uninitialised")
            connection = connect(
                anchored,
                "ro",
                query_only=True,
                policy=self._connection_policy,
                purpose=SQLiteConnectionPurpose.POLICY_INSPECTION,
            )
            connection.execute("BEGIN")
            try:
                return _inspect_connection(connection, integrity=True)
            finally:
                connection.rollback()
        finally:
            _close(connection, anchored, allow_journal=False)

    @_bounded_adapter
    def migrate(self) -> MigrationState:
        anchored: AnchoredLocation | None = None
        connection: sqlite3.Connection | None = None
        try:
            anchored = self._acquire_for_migration()
            connection = self._migration_connection(anchored)
            connection.execute("BEGIN IMMEDIATE")
            action = _prepare_under_writer_lock(connection)
            if action == "ready_r4":
                connection.rollback()
            elif action == "prepare":
                _prepare_r3(connection)
                _require_policy_state(connection, WALPolicyState.PREPARED_DELETE)
                connection.commit()
            elif action == "fresh":
                _create_r1(connection)
                _upgrade_r1_to_r2(connection)
                _prepare_r3(connection)
                _require_policy_state(connection, WALPolicyState.PREPARED_DELETE)
                connection.commit()
            elif action == "legacy_r1":
                _upgrade_r1_to_r2(connection)
                _prepare_r3(connection)
                _require_policy_state(connection, WALPolicyState.PREPARED_DELETE)
                connection.commit()
            elif action in {"prepared_delete", "prepared_wal"}:
                connection.rollback()
            elif action == "upgrade_r4":
                _upgrade_r3_to_r4(connection)
                connection.commit()
            elif action == "incompatible":
                connection.rollback()
                return _inspect_connection(connection, integrity=True)
            else:
                raise StorageMigrationError()
            _close(connection, anchored, allow_journal=False)
            connection = None
            anchored = None

            if action in {"fresh", "legacy_r1", "prepare", "prepared_delete"}:
                self._switch_prepared_delete_to_wal()
            if action not in {"ready_r4", "upgrade_r4"}:
                self._finalise_prepared_wal()
                self._upgrade_ready_r3_to_r4()
            return self._checkpoint_and_observe_ready()
        finally:
            _close(connection, anchored, allow_journal=True)

    @_bounded_adapter
    def open_uow(self) -> RegistryUnitOfWork:
        anchored: AnchoredLocation | None = None
        connection: sqlite3.Connection | None = None
        try:
            anchored = self._location.acquire(create_root=False, allow_journal=False)
            if anchored is None or anchored._database_stat is None:
                raise StorageUnavailable()
            connection = connect(
                anchored,
                "rw",
                query_only=True,
                policy=self._connection_policy,
                purpose=SQLiteConnectionPurpose.READY_UNIT_OF_WORK,
            )
            connection.execute("BEGIN")
            try:
                if _inspect_connection(connection, integrity=False).status != "ready":
                    raise StorageMigrationError()
            finally:
                connection.rollback()
            result = SQLiteRegistryUnitOfWork(connection, anchored)
            connection = None
            anchored = None
            return result
        finally:
            _close(connection, anchored)

    def _acquire_for_migration(self) -> AnchoredLocation:
        anchored = self._location.acquire(create_root=True, allow_journal=True)
        if anchored is None:
            raise StorageUnavailable()
        if anchored.journal_present:
            if anchored._database_stat is None:
                raise StorageUnavailable()
            connection = self._migration_connection(anchored)
            connection.execute("SELECT 1").fetchone()
            _close(connection, anchored, allow_journal=True)
            anchored = self._location.acquire(create_root=True, allow_journal=False)
            if anchored is None:
                raise StorageUnavailable()
        if anchored._database_stat is None:
            anchored._database_stat = create_database(anchored._root_fd)
        return anchored

    def _migration_connection(self, anchored: AnchoredLocation) -> sqlite3.Connection:
        return connect(
            anchored,
            "rw",
            query_only=False,
            policy=self._connection_policy,
            purpose=SQLiteConnectionPurpose.MIGRATION,
        )

    def _switch_prepared_delete_to_wal(self) -> None:
        anchored = self._location.acquire(create_root=True, allow_journal=False)
        connection: sqlite3.Connection | None = None
        try:
            if anchored is None or anchored._database_stat is None:
                raise StorageUnavailable()
            connection = self._migration_connection(anchored)
            _require_policy_state(connection, WALPolicyState.PREPARED_DELETE)
            row = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            if row is None or row[0] != "wal":
                raise StorageMigrationError()
            anchored.verify_live_database_identity()
            anchored.record_connection_header_mode("wal")
            _require_policy_state(connection, WALPolicyState.PREPARED_WAL)
            anchored.adopt_live_sidecars(allow_journal=False)
            anchored.verify_live_sidecars(allow_journal=False)
        finally:
            _close(connection, anchored, allow_journal=False)

    def _finalise_prepared_wal(self) -> None:
        anchored = self._location.acquire(create_root=True, allow_journal=False)
        connection: sqlite3.Connection | None = None
        try:
            if anchored is None or anchored._database_stat is None:
                raise StorageUnavailable()
            connection = self._migration_connection(anchored)
            connection.execute("BEGIN IMMEDIATE")
            state = _wal_state(connection)
            if state is WALPolicyState.READY_WAL:
                connection.rollback()
                return
            if state is not WALPolicyState.PREPARED_WAL:
                raise StorageMigrationError()
            connection.execute(
                f'UPDATE "{R3_POLICY.table_name}" SET "phase"=\'ready\' WHERE "singleton"=1'
            )
            connection.execute(
                'INSERT INTO "aipcs_registry_migration" VALUES (?,?,?,?,?)',
                ("registry", R3.revision, R3.migration_id, R3.checksum, encode_time(datetime.now(UTC))),
            )
            connection.execute(
                'UPDATE "aipcs_registry_meta" SET "applied_revision"=?,"dirty"=0 '
                'WHERE "singleton"=1',
                (R3.revision,),
            )
            _require_policy_state(connection, WALPolicyState.READY_WAL)
            connection.commit()
        finally:
            _close(connection, anchored)

    def _checkpoint_and_observe_ready(self) -> MigrationState:
        anchored = self._location.acquire(create_root=False, allow_journal=False)
        connection: sqlite3.Connection | None = None
        try:
            if anchored is None or anchored._database_stat is None:
                raise StorageUnavailable()
            connection = self._migration_connection(anchored)
            if not _r4_ready(connection, integrity=True):
                raise StorageMigrationError()
            _checkpoint_passive(connection)
        finally:
            _close(connection, anchored)
        state = self.inspect_migration()
        if state.status != "ready":
            raise StorageMigrationError()
        return state

    def _upgrade_ready_r3_to_r4(self) -> None:
        anchored = self._location.acquire(create_root=False, allow_journal=False)
        connection: sqlite3.Connection | None = None
        try:
            if anchored is None or anchored._database_stat is None:
                raise StorageUnavailable()
            connection = self._migration_connection(anchored)
            connection.execute("BEGIN IMMEDIATE")
            if _wal_state(connection) is not WALPolicyState.READY_WAL:
                raise StorageMigrationError()
            _upgrade_r3_to_r4(connection)
            connection.commit()
        finally:
            _close(connection, anchored)


def _create_r1(connection: sqlite3.Connection) -> None:
    for statement in R1.ddl:
        connection.execute(statement)
    connection.execute(
        'INSERT INTO "aipcs_registry_meta" VALUES (1,?,?,?,?)',
        ("aipcs.sqlite.registry", "registry", 1, 1),
    )
    connection.execute(
        'INSERT INTO "aipcs_registry_migration" VALUES (?,?,?,?,?)',
        ("registry", R1.revision, R1.migration_id, R1.checksum, encode_time(datetime.now(UTC))),
    )


def _upgrade_r1_to_r2(connection: sqlite3.Connection) -> None:
    connection.execute('UPDATE "aipcs_registry_meta" SET "dirty"=1 WHERE "singleton"=1')
    connection.execute('ALTER TABLE "aipcs_registry_service" RENAME TO "aipcs_registry_service_r1"')
    connection.execute('ALTER TABLE "aipcs_registry_mutation" RENAME TO "aipcs_registry_mutation_r1"')
    connection.execute('ALTER TABLE "aipcs_registry_audit" RENAME TO "aipcs_registry_audit_r1"')
    connection.execute('DROP INDEX "aipcs_registry_service_list"')
    for statement in R2.ddl[:3]:
        connection.execute(statement)
    connection.execute(
        'INSERT INTO "aipcs_registry_service"('
        '"service_id","principal_id","domain_name","domain_class","intent_description",'
        '"created_at","updated_at","last_activity_at","manifest_json","schema_version",'
        '"design_state","operational_status","service_revision","materialised_at",'
        '"storage_backend","storage_namespace") '
        'SELECT "service_id","principal_id","domain_name","domain_class","intent_description",'
        '"created_at","updated_at","last_activity_at","manifest_json","schema_version",'
        '"design_state","operational_status",1,NULL,NULL,NULL '
        'FROM "aipcs_registry_service_r1"'
    )
    connection.execute(
        'INSERT INTO "aipcs_registry_mutation"('
        '"principal_id","idempotency_key","fingerprint","service_id","operation_kind","phase",'
        '"created_via","expected_service_revision","expected_schema_version","target_manifest_json",'
        '"result_json","recovery_category") '
        'SELECT "principal_id","idempotency_key","fingerprint","service_id",\'legacy\',\'completed\','
        'NULL,NULL,NULL,NULL,"result_json",NULL FROM "aipcs_registry_mutation_r1"'
    )
    connection.execute(
        'INSERT INTO "aipcs_registry_audit"('
        '"audit_id","action","outcome","service_id","principal_id","created_via","at") '
        'SELECT "audit_id","action","outcome","service_id","principal_id","created_via","at" '
        'FROM "aipcs_registry_audit_r1"'
    )
    connection.execute('DROP TABLE "aipcs_registry_mutation_r1"')
    connection.execute('DROP TABLE "aipcs_registry_audit_r1"')
    connection.execute('DROP TABLE "aipcs_registry_service_r1"')
    for statement in R2.ddl[3:]:
        connection.execute(statement)
    connection.execute(
        'DELETE FROM "aipcs_registry_audit" AS "old" '
        'WHERE (SELECT count(*) FROM "aipcs_registry_audit" AS "newer" '
        'WHERE "newer"."principal_id"="old"."principal_id" '
        'AND "newer"."audit_id">"old"."audit_id") >= 1000'
    )
    connection.execute(
        'INSERT INTO "aipcs_registry_migration" VALUES (?,?,?,?,?)',
        ("registry", R2.revision, R2.migration_id, R2.checksum, encode_time(datetime.now(UTC))),
    )
    connection.execute(
        'UPDATE "aipcs_registry_meta" SET "applied_revision"=2 WHERE "singleton"=1'
    )
    objects = _objects(connection)
    if not _layout_matches(connection, objects, R2, integrity=True):
        raise StorageMigrationError()
    connection.execute('UPDATE "aipcs_registry_meta" SET "dirty"=0 WHERE "singleton"=1')


def _is_exact_upgradeable_r1(connection: sqlite3.Connection) -> bool:
    try:
        objects = _objects(connection)
        meta = _meta(connection, objects)
        if meta != (R1.revision, 0) or not _history_prefix(connection, objects, R1.revision):
            return False
        if not _layout_matches(connection, objects, R1, integrity=True):
            return False
        for row in connection.execute('SELECT * FROM "aipcs_registry_service"'):
            decode_r1_service(row)
        for row in connection.execute('SELECT * FROM "aipcs_registry_mutation"'):
            validate_r1_mutation_row(row)
        for row in connection.execute('SELECT * FROM "aipcs_registry_audit"'):
            validate_r1_audit_row(row)
    except Exception as error:
        if is_sqlite_busy(error):
            raise
        return False
    return True


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _r3_intent_json(row: sqlite3.Row) -> str | None:
    operation_kind = row["operation_kind"]
    if operation_kind in {"legacy", "seed", "design"}:
        return None
    if operation_kind not in {"materialise", "evolve"}:
        raise StorageMigrationError()
    try:
        target_manifest = json.loads(row["target_manifest_json"])
        value = {
            "created_via": row["created_via"],
            "expected_schema_version": row["expected_schema_version"],
            "expected_service_revision": row["expected_service_revision"],
            "kind": operation_kind,
            "principal": row["principal_id"],
            "service_id": row["service_id"],
            "target_manifest": target_manifest,
        }
        encoded = _canonical_json(value)
    except Exception:
        raise StorageMigrationError() from None
    return encoded


def _upgrade_r3_to_r4(connection: sqlite3.Connection) -> None:
    """Atomically replace the R3 live-service ledger with purge-safe R4 evidence."""

    if _wal_state(connection) is not WALPolicyState.READY_WAL:
        raise StorageMigrationError()
    connection.execute('UPDATE "aipcs_registry_meta" SET "dirty"=1 WHERE "singleton"=1')
    connection.execute(
        'ALTER TABLE "aipcs_registry_mutation" RENAME TO "aipcs_registry_mutation_r3"'
    )
    connection.execute('ALTER TABLE "aipcs_registry_audit" RENAME TO "aipcs_registry_audit_r3"')
    connection.execute('DROP INDEX "aipcs_registry_mutation_active_lifecycle"')
    connection.execute('DROP INDEX "aipcs_registry_audit_principal"')
    for statement in R4.ddl[:4]:
        connection.execute(statement)

    old_claim_count = connection.execute(
        'SELECT count(*) FROM "aipcs_registry_mutation_r3"'
    ).fetchone()[0]
    for row in connection.execute(
        'SELECT * FROM "aipcs_registry_mutation_r3" '
        'ORDER BY "principal_id","idempotency_key"'
    ).fetchall():
        validate_r2_mutation_row(row)
        connection.execute(
            'INSERT INTO "aipcs_registry_claim"('
            '"principal_id","idempotency_key","fingerprint","service_id",'
            '"operation_kind","phase","created_via","intent_json","result_json",'
            '"recovery_category") VALUES (?,?,?,?,?,?,?,?,?,?)',
            (
                row["principal_id"],
                row["idempotency_key"],
                row["fingerprint"],
                row["service_id"],
                row["operation_kind"],
                row["phase"],
                row["created_via"],
                _r3_intent_json(row),
                row["result_json"],
                row["recovery_category"],
            ),
        )
    if connection.execute('SELECT count(*) FROM "aipcs_registry_claim"').fetchone()[0] != (
        old_claim_count
    ):
        raise StorageMigrationError()

    connection.execute(
        'INSERT INTO "aipcs_registry_identity"('
        '"service_id","principal_id","identity_state","domain_name","storage_backend",'
        '"storage_namespace","claim_idempotency_key","lifecycle_at") '
        'SELECT "service_id","principal_id",\'live\',"domain_name","storage_backend",'
        '\'svc_\' || replace("service_id",\'-\',\'\'),NULL,NULL '
        'FROM "aipcs_registry_service"'
    )
    connection.execute(
        'INSERT INTO "aipcs_registry_audit"('
        '"audit_id","action","outcome","service_id","principal_id","created_via","at") '
        'SELECT "audit_id","action","outcome","service_id","principal_id","created_via","at" '
        'FROM "aipcs_registry_audit_r3"'
    )
    connection.execute('DROP TABLE "aipcs_registry_mutation_r3"')
    connection.execute('DROP TABLE "aipcs_registry_audit_r3"')
    for statement in R4.ddl[4:]:
        connection.execute(statement)
    connection.execute(
        'DELETE FROM "aipcs_registry_audit" AS "old" '
        'WHERE (SELECT count(*) FROM "aipcs_registry_audit" AS "newer" '
        'WHERE "newer"."principal_id"="old"."principal_id" '
        'AND "newer"."audit_id">"old"."audit_id") >= 1000'
    )
    connection.execute(
        'INSERT INTO "aipcs_registry_migration" VALUES (?,?,?,?,?)',
        ("registry", R4.revision, R4.migration_id, R4.checksum, encode_time(datetime.now(UTC))),
    )
    connection.execute(
        'UPDATE "aipcs_registry_meta" SET "applied_revision"=? WHERE "singleton"=1',
        (R4.revision,),
    )
    objects = _objects(connection)
    if not _layout_matches(connection, objects, R4, integrity=True):
        raise StorageMigrationError()
    connection.execute('UPDATE "aipcs_registry_meta" SET "dirty"=0 WHERE "singleton"=1')
    if not _r4_ready(connection, integrity=True):
        raise StorageMigrationError()


def _objects(connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = connection.execute(
        "SELECT type,name,sql FROM sqlite_schema WHERE substr(name,1,7) <> 'sqlite_'"
    ).fetchall()
    return {row["name"]: row for row in rows}


def _inspect_connection(connection: sqlite3.Connection, *, integrity: bool) -> MigrationState:
    by_name = _objects(connection)
    if not by_name:
        return _state(0, "uninitialised")
    meta = _meta(connection, by_name)
    if meta is None:
        return _state(0, "incompatible")
    revision, dirty = meta
    if revision > TARGET_REVISION:
        return _state(revision, "incompatible")
    if revision == 0:
        expected_partial = all(
            row["sql"] == R1.expected_sql[name]
            for name, row in by_name.items()
            if name in R1.expected_sql
        )
        if (
            dirty == 1
            and _history_prefix(connection, by_name, revision)
            and not (set(by_name) - set(R1.expected_sql))
            and expected_partial
        ):
            return _state(revision, "dirty")
        return _state(revision, "incompatible")
    descriptor = (
        R1
        if revision == 1
        else R2
        if revision == 2
        else R3
        if revision == 3
        else R4
        if revision == 4
        else None
    )
    if descriptor is None:
        return _state(revision, "incompatible")
    if revision == 1:
        history_ok = _history_prefix(connection, by_name, revision)
        layout_ok = _layout_matches(connection, by_name, descriptor, integrity=integrity)
        if dirty == 1 and history_ok and layout_ok:
            return _state(revision, "dirty")
        return _state(revision, "incompatible")
    if revision == R4.revision:
        if dirty == 0 and _r4_ready(
            connection, by_name=by_name, integrity=integrity
        ):
            return _state(R4.revision, "ready")
        return _state(revision, "incompatible")
    state = _wal_state(connection, by_name=by_name, integrity=integrity)
    if state is WALPolicyState.READY_WAL:
        return _state(R3.revision, "outdated")
    if state in {WALPolicyState.PREPARED_DELETE, WALPolicyState.PREPARED_WAL}:
        return _state(R2.revision, "dirty")
    return _state(revision, "incompatible")


def _prepare_under_writer_lock(connection: sqlite3.Connection) -> str:
    """Classify again after BEGIN IMMEDIATE; never act on a stale observation."""

    by_name = _objects(connection)
    if not by_name:
        return "fresh"
    if _r4_ready(connection, by_name=by_name, integrity=True):
        return "ready_r4"
    meta = _meta(connection, by_name)
    if meta == (R1.revision, 0) and _is_exact_upgradeable_r1(connection):
        return "legacy_r1"
    state = _wal_state(connection, by_name=by_name, integrity=True)
    if state is WALPolicyState.CLEAN_PREDECESSOR:
        return "prepare"
    if state is WALPolicyState.PREPARED_DELETE:
        return "prepared_delete"
    if state is WALPolicyState.PREPARED_WAL:
        return "prepared_wal"
    if state is WALPolicyState.READY_WAL:
        return "upgrade_r4"
    return "incompatible"


def _prepare_r3(connection: sqlite3.Connection) -> None:
    connection.execute(R3_POLICY.ddl)
    connection.execute(
        f'INSERT INTO "{R3_POLICY.table_name}" VALUES (?,?,?,?)',
        (1, WAL_POLICY_ID, R3_POLICY.checksum, "prepared"),
    )
    connection.execute('UPDATE "aipcs_registry_meta" SET "dirty"=1 WHERE "singleton"=1')


def _require_policy_state(connection: sqlite3.Connection, expected: WALPolicyState) -> None:
    if _wal_state(connection) is not expected:
        raise StorageMigrationError()


def _wal_state(
    connection: sqlite3.Connection,
    *,
    by_name: dict[str, sqlite3.Row] | None = None,
    integrity: bool = True,
) -> WALPolicyState:
    try:
        by_name = _objects(connection) if by_name is None else by_name
        meta = _meta(connection, by_name)
        if meta is None:
            return WALPolicyState.INCOMPATIBLE
        revision, dirty = meta
        policy_object = by_name.get(R3_POLICY.table_name)
        rows: tuple[tuple[object, ...], ...] = ()
        if policy_object is not None:
            rows = tuple(
                tuple(row)
                for row in connection.execute(
                    f'SELECT singleton,policy_id,policy_checksum,phase '
                    f'FROM "{R3_POLICY.table_name}"'
                )
            )
        descriptor = R3 if policy_object is not None else R2
        evidence = WALPolicyEvidence(
            revision=revision,
            dirty=dirty,
            journal_mode=connection.execute("PRAGMA journal_mode").fetchone()[0],
            layout_matches=_layout_matches(connection, by_name, descriptor, integrity=integrity),
            history_matches=_history_prefix(connection, by_name, revision),
            policy_table_sql=None if policy_object is None else policy_object["sql"],
            policy_rows=rows,
        )
        return classify_wal_policy(
            evidence,
            predecessor_revision=R2.revision,
            target_revision=R3.revision,
            descriptor=R3_POLICY,
        )
    except Exception as error:
        if is_sqlite_busy(error):
            raise
        return WALPolicyState.INCOMPATIBLE


def _r4_ready(
    connection: sqlite3.Connection,
    *,
    by_name: dict[str, sqlite3.Row] | None = None,
    integrity: bool = True,
) -> bool:
    """Validate logical R4 without reinterpreting the immutable R3 WAL epoch."""

    try:
        by_name = _objects(connection) if by_name is None else by_name
        if _meta(connection, by_name) != (R4.revision, 0):
            return False
        policy_object = by_name.get(R3_POLICY.table_name)
        if policy_object is None or not policy_table_matches(policy_object["sql"], R3_POLICY):
            return False
        policy_rows = tuple(
            tuple(row)
            for row in connection.execute(
                f'SELECT singleton,policy_id,policy_checksum,phase '
                f'FROM "{R3_POLICY.table_name}"'
            )
        )
        return (
            journal_mode_matches(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            and policy_row_matches(policy_rows, R3_POLICY, "ready")
            and _history_prefix(connection, by_name, R4.revision)
            and _layout_matches(connection, by_name, R4, integrity=integrity)
        )
    except Exception as error:
        if is_sqlite_busy(error):
            raise
        return False


def _checkpoint_passive(connection: sqlite3.Connection) -> None:
    row = connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    if row is None or len(row) != 3 or any(type(value) is not int for value in row):
        raise StorageMigrationError()
    busy, log_frames, checkpointed_frames = row
    if busy == 1:
        raise StorageBusy()
    if busy != 0:
        raise StorageMigrationError()
    if (log_frames, checkpointed_frames) == (-1, -1):
        return
    if (
        log_frames < 0
        or checkpointed_frames < 0
        or checkpointed_frames > log_frames
    ):
        raise StorageMigrationError()


def _meta(
    connection: sqlite3.Connection, objects: dict[str, sqlite3.Row]
) -> tuple[int, int] | None:
    if "aipcs_registry_meta" not in objects:
        return None
    try:
        rows = connection.execute(
            'SELECT singleton,adapter_id,component,applied_revision,dirty FROM "aipcs_registry_meta"'
        ).fetchall()
        if len(rows) != 1 or tuple(rows[0][:3]) != (1, "aipcs.sqlite.registry", "registry"):
            return None
        revision, dirty = rows[0][3], rows[0][4]
        if type(revision) is not int or revision < 0 or dirty not in (0, 1):
            return None
        return revision, dirty
    except Exception as error:
        if is_sqlite_busy(error):
            raise
        return None


def _history_prefix(
    connection: sqlite3.Connection,
    objects: dict[str, sqlite3.Row],
    revision: int,
) -> bool:
    try:
        if revision == 0:
            return (
                "aipcs_registry_migration" not in objects
                or not connection.execute(
                    'SELECT 1 FROM "aipcs_registry_migration" LIMIT 1'
                ).fetchone()
            )
        if "aipcs_registry_migration" not in objects:
            return False
        rows = connection.execute(
            "SELECT component,revision,migration_id,checksum,applied_at "
            'FROM "aipcs_registry_migration" ORDER BY revision'
        ).fetchall()
        descriptors = (R1, R2, R3, R4)
        if revision > len(descriptors) or len(rows) != revision:
            return False
        for row, descriptor in zip(rows, descriptors[:revision], strict=True):
            if tuple(row[:4]) != (
                "registry",
                descriptor.revision,
                descriptor.migration_id,
                descriptor.checksum,
            ):
                return False
            decode_time(row[4])
    except StorageMigrationError:
        return False
    except sqlite3.OperationalError as error:
        if str(error).startswith(("no such column:", "no such table:")):
            return False
        raise
    return True


def _descriptor_maps(descriptor):
    return (
        descriptor.expected_sql,
        descriptor.table_xinfo,
        descriptor.foreign_keys,
        descriptor.index_list,
        descriptor.index_xinfo,
    )


def _layout_matches(
    connection: sqlite3.Connection,
    by_name: dict[str, sqlite3.Row],
    descriptor,
    *,
    integrity: bool,
) -> bool:
    expected_sql, _, _, _, _ = _descriptor_maps(descriptor)
    if set(by_name) != set(expected_sql):
        return False
    if any(row["sql"] != expected_sql[name] for name, row in by_name.items()):
        return False
    if not _signatures(connection, descriptor):
        return False
    if integrity:
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            return False
        _quick_check(connection)
        if descriptor is R2 or descriptor is R3:
            if not _r2_data_matches(connection):
                return False
        elif descriptor is R4:
            if not _r4_data_matches(connection):
                return False
    return True


def _r4_data_matches(connection: sqlite3.Connection) -> bool:
    try:
        services: dict[tuple[str, str], Service] = {}
        for row in connection.execute('SELECT * FROM "aipcs_registry_service"'):
            service = decode_service(row)
            services[(service.principal_id, str(service.service_id))] = service

        identities: dict[tuple[str, str], sqlite3.Row] = {}
        for row in connection.execute('SELECT * FROM "aipcs_registry_identity"'):
            service_id = row["service_id"]
            principal_id = row["principal_id"]
            if (
                type(service_id) is not str
                or type(principal_id) is not str
                or row["storage_namespace"] != f"svc_{service_id.replace('-', '')}"
            ):
                return False
            identities[(principal_id, service_id)] = row
            if row["lifecycle_at"] is not None:
                decode_time(row["lifecycle_at"])
            claim_key = row["claim_idempotency_key"]
            if row["identity_state"] != "live":
                claim = connection.execute(
                    'SELECT operation_kind,phase FROM "aipcs_registry_claim" '
                    'WHERE principal_id=? AND idempotency_key=? AND service_id=?',
                    (principal_id, claim_key, service_id),
                ).fetchone()
                if claim is None:
                    return False
                if row["identity_state"] == "import_prepared" and not (
                    claim["operation_kind"] == "import"
                    and claim["phase"] in {"prepared", "recovery_required"}
                ):
                    return False
                if row["identity_state"] == "tombstoned" and tuple(claim) != (
                    "purge",
                    "completed",
                ):
                    return False

        for key, service in services.items():
            identity = identities.get(key)
            if (
                identity is None
                or identity["identity_state"] != "live"
                or identity["domain_name"] != service.domain_name
                or identity["storage_backend"]
                != (None if service.storage is None else service.storage.backend)
            ):
                return False
        if any(
            row["identity_state"] == "live" and key not in services
            for key, row in identities.items()
        ):
            return False

        authority_claims: dict[tuple[str, str, str], tuple[object, object]] = {}
        for row in connection.execute('SELECT * FROM "aipcs_registry_claim"'):
            operation_kind = row["operation_kind"]
            phase = row["phase"]
            if phase == "tombstoned":
                if operation_kind in {"suspend", "resume", "archive", "restore", "export", "import", "purge"}:
                    validate_registry_authority_claim_row(dict(row))
                continue
            if operation_kind in {"legacy", "seed", "design"}:
                _, completion = validate_r2_mutation_row(
                    {
                        "principal_id": row["principal_id"],
                        "idempotency_key": row["idempotency_key"],
                        "fingerprint": row["fingerprint"],
                        "service_id": row["service_id"],
                        "operation_kind": operation_kind,
                        "phase": phase,
                        "created_via": row["created_via"],
                        "expected_service_revision": None,
                        "expected_schema_version": None,
                        "target_manifest_json": None,
                        "result_json": row["result_json"],
                        "recovery_category": row["recovery_category"],
                    }
                )
                if completion is None:
                    return False
                current = services.get((row["principal_id"], row["service_id"]))
                if current is None or _immutable_service_identity(completion) != (
                    _immutable_service_identity(current)
                ):
                    return False
            elif operation_kind in {"materialise", "evolve"}:
                intent_json = _canonical_json(json.loads(row["intent_json"]))
                if intent_json != row["intent_json"]:
                    return False
                intent = json.loads(intent_json)
                if (
                    set(intent)
                    != {
                        "created_via",
                        "expected_schema_version",
                        "expected_service_revision",
                        "kind",
                        "principal",
                        "service_id",
                        "target_manifest",
                    }
                    or intent["kind"] != operation_kind
                    or intent["principal"] != row["principal_id"]
                    or intent["service_id"] != row["service_id"]
                    or intent["created_via"] != row["created_via"]
                ):
                    return False
                legacy_row = {
                    "principal_id": row["principal_id"],
                    "idempotency_key": row["idempotency_key"],
                    "fingerprint": row["fingerprint"],
                    "service_id": row["service_id"],
                    "operation_kind": operation_kind,
                    "phase": phase,
                    "created_via": row["created_via"],
                    "expected_service_revision": intent["expected_service_revision"],
                    "expected_schema_version": intent["expected_schema_version"],
                    "target_manifest_json": _canonical_json(intent["target_manifest"]),
                    "result_json": row["result_json"],
                    "recovery_category": row["recovery_category"],
                }
                lifecycle_intent, completion = validate_r2_mutation_row(legacy_row)
                if lifecycle_intent is None:
                    return False
                current = services.get((row["principal_id"], row["service_id"]))
                if (
                    current is None
                    or current.schema_version is None
                    or lifecycle_intent.expected_service_revision > current.service_revision
                    or lifecycle_intent.expected_schema_version > current.schema_version
                ):
                    return False
                if completion is not None:
                    if _immutable_service_identity(completion) != _immutable_service_identity(current):
                        return False
                    if (
                        completion.service_revision > current.service_revision
                        or completion.updated_at > current.updated_at
                        or completion.schema_version is None
                        or completion.schema_version > current.schema_version
                        or (
                            completion.service_revision == current.service_revision
                            and completion != current
                        )
                        or (completion.materialised_at, completion.storage)
                        != (current.materialised_at, current.storage)
                    ):
                        return False
            else:
                authority_value = validate_registry_authority_claim_row(dict(row))
                authority_intent = decode_registry_authority_intent(row["intent_json"])
                intent_json = _canonical_json(json.loads(row["intent_json"]))
                if intent_json != row["intent_json"]:
                    return False
                intent = json.loads(intent_json)
                if (
                    type(intent) is not dict
                    or intent.get("kind") != operation_kind
                    or intent.get("principal_id") != row["principal_id"]
                    or intent.get("service_id") != row["service_id"]
                    or intent.get("created_via") != row["created_via"]
                ):
                    return False
                authority_claims[(
                    row["principal_id"], row["idempotency_key"], row["service_id"]
                )] = (authority_intent, authority_value)

        for row in connection.execute('SELECT * FROM "aipcs_registry_receipt"'):
            receipt = validate_transfer_receipt_row(dict(row))
            pairing = authority_claims.get((
                receipt.principal_id, row["idempotency_key"], str(receipt.service_id)
            ))
            if pairing is None or not _receipt_matches_authority_completion(
                receipt, pairing, services
            ):
                return False

        allowed_audits = {
            ("seed", "created"),
            ("seed", "duplicate"),
            ("design", "accepted"),
        } | {
            (action, outcome)
            for action in {
                "materialise",
                "evolve",
                "suspend",
                "resume",
                "archive",
                "restore",
                "export",
                "import",
                "purge",
            }
            for outcome in {"completed", "recovery_required"}
        }
        for row in connection.execute('SELECT * FROM "aipcs_registry_audit"'):
            if (
                type(row["audit_id"]) is not int
                or row["audit_id"] < 1
                or (row["action"], row["outcome"]) not in allowed_audits
                or type(row["service_id"]) is not str
                or len(row["service_id"]) != 36
                or type(row["principal_id"]) is not str
                or not 1 <= len(row["principal_id"]) <= 128
                or type(row["created_via"]) is not str
                or not 1 <= len(row["created_via"]) <= 64
            ):
                return False
            decode_time(row["at"])
        return connection.execute(
            'SELECT 1 FROM "aipcs_registry_audit" '
            'GROUP BY "principal_id" HAVING count(*) > 1000 LIMIT 1'
        ).fetchone() is None
    except Exception as error:
        if is_sqlite_busy(error):
            raise
        return False


def _receipt_matches_authority_completion(
    receipt: TransferReceipt,
    pairing: tuple[object, object],
    services: dict[tuple[str, str], Service],
) -> bool:
    """Bind a flattened receipt to the exact completed claim evidence."""

    intent, completion = pairing
    if type(intent) is not PortableIntent or intent.phase is not OperationalLifecyclePhase.COMPLETED:
        return False
    if intent.kind is PortableOperationKind.EXPORT:
        return receipt == completion
    if intent.kind is not PortableOperationKind.IMPORT or type(completion) is not Service:
        return False
    registered = services.get((receipt.principal_id, str(receipt.service_id)))
    return (
        receipt.kind.value == "import"
        and receipt.verification.value == "verified"
        and intent.bundle_root_sha256 == receipt.bundle_root_sha256
        and intent.destination_backend is receipt.storage_backend
        and completion == registered
        and completion.principal_id == receipt.principal_id
        and completion.service_id == receipt.service_id
        and completion.service_revision == receipt.service_revision
        and completion.operational_status == receipt.operational_status
        and (
            completion.storage is None
            or completion.storage.backend == receipt.storage_backend.value
        )
    )


def _r2_data_matches(connection: sqlite3.Connection) -> bool:
    try:
        services: dict[tuple[str, str], Service] = {}
        for row in connection.execute('SELECT * FROM "aipcs_registry_service"'):
            service = decode_service(row)
            services[(service.principal_id, str(service.service_id))] = service
        for row in connection.execute('SELECT * FROM "aipcs_registry_mutation"'):
            intent, completion = validate_r2_mutation_row(row)
            if intent is not None:
                current = services.get((intent.principal_id, str(intent.service_id)))
                if (
                    current is None
                    or current.schema_version is None
                    or intent.expected_service_revision > current.service_revision
                    or intent.expected_schema_version > current.schema_version
                ):
                    return False
            if completion is not None:
                current = services.get((completion.principal_id, str(completion.service_id)))
                if current is None or _immutable_service_identity(completion) != (
                    _immutable_service_identity(current)
                ):
                    return False
                if (
                    completion.service_revision > current.service_revision
                    or completion.updated_at > current.updated_at
                    or (
                        completion.schema_version is not None
                        and (
                            current.schema_version is None
                            or completion.schema_version > current.schema_version
                        )
                    )
                ):
                    return False
                if (
                    row["operation_kind"] != "legacy"
                    and completion.service_revision == current.service_revision
                    and completion != current
                ):
                    return False
                if row["operation_kind"] in {"materialise", "evolve"} and (
                    completion.materialised_at,
                    completion.storage,
                ) != (
                    current.materialised_at,
                    current.storage,
                ):
                    return False
        for row in connection.execute('SELECT * FROM "aipcs_registry_audit"'):
            validate_audit_row(row)
        if connection.execute(
            'SELECT 1 FROM "aipcs_registry_audit" '
            'GROUP BY "principal_id" HAVING count(*) > 1000 LIMIT 1'
        ).fetchone() is not None:
            return False
    except Exception as error:
        if is_sqlite_busy(error):
            raise
        return False
    return True


def _immutable_service_identity(service: Service) -> tuple[object, ...]:
    return (
        service.principal_id,
        service.service_id,
        service.domain_name,
        service.domain_class,
        service.intent_description,
        service.created_at,
    )


def _signatures(connection: sqlite3.Connection, descriptor=R2) -> bool:
    try:
        _, table_xinfo, foreign_keys, index_list, index_xinfo = _descriptor_maps(descriptor)
        for table, expected in table_xinfo.items():
            rows = connection.execute(f'PRAGMA table_xinfo("{table}")')
            if tuple(map(tuple, rows)) != expected:
                return False
        for table, expected in foreign_keys.items():
            rows = connection.execute(f'PRAGMA foreign_key_list("{table}")')
            if tuple(map(tuple, rows)) != expected:
                return False
        for table, expected in index_list.items():
            rows = connection.execute(f'PRAGMA index_list("{table}")')
            actual = tuple(sorted((row[1], row[2], row[3], row[4]) for row in rows))
            if actual != expected:
                return False
        for index, expected in index_xinfo.items():
            rows = connection.execute(f'PRAGMA index_xinfo("{index}")')
            if tuple(map(tuple, rows)) != expected:
                return False
        return True
    except Exception as error:
        if is_sqlite_busy(error):
            raise
        return False


def _quick_check(connection: sqlite3.Connection) -> None:
    # R4 retains up to 1,000 audit rows per principal and carries additional
    # claim/identity indexes.  Keep inspection bounded while allowing that
    # documented maximum to complete on supported SQLite builds.
    budget = 1_000_000

    def progress() -> int:
        nonlocal budget
        budget -= 1_000
        return int(budget <= 0)

    try:
        connection.set_progress_handler(progress, 1_000)
        row = connection.execute("PRAGMA quick_check(1)").fetchone()
        if row is None or row[0] != "ok":
            raise ValueError
    except Exception as error:
        if is_sqlite_busy(error):
            raise StorageBusy() from None
        failure = StorageMigrationError()
    else:
        return
    finally:
        with suppress(Exception):
            connection.set_progress_handler(None, 0)
    raise failure from None


def _state(revision: int, status: str) -> MigrationState:
    return MigrationState("registry", revision, TARGET_REVISION, status)  # type: ignore[arg-type]


def _close(
    connection: sqlite3.Connection | None,
    location: AnchoredLocation | None,
    *,
    allow_journal: bool = False,
) -> None:
    """Release a live SQLite handle only after its anchored state is rechecked."""

    failure: BaseException | None = None
    if location is not None and location._database_stat is not None and connection is not None:
        try:
            location.verify_live_database_identity()
            if allow_journal:
                location.validate_sidecars(allow_journal=True)
            else:
                location.verify_live_sidecars(allow_journal=False)
        except Exception as error:
            failure = error
    if connection is not None:
        try:
            connection.close()
        except Exception as error:
            if failure is None:
                failure = error
    if location is not None:
        try:
            if location._database_stat is not None:
                location.verify_database_identity()
                location.verify_closed_sidecars(allow_journal=False)
        except Exception as error:
            # Metadata/identity violations dominate a concurrent SQLite error.
            failure = error
        finally:
            try:
                location.close()
            except Exception as error:
                if failure is None:
                    failure = error
    if failure is not None:
        if is_sqlite_busy(failure):
            raise StorageBusy() from None
        raise StorageUnavailable() from None
