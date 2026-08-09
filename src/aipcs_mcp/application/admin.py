"""Backend-neutral, read-only administration inspection application."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from aipcs_mcp.contracts import ServiceMetadata
from aipcs_mcp.manifest_v2 import ManifestV2
from aipcs_mcp.records import DataFailure, MaintenanceQuery, MaintenanceResult

from .data import BootstrapResult, DataApplication
from .errors import NotFound
from .models import ApplicationContext, MaterialisationStorage
from .services import ServiceApplication

InspectionStatus = Literal[
    "uninitialised", "outdated", "ready", "incompatible", "dirty", "unavailable"
]
OverallStatus = Literal["ready", "attention_required", "unavailable"]
CheckStatus = Literal["pass", "warning", "fail"]

_ALL_MAINTENANCE_SCANS = (
    "expired",
    "stale",
    "low_confidence",
    "superseded",
    "missing_authority",
    "unbranched",
    "duplicate_authority",
    "blob_candidate",
)


@dataclass(frozen=True, slots=True)
class SafeMigrationState:
    component: Literal["registry", "service_store"]
    backend: Literal["sqlite", "postgresql"]
    status: InspectionStatus
    applied_revision: int | None
    target_revision: int | None
    issue_code: str | None


@dataclass(frozen=True, slots=True)
class MigrationObservation:
    component: Literal["registry", "service_store"]
    status: Literal["uninitialised", "outdated", "ready", "incompatible", "dirty"]
    applied_revision: int
    target_revision: int


class AdminStorageInspector(Protocol):
    def inspect_registry(self) -> MigrationObservation: ...

    def inspect_service(
        self, storage: MaterialisationStorage
    ) -> MigrationObservation: ...

    def inspect_domain(
        self, storage: MaterialisationStorage, manifest: ManifestV2
    ) -> Literal["unmaterialised", "ready", "incompatible"]: ...


class AdminInspectionUnavailable(Exception):
    """Bounded inspection failure with no adapter diagnostic detail."""

    def __init__(self, issue_code: str) -> None:
        if issue_code not in {"storage_busy", "storage_unavailable", "internal_error"}:
            raise ValueError("Inspection issue is invalid.")
        self.issue_code = issue_code
        super().__init__("Administration inspection is unavailable.")


@dataclass(frozen=True, slots=True)
class AdminStatusResult:
    profile: Literal["stateless", "sqlite", "postgresql"]
    backend: Literal["sqlite", "postgresql"] | None
    registry: SafeMigrationState | None
    design_counts: tuple[tuple[str, int], ...]
    operational_counts: tuple[tuple[str, int], ...]
    recovery_counts: tuple[tuple[str, int], ...]
    services_truncated: bool
    overall: OverallStatus


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    check_id: str
    status: CheckStatus
    issue_code: str | None = None


@dataclass(frozen=True, slots=True)
class DoctorResult:
    profile: Literal["stateless", "sqlite", "postgresql"]
    backend: Literal["sqlite", "postgresql"] | None
    service_id: UUID | None
    overall: CheckStatus
    checks: tuple[DoctorCheck, ...]


class AdminInspectionApplication:
    """Inspect one configured homogeneous runtime without migrating or repairing it."""

    def __init__(
        self,
        profile: Literal["stateless", "sqlite", "postgresql"],
        storage: AdminStorageInspector | None,
        services: ServiceApplication | None,
        data: DataApplication | None,
    ) -> None:
        if profile == "stateless":
            if any(value is not None for value in (storage, services, data)):
                raise ValueError("Stateless administration composition is invalid.")
        elif any(value is None for value in (storage, services, data)):
            raise ValueError("Persistent administration composition is incomplete.")
        self._profile = profile
        self._backend = None if profile == "stateless" else profile
        self._storage = storage
        self._services = services
        self._data = data

    def status(self, context: ApplicationContext) -> AdminStatusResult:
        _require_context(context)
        if self._profile == "stateless":
            return AdminStatusResult(
                "stateless", None, None, (), (), (), False, "unavailable"
            )
        registry = self.registry_status()
        if registry.status != "ready":
            overall: OverallStatus = (
                "unavailable" if registry.status == "unavailable" else "attention_required"
            )
            return AdminStatusResult(
                self._profile, self._backend, registry, (), (), (), False, overall
            )
        if self._data is None:
            raise ValueError("Persistent administration composition is incomplete.")
        topology = self._data.bootstrap(context)
        if type(topology) is DataFailure:
            return AdminStatusResult(
                self._profile,
                self._backend,
                registry,
                (),
                (),
                (),
                False,
                "unavailable",
            )
        if type(topology) is not BootstrapResult:
            raise ValueError("Administration topology result is invalid.")
        design = _counts(card.design_state for card in topology.services)
        operational = _counts(card.operational_status for card in topology.services)
        recovery = _counts(card.recovery_state for card in topology.services)
        overall = (
            "attention_required"
            if any(name != "clear" and count for name, count in recovery)
            else "ready"
        )
        return AdminStatusResult(
            self._profile,
            self._backend,
            registry,
            design,
            operational,
            recovery,
            topology.truncated,
            overall,
        )

    def registry_status(self) -> SafeMigrationState:
        if self._storage is None or self._backend is None:
            raise NotFound()
        return _inspect_migration(
            self._backend, "registry", self._storage.inspect_registry
        )

    def storage_status(
        self, context: ApplicationContext, service_id: UUID | None
    ) -> SafeMigrationState:
        _require_context(context)
        if service_id is None:
            return self.registry_status()
        service = self.service_inspect(context, service_id)
        if service.storage is None:
            if self._backend is None:
                raise ValueError("Persistent administration composition is incomplete.")
            return SafeMigrationState(
                "service_store",
                self._backend,
                "uninitialised",
                0,
                None,
                "service_not_materialised",
            )
        if service.storage.backend != self._backend:
            raise ValueError("Service storage backend is inconsistent.")
        storage = MaterialisationStorage(
            service.storage.backend, service.storage.namespace
        )
        if self._storage is None or self._backend is None:
            raise ValueError("Persistent administration composition is incomplete.")
        return _inspect_migration(
            self._backend,
            "service_store",
            lambda: self._storage.inspect_service(storage),
        )

    def service_list(
        self, context: ApplicationContext, limit: int
    ) -> list[ServiceMetadata]:
        _require_context(context)
        if self._services is None:
            raise NotFound()
        return self._services.list(context, limit)

    def service_inspect(
        self, context: ApplicationContext, service_id: UUID
    ) -> ServiceMetadata:
        _require_context(context)
        if self._services is None:
            raise NotFound()
        return self._services.inspect(context, service_id)

    def maintenance_scan(
        self, context: ApplicationContext, service_id: UUID
    ) -> MaintenanceResult | DataFailure:
        _require_context(context)
        if self._data is None:
            return DataFailure("storage_unavailable")
        return self._data.maintenance_scan(
            context,
            service_id,
            MaintenanceQuery(_ALL_MAINTENANCE_SCANS),
        )

    def doctor(
        self, context: ApplicationContext, service_id: UUID | None
    ) -> DoctorResult:
        _require_context(context)
        checks = [
            DoctorCheck("configuration", "pass"),
            DoctorCheck("runtime", "pass"),
        ]
        if self._profile == "stateless":
            checks.append(
                DoctorCheck("persistent_profile", "fail", "persistent_storage_not_configured")
            )
            return DoctorResult("stateless", None, service_id, "fail", tuple(checks))

        if self._profile == "postgresql":
            checks.append(DoctorCheck("credential_reference", "pass"))
        registry = self.registry_status()
        checks.append(_migration_check("registry_migration", registry))
        if registry.status != "ready" or service_id is None:
            return DoctorResult(
                self._profile,
                self._backend,
                service_id,
                _doctor_overall(checks),
                tuple(checks),
            )

        try:
            service = self.service_inspect(context, service_id)
        except NotFound:
            checks.append(DoctorCheck("service_identity", "fail", "service_not_found"))
            return DoctorResult(
                self._profile,
                self._backend,
                service_id,
                "fail",
                tuple(checks),
            )
        checks.append(DoctorCheck("service_identity", "pass"))
        if service.recovery_state == "clear":
            checks.append(DoctorCheck("admission_fence", "pass"))
        elif service.recovery_state == "pending":
            checks.append(DoctorCheck("admission_fence", "warning", "operation_in_progress"))
        else:
            checks.append(DoctorCheck("admission_fence", "fail", "recovery_required"))

        store = self.storage_status(context, service_id)
        checks.append(_migration_check("service_store_migration", store))
        if service.storage is None or service.schema_ is None or store.status != "ready":
            checks.append(
                DoctorCheck("domain_schema", "warning", "domain_schema_not_inspectable")
            )
        else:
            try:
                storage = MaterialisationStorage(
                    service.storage.backend, service.storage.namespace
                )
                if self._storage is None:
                    raise ValueError("Persistent administration composition is incomplete.")
                domain_status = self._storage.inspect_domain(storage, service.schema_)
                status: CheckStatus = "pass" if domain_status == "ready" else "fail"
                issue = None if status == "pass" else f"domain_schema_{domain_status}"
                checks.append(DoctorCheck("domain_schema", status, issue))
            except AdminInspectionUnavailable as error:
                check_status: CheckStatus = (
                    "warning" if error.issue_code == "storage_busy" else "fail"
                )
                checks.append(
                    DoctorCheck("domain_schema", check_status, error.issue_code)
                )
            except Exception:
                checks.append(DoctorCheck("domain_schema", "fail", "internal_error"))
        return DoctorResult(
            self._profile,
            self._backend,
            service_id,
            _doctor_overall(checks),
            tuple(checks),
        )


def project_status(value: AdminStatusResult) -> dict[str, object]:
    return {
        "profile": value.profile,
        "backend": value.backend,
        "registry": None if value.registry is None else project_migration(value.registry),
        "services": {
            "design": dict(value.design_counts),
            "operational": dict(value.operational_counts),
            "recovery": dict(value.recovery_counts),
            "truncated": value.services_truncated,
        },
        "overall": value.overall,
    }


def project_migration(value: SafeMigrationState) -> dict[str, object]:
    return {
        "component": value.component,
        "backend": value.backend,
        "status": value.status,
        "applied_revision": value.applied_revision,
        "target_revision": value.target_revision,
        "issue_code": value.issue_code,
    }


def project_doctor(value: DoctorResult) -> dict[str, object]:
    return {
        "profile": value.profile,
        "backend": value.backend,
        "service_id": None if value.service_id is None else str(value.service_id),
        "overall": value.overall,
        "checks": [
            {
                "check_id": check.check_id,
                "status": check.status,
                "issue_code": check.issue_code,
            }
            for check in value.checks
        ],
    }


def _inspect_migration(
    backend: Literal["sqlite", "postgresql"],
    component: Literal["registry", "service_store"],
    inspect: object,
) -> SafeMigrationState:
    if not callable(inspect):
        raise ValueError("Migration inspector is invalid.")
    try:
        state = inspect()
    except AdminInspectionUnavailable as error:
        return SafeMigrationState(
            component, backend, "unavailable", None, None, error.issue_code
        )
    except Exception:
        return SafeMigrationState(component, backend, "unavailable", None, None, "internal_error")
    if type(state) is not MigrationObservation or state.component != component:
        raise ValueError("Migration inspection result is invalid.")
    issue = {
        "uninitialised": "storage_uninitialised",
        "outdated": "storage_migration_required",
        "ready": None,
        "incompatible": "storage_incompatible",
        "dirty": "storage_dirty",
    }[state.status]
    return SafeMigrationState(
        component,
        backend,
        state.status,
        state.applied_revision,
        state.target_revision,
        issue,
    )


def _migration_check(check_id: str, state: SafeMigrationState) -> DoctorCheck:
    if state.status == "ready":
        return DoctorCheck(check_id, "pass")
    status: CheckStatus = (
        "warning"
        if state.issue_code
        in {"storage_busy", "storage_migration_required", "service_not_materialised"}
        else "fail"
    )
    return DoctorCheck(check_id, status, state.issue_code)


def _doctor_overall(checks: list[DoctorCheck]) -> CheckStatus:
    if any(check.status == "fail" for check in checks):
        return "fail"
    if any(check.status == "warning" for check in checks):
        return "warning"
    return "pass"


def _counts(values: object) -> tuple[tuple[str, int], ...]:
    if not hasattr(values, "__iter__"):
        raise ValueError("Count source is invalid.")
    counts: dict[str, int] = {}
    for value in values:  # type: ignore[union-attr]
        if type(value) is not str:
            raise ValueError("Count value is invalid.")
        counts[value] = counts.get(value, 0) + 1
    return tuple(sorted(counts.items()))


def _require_context(context: ApplicationContext) -> None:
    if (
        type(context) is not ApplicationContext
        or type(context.principal_id) is not str
        or not context.principal_id
        or type(context.created_via) is not str
        or context.created_via != "cli"
    ):
        raise ValueError("Administration context is invalid.")
