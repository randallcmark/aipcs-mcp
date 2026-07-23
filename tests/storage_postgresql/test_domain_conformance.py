"""SYNTHETIC_FIXTURE. Managed-fixture PostgreSQL binding for domain parity."""

from __future__ import annotations

from uuid import UUID

from storage_contracts.domain_conformance import assert_domain_materialisation_and_evolution

from aipcs_mcp.relational import RelationalSpecification, RelationalTransition
from aipcs_mcp.storage import DomainSchemaState
from aipcs_mcp.storage.postgresql.connection import PostgreSQLDsn
from aipcs_mcp.storage.postgresql.domain_schema import PostgreSQLDomainSchemaStore
from aipcs_mcp.storage.postgresql.service_store import PostgreSQLServiceStoreCatalog

from .container import PostgresTestTarget


class PostgreSQLDomainParityHarness:
    """Production adapter assembly using only the project-managed disposable target."""

    def __init__(self, target: PostgresTestTarget) -> None:
        dsn = PostgreSQLDsn(target.dsn)
        self._catalog = PostgreSQLServiceStoreCatalog(dsn)
        self._locator = self._catalog.allocate(UUID(int=1))
        assert self._catalog.migrate(self._locator).status == "ready"
        self._store = PostgreSQLDomainSchemaStore(dsn, service_store_catalog=self._catalog)

    def materialise(self, specification: RelationalSpecification) -> DomainSchemaState:
        return self._store.materialise(self._locator, specification)

    def inspect(self, specification: RelationalSpecification) -> DomainSchemaState:
        return self._store.inspect(self._locator, specification)

    def evolve(self, transition: RelationalTransition) -> DomainSchemaState:
        return self._store.evolve(self._locator, transition)


def test_postgresql_satisfies_domain_materialisation_and_evolution_parity(
    postgres_test_target: PostgresTestTarget,
) -> None:
    assert_domain_materialisation_and_evolution(
        lambda: PostgreSQLDomainParityHarness(postgres_test_target)
    )
