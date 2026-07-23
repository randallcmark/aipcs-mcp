"""SYNTHETIC_FIXTURE. SQLite binding for the backend-neutral domain parity contract."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from storage_contracts.domain_conformance import assert_domain_materialisation_and_evolution

from aipcs_mcp.relational import RelationalSpecification, RelationalTransition
from aipcs_mcp.storage import DomainSchemaState
from aipcs_mcp.storage.sqlite import SQLiteLocationPolicy, SQLiteServiceStoreCatalog
from aipcs_mcp.storage.sqlite.domain_schema import SQLiteDomainSchemaStore


class SQLiteDomainParityHarness:
    """SQLite-only assembly; generic assertions remain in ``storage_contracts``."""

    def __init__(self, root: Path) -> None:
        location = SQLiteLocationPolicy(root)
        self._locator = SQLiteServiceStoreCatalog(location).allocate(UUID(int=1))
        assert SQLiteServiceStoreCatalog(location).migrate(self._locator).status == "ready"
        self._store = SQLiteDomainSchemaStore(location)

    def materialise(self, specification: RelationalSpecification) -> DomainSchemaState:
        return self._store.materialise(self._locator, specification)

    def inspect(self, specification: RelationalSpecification) -> DomainSchemaState:
        return self._store.inspect(self._locator, specification)

    def evolve(self, transition: RelationalTransition) -> DomainSchemaState:
        return self._store.evolve(self._locator, transition)


def test_sqlite_satisfies_domain_materialisation_and_evolution_parity(tmp_path: Path) -> None:
    assert_domain_materialisation_and_evolution(lambda: SQLiteDomainParityHarness(tmp_path / "domain"))
