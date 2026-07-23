"""Private SQLite registry adapter and migration state machine."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from functools import wraps

from aipcs_mcp.application.models import Service
from aipcs_mcp.application.ports import RegistryUnitOfWork
from aipcs_mcp.storage.contracts import MigrationState, StorageAdapterInfo
from aipcs_mcp.storage.errors import StorageMigrationError, StorageUnavailable

from .codecs import (
    decode_r1_service,
    decode_service,
    decode_time,
    encode_time,
    validate_audit_row,
    validate_r1_audit_row,
    validate_r1_mutation_row,
    validate_r2_mutation_row,
)
from .connection import connect
from .location import AnchoredLocation, SQLiteLocationPolicy, create_database
from .migrations import (
    EXPECTED_SQL,
    FOREIGN_KEYS,
    INDEX_LIST,
    INDEX_XINFO,
    R1,
    R2,
    TABLE_XINFO,
    TARGET_REVISION,
)
from .uow import SQLiteRegistryUnitOfWork


def _bounded_adapter[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return method(*args, **kwargs)
        except StorageUnavailable:
            failure = StorageUnavailable()
        except StorageMigrationError:
            failure = StorageMigrationError()
        except Exception:
            failure = StorageMigrationError()
        raise failure from None

    return wrapped


class SQLiteRegistryAdapter:
    def __init__(self, location: SQLiteLocationPolicy) -> None:
        if not isinstance(location, SQLiteLocationPolicy):
            raise StorageUnavailable()
        self._location = location

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
            connection = connect(anchored, "ro", query_only=True)
            return _inspect_connection(connection, integrity=True)
        finally:
            _close(connection, anchored)

    @_bounded_adapter
    def migrate(self) -> MigrationState:
        anchored: AnchoredLocation | None = None
        connection: sqlite3.Connection | None = None
        try:
            anchored = self._location.acquire(create_root=True, allow_journal=True)
            if anchored is None:
                raise StorageUnavailable()
            if anchored.journal_present:
                if anchored._database_stat is None:
                    raise StorageUnavailable()
                connection = connect(anchored, "rw", query_only=False)
                connection.execute("SELECT 1").fetchone()
                connection.close()
                connection = None
                anchored.close()
                anchored = None
                anchored = self._location.acquire(create_root=True, allow_journal=False)
                if anchored is None:
                    raise StorageUnavailable()
            upgrade_r1 = False
            if anchored._database_stat is None:
                anchored._database_stat = create_database(anchored._root_fd)
            elif anchored._database_stat.st_size:
                connection = connect(anchored, "rw", query_only=False)
                state = _inspect_connection(connection, integrity=True)
                upgrade_r1 = _is_exact_upgradeable_r1(connection)
                connection.close()
                connection = None
                if state.status == "ready":
                    return state
                if not upgrade_r1:
                    return state
            connection = connect(anchored, "rw", query_only=False)
            connection.execute("BEGIN EXCLUSIVE")
            if upgrade_r1:
                if not _is_exact_upgradeable_r1(connection):
                    raise StorageMigrationError()
            else:
                _create_r1(connection)
            _upgrade_r1_to_r2(connection)
            if _inspect_connection(connection, integrity=True).status != "ready":
                raise StorageMigrationError()
            connection.commit()
            connection.close()
            connection = None
            anchored.verify_database_identity()
            anchored.close()
            anchored = None
            return self.inspect_migration()
        finally:
            _close(connection, anchored)

    @_bounded_adapter
    def open_uow(self) -> RegistryUnitOfWork:
        anchored: AnchoredLocation | None = None
        connection: sqlite3.Connection | None = None
        try:
            anchored = self._location.acquire(create_root=False, allow_journal=False)
            if anchored is None or anchored._database_stat is None:
                raise StorageUnavailable()
            connection = connect(anchored, "rw", query_only=True)
            if _inspect_connection(connection, integrity=False).status != "ready":
                raise StorageMigrationError()
            anchored.close()
            anchored = None
            result = SQLiteRegistryUnitOfWork(connection)
            connection = None
            return result
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
    except Exception:
        return False
    return True


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
    descriptor = R1 if revision == 1 else R2 if revision == 2 else None
    if descriptor is None:
        return _state(revision, "incompatible")
    history_ok = _history_prefix(connection, by_name, revision)
    layout_ok = _layout_matches(connection, by_name, descriptor, integrity=integrity)
    if dirty == 1 and history_ok and layout_ok:
        return _state(revision, "dirty")
    if dirty != 0 or revision != TARGET_REVISION or not history_ok:
        return _state(revision, "incompatible")
    return _state(revision, "ready" if layout_ok else "incompatible")


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
    except Exception:
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
        descriptors = (R1, R2)
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
    if descriptor is R2:
        return EXPECTED_SQL, TABLE_XINFO, FOREIGN_KEYS, INDEX_LIST, INDEX_XINFO
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
        if descriptor is R2 and not _r2_data_matches(connection):
            return False
    return True


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
    except Exception:
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
    except Exception:
        return False


def _quick_check(connection: sqlite3.Connection) -> None:
    budget = 100_000

    def progress() -> int:
        nonlocal budget
        budget -= 1_000
        return int(budget <= 0)

    try:
        connection.set_progress_handler(progress, 1_000)
        row = connection.execute("PRAGMA quick_check(1)").fetchone()
        if row is None or row[0] != "ok":
            raise ValueError
    except Exception:
        failure = StorageMigrationError()
    else:
        return
    finally:
        with suppress(Exception):
            connection.set_progress_handler(None, 0)
    raise failure from None


def _state(revision: int, status: str) -> MigrationState:
    return MigrationState("registry", revision, TARGET_REVISION, status)  # type: ignore[arg-type]


def _close(connection: sqlite3.Connection | None, location: AnchoredLocation | None) -> None:
    if connection is not None:
        with suppress(Exception):
            connection.close()
    if location is not None:
        with suppress(Exception):
            location.close()
