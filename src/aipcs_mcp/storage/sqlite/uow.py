"""SQLite transaction and registry repositories."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from functools import wraps
from uuid import UUID

from aipcs_mcp.application.models import AuditEvent, MutationClaim, Service
from aipcs_mcp.storage.errors import StorageMigrationError

from .codecs import (
    decode_result,
    decode_service,
    encode_result,
    encode_service_values,
    encode_time,
)
from .connection import set_query_only


def _bounded_uow[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return method(*args, **kwargs)
        except Exception:
            pass
        raise StorageMigrationError() from None

    return wrapped


class SQLiteRegistryUnitOfWork:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._closed = False
        self._terminal = False
        self.services = self
        self.mutations = self
        self.audits = self

    def _open(self) -> None:
        if self._closed or self._terminal:
            raise StorageMigrationError()

    def _write(self) -> None:
        self._open()
        if not self._connection.in_transaction:
            set_query_only(self._connection, False)
            self._connection.execute("BEGIN IMMEDIATE")

    @_bounded_uow
    def find_domain(self, principal_id: str, domain_name: str) -> Service | None:
        self._open()
        row = self._connection.execute(
            'SELECT * FROM "aipcs_registry_service" WHERE "principal_id"=? AND "domain_name"=?',
            (principal_id, domain_name),
        ).fetchone()
        return decode_service(row) if row else None

    @_bounded_uow
    def get(self, principal_id: str, service_id: UUID) -> Service | None:
        self._open()
        row = self._connection.execute(
            'SELECT * FROM "aipcs_registry_service" WHERE "principal_id"=? AND "service_id"=?',
            (principal_id, str(service_id)),
        ).fetchone()
        return decode_service(row) if row else None

    @_bounded_uow
    def list(self, principal_id: str, limit: int) -> list[Service]:
        self._open()
        rows = self._connection.execute(
            'SELECT * FROM "aipcs_registry_service" WHERE "principal_id"=? '
            'ORDER BY "created_at" ASC, "service_id" ASC LIMIT ?',
            (principal_id, limit),
        ).fetchall()
        return [decode_service(row) for row in rows]

    @_bounded_uow
    def add(self, service: Service) -> None:
        self._write()
        self._connection.execute(
            'INSERT INTO "aipcs_registry_service" VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            encode_service_values(service),
        )

    @_bounded_uow
    def save(self, service: Service) -> None:
        self._write()
        values = encode_service_values(service)
        cursor = self._connection.execute(
            'UPDATE "aipcs_registry_service" SET "domain_name"=?,"domain_class"=?,'
            '"intent_description"=?,"created_at"=?,"updated_at"=?,"last_activity_at"=?,'
            '"manifest_json"=?,"schema_version"=?,"design_state"=?,"operational_status"=? '
            'WHERE "service_id"=? AND "principal_id"=?',
            values[2:] + values[:2],
        )
        if cursor.rowcount != 1:
            raise ValueError

    @_bounded_uow
    def claim(self, principal_id: str, key: str, fingerprint: str) -> MutationClaim:
        self._write()
        row = self._connection.execute(
            'SELECT "fingerprint","service_id","result_json" FROM "aipcs_registry_mutation" '
            'WHERE "principal_id"=? AND "idempotency_key"=?',
            (principal_id, key),
        ).fetchone()
        if row is None:
            return MutationClaim("new")
        if row["fingerprint"] != fingerprint:
            return MutationClaim("conflict")
        return MutationClaim(
            "replay", decode_result(row["result_json"], principal_id, row["service_id"])
        )

    @_bounded_uow
    def complete(self, principal_id: str, key: str, fingerprint: str, result: Service) -> None:
        self._write()
        if result.principal_id != principal_id:
            raise ValueError
        self._connection.execute(
            'INSERT INTO "aipcs_registry_mutation" VALUES (?,?,?,?,?)',
            (principal_id, key, fingerprint, str(result.service_id), encode_result(result)),
        )

    @_bounded_uow
    def append(self, event: AuditEvent) -> None:
        self._write()
        self._connection.execute(
            'INSERT INTO "aipcs_registry_audit"('
            '"action","outcome","service_id","principal_id","created_via","at") '
            "VALUES (?,?,?,?,?,?)",
            (
                event.action,
                event.outcome,
                str(event.service_id),
                event.principal_id,
                event.created_via,
                encode_time(event.at),
            ),
        )

    @_bounded_uow
    def commit(self) -> None:
        self._open()
        self._connection.commit()
        self._terminal = True

    @_bounded_uow
    def rollback(self) -> None:
        if self._closed or self._terminal:
            return
        self._connection.rollback()
        self._terminal = True

    @_bounded_uow
    def close(self) -> None:
        if self._closed:
            return
        failed = False
        try:
            if not self._terminal and self._connection.in_transaction:
                self._connection.rollback()
        except Exception:
            failed = True
        try:
            self._connection.close()
        except Exception:
            failed = True
        finally:
            self._closed = True
            self._terminal = True
        if failed:
            raise StorageMigrationError()
