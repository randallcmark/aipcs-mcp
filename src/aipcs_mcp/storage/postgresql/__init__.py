"""Private PostgreSQL storage primitives safe to import during composition.

Importing this package never imports a PostgreSQL driver or reads a DSN
environment variable.  Runtime composition retains the sole secret-resolution
boundary.
"""

from .connection import (
    PostgreSQLConnectionPolicy,
    PostgreSQLDsn,
    connect_postgresql,
    resolve_postgresql_dsn_for_serve,
)
from .data_store import PostgreSQLMaterialisedDataStore
from .domain_schema import PostgreSQLDomainSchemaStore
from .portable import PostgreSQLPortableServiceStore
from .registry import PostgreSQLRegistryAdapter
from .service_store import PostgreSQLServiceStoreCatalog

__all__ = [
    "PostgreSQLConnectionPolicy",
    "PostgreSQLDsn",
    "connect_postgresql",
    "resolve_postgresql_dsn_for_serve",
    "PostgreSQLDomainSchemaStore",
    "PostgreSQLMaterialisedDataStore",
    "PostgreSQLPortableServiceStore",
    "PostgreSQLRegistryAdapter",
    "PostgreSQLServiceStoreCatalog",
]
