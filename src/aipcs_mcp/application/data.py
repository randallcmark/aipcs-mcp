"""Backend-neutral admission for materialised record and discovery operations.

The registry remains the source of truth for whether a principal may use a
service.  This facade takes one detached, principal-scoped registry snapshot,
then releases its unit of work before handing the logical service-store
identity and compiled record specification to the data adapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from aipcs_mcp.records import (
    AssignBranchRecordsCommand,
    BranchAssignmentOutcome,
    BranchMutationOutcome,
    BranchPage,
    CreateBranchCommand,
    CreateRecordCommand,
    DataFailure,
    DeleteRecordCommand,
    DeleteRecordOutcome,
    GetRecordQuery,
    HistoryPage,
    ListBranchesQuery,
    ListRecordsQuery,
    MaintenanceQuery,
    MaintenanceResult,
    PageRequest,
    RecordHistoryQuery,
    RecordMutationOutcome,
    RecordPage,
    RecordSpecification,
    RecordValue,
    SearchRecordsQuery,
    ServiceSummary,
    SummaryQuery,
    UpdateBranchCommand,
    UpdateRecordCommand,
    compile_record_specification,
)

from .models import ApplicationContext, MaterialisationStorage, Service
from .ports import AdmittedDataStore, DataOperation, RegistryUnitOfWork, UowFactory

_MAX_PRINCIPAL_LENGTH = 128
_MAX_CREATED_VIA_LENGTH = 64
_MAX_BOOTSTRAP_SERVICES = 100


@dataclass(frozen=True, slots=True)
class BootstrapService:
    """A value-free, registry-only service card for initial orientation."""

    service_id: UUID
    domain_name: str
    domain_class: str
    intent_description: str
    design_state: str
    operational_status: str
    schema_version: int | None
    recovery_state: str
    entity_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """Bounded registry topology that deliberately contains no record values."""

    services: tuple[BootstrapService, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class SummaryView:
    """One detached summary paired with the manifest facts needed to project it."""

    summary: ServiceSummary
    specification: RecordSpecification


@dataclass(frozen=True, slots=True)
class _Admission:
    storage: MaterialisationStorage
    principal_id: str
    specification: RecordSpecification


class DataApplication:
    """Admit pure record operations without exposing registry or storage mechanics."""

    def __init__(self, uows: UowFactory, data_store: AdmittedDataStore) -> None:
        self._uows = uows
        self._data_store = data_store

    def create_record(
        self, context: ApplicationContext, service_id: UUID, command: CreateRecordCommand
    ) -> RecordMutationOutcome | DataFailure:
        return self._dispatch(
            context, service_id, command, CreateRecordCommand, "create_record", RecordMutationOutcome
        )

    def get_record(
        self, context: ApplicationContext, service_id: UUID, query: GetRecordQuery
    ) -> RecordValue | DataFailure:
        return self._dispatch(context, service_id, query, GetRecordQuery, "get_record", RecordValue)

    def list_records(
        self, context: ApplicationContext, service_id: UUID, query: ListRecordsQuery
    ) -> RecordPage | DataFailure:
        return self._dispatch(context, service_id, query, ListRecordsQuery, "list_records", RecordPage)

    def search_records(
        self, context: ApplicationContext, service_id: UUID, query: SearchRecordsQuery
    ) -> RecordPage | DataFailure:
        return self._dispatch(context, service_id, query, SearchRecordsQuery, "search_records", RecordPage)

    def search_records_from_filters(
        self,
        context: ApplicationContext,
        service_id: UUID,
        entity_name: str,
        filters: Mapping[str, object],
        *,
        branch_id: UUID | None,
        branch_role: str | None,
        page: PageRequest,
    ) -> RecordPage | DataFailure:
        """Compile exact filter modes only after detached manifest admission."""

        if not isinstance(filters, Mapping):
            return DataFailure("validation_failed")
        admission = self._admit(context, service_id)
        if type(admission) is DataFailure:
            return admission
        try:
            compiled = tuple(
                admission.specification.validate_filter(entity_name, name, value)
                for name, value in sorted(filters.items())
            )
            query = SearchRecordsQuery(entity_name, compiled, branch_id, branch_role, page)  # type: ignore[arg-type]
        except Exception:
            return DataFailure("validation_failed")
        return self._execute_admitted(admission, context, "search_records", query, RecordPage)

    def update_record(
        self, context: ApplicationContext, service_id: UUID, command: UpdateRecordCommand
    ) -> RecordMutationOutcome | DataFailure:
        return self._dispatch(
            context, service_id, command, UpdateRecordCommand, "update_record", RecordMutationOutcome
        )

    def delete_record(
        self, context: ApplicationContext, service_id: UUID, command: DeleteRecordCommand
    ) -> DeleteRecordOutcome | DataFailure:
        return self._dispatch(
            context, service_id, command, DeleteRecordCommand, "delete_record", DeleteRecordOutcome
        )

    def record_history(
        self, context: ApplicationContext, service_id: UUID, query: RecordHistoryQuery
    ) -> HistoryPage | DataFailure:
        return self._dispatch(context, service_id, query, RecordHistoryQuery, "record_history", HistoryPage)

    def create_branch(
        self, context: ApplicationContext, service_id: UUID, command: CreateBranchCommand
    ) -> BranchMutationOutcome | DataFailure:
        return self._dispatch(
            context, service_id, command, CreateBranchCommand, "create_branch", BranchMutationOutcome
        )

    def list_branches(
        self, context: ApplicationContext, service_id: UUID, query: ListBranchesQuery
    ) -> BranchPage | DataFailure:
        return self._dispatch(context, service_id, query, ListBranchesQuery, "list_branches", BranchPage)

    def update_branch(
        self, context: ApplicationContext, service_id: UUID, command: UpdateBranchCommand
    ) -> BranchMutationOutcome | DataFailure:
        return self._dispatch(
            context, service_id, command, UpdateBranchCommand, "update_branch", BranchMutationOutcome
        )

    def assign_branch_records(
        self, context: ApplicationContext, service_id: UUID, command: AssignBranchRecordsCommand
    ) -> BranchAssignmentOutcome | DataFailure:
        return self._dispatch(
            context,
            service_id,
            command,
            AssignBranchRecordsCommand,
            "assign_branch_records",
            BranchAssignmentOutcome,
        )

    def service_summary(
        self, context: ApplicationContext, service_id: UUID, query: SummaryQuery
    ) -> SummaryView | DataFailure:
        if type(query) is not SummaryQuery:
            return DataFailure("validation_failed")
        admission = self._admit(context, service_id)
        if type(admission) is DataFailure:
            return admission
        result = self._execute_admitted(admission, context, "service_summary", query, ServiceSummary)
        return SummaryView(result, admission.specification) if type(result) is ServiceSummary else result

    def service_summary_sample(
        self, context: ApplicationContext, service_id: UUID, sample: int
    ) -> SummaryView | DataFailure:
        """Build uniform, bounded per-entity samples after manifest admission."""

        if type(sample) is not int or not 0 <= sample <= 3:
            return DataFailure("validation_failed")
        admission = self._admit(context, service_id)
        if type(admission) is DataFailure:
            return admission
        try:
            query = SummaryQuery(tuple((entity.name, sample) for entity in admission.specification.entities))
        except Exception:
            return DataFailure("internal_error")
        result = self._execute_admitted(admission, context, "service_summary", query, ServiceSummary)
        return SummaryView(result, admission.specification) if type(result) is ServiceSummary else result

    def maintenance_scan(
        self, context: ApplicationContext, service_id: UUID, query: MaintenanceQuery
    ) -> MaintenanceResult | DataFailure:
        return self._dispatch(
            context, service_id, query, MaintenanceQuery, "maintenance_scan", MaintenanceResult
        )

    def bootstrap(
        self, context: ApplicationContext, limit: int = _MAX_BOOTSTRAP_SERVICES
    ) -> BootstrapResult | DataFailure:
        """Return bounded registry shape without opening or consulting a data store."""

        if not self._valid_context(context) or not self._valid_limit(limit):
            return DataFailure("validation_failed")
        uow = self._new_uow()
        if uow is None:
            return DataFailure("internal_error")
        try:
            total = uow.services.count(context.principal_id)
            services = uow.services.list(context.principal_id, limit)
            if type(total) is not int or total < 0 or any(
                type(service) is not Service or service.principal_id != context.principal_id
                for service in services
            ):
                raise ValueError
            scoped = sorted(
                services,
                key=lambda service: (service.created_at, service.service_id),
            )
            cards = tuple(
                self._bootstrap_card(
                    service,
                    uow.mutations.recovery_state(context.principal_id, service.service_id),
                )
                for service in scoped[:limit]
            )
            result: BootstrapResult | DataFailure = BootstrapResult(
                services=cards,
                truncated=total > len(cards),
            )
        except Exception:
            result = DataFailure("internal_error")
        if not self._close(uow):
            return DataFailure("internal_error")
        return result

    def _dispatch(
        self,
        context: ApplicationContext,
        service_id: UUID,
        value: object,
        value_type: type[object],
        method_name: DataOperation,
        success_type: type[object],
    ) -> object:
        if type(value) is not value_type:
            return DataFailure("validation_failed")
        admission = self._admit(context, service_id)
        if type(admission) is DataFailure:
            return admission
        return self._execute_admitted(admission, context, method_name, value, success_type)

    def _execute_admitted(
        self,
        admission: _Admission,
        context: ApplicationContext,
        method_name: DataOperation,
        value: object,
        success_type: type[object],
    ) -> object:
        try:
            result = self._data_store.execute(
                method_name,
                admission.storage,
                admission.principal_id,
                context.created_via,
                admission.specification,
                value,
            )
        except Exception:
            return DataFailure("internal_error")
        if type(result) is DataFailure or type(result) is success_type:
            return result
        return DataFailure("internal_error")

    def _admit(
        self, context: ApplicationContext, service_id: UUID
    ) -> _Admission | DataFailure:
        if not self._valid_context(context) or not self._valid_service_id(service_id):
            return DataFailure("validation_failed")
        uow = self._new_uow()
        if uow is None:
            return DataFailure("internal_error")
        try:
            service = uow.services.get(context.principal_id, service_id)
            if type(service) is not Service or service.principal_id != context.principal_id:
                result: _Admission | DataFailure = DataFailure("not_found")
            else:
                recovery_state = uow.mutations.recovery_state(context.principal_id, service_id)
                result = self._detach_admission(context.principal_id, service, recovery_state.value)
        except Exception:
            result = DataFailure("internal_error")
        if not self._close(uow):
            return DataFailure("internal_error")
        return result

    @staticmethod
    def _detach_admission(
        principal_id: str, service: Service, recovery_state: str
    ) -> _Admission | DataFailure:
        if recovery_state == "pending":
            return DataFailure("storage_busy")
        if recovery_state != "clear":
            return DataFailure("storage_unavailable")
        if (
            service.design_state != "materialised"
            or service.operational_status != "active"
            or service.manifest is None
            or service.storage is None
        ):
            return DataFailure("storage_unavailable")
        try:
            storage = MaterialisationStorage(service.storage.backend, service.storage.namespace)
            specification = compile_record_specification(service.manifest)
        except Exception:
            return DataFailure("internal_error")
        return _Admission(storage, principal_id, specification)

    @staticmethod
    def _bootstrap_card(service: Service, recovery_state: object) -> BootstrapService:
        if not isinstance(recovery_state, str) or recovery_state not in {
            "clear",
            "pending",
            "recovery_required",
        }:
            raise ValueError
        entity_names: tuple[str, ...] = ()
        if service.manifest is not None:
            entity_names = tuple(entity.name for entity in compile_record_specification(service.manifest).entities)
        return BootstrapService(
            service_id=service.service_id,
            domain_name=service.domain_name,
            domain_class=service.domain_class,
            intent_description=service.intent_description,
            design_state=service.design_state,
            operational_status=service.operational_status,
            schema_version=service.schema_version,
            recovery_state=str(recovery_state),
            entity_names=entity_names,
        )

    def _new_uow(self) -> RegistryUnitOfWork | None:
        try:
            return self._uows()
        except Exception:
            return None

    @staticmethod
    def _close(uow: RegistryUnitOfWork) -> bool:
        try:
            uow.close()
        except Exception:
            return False
        return True

    @staticmethod
    def _valid_context(context: object) -> bool:
        return (
            type(context) is ApplicationContext
            and DataApplication._valid_text(context.principal_id, _MAX_PRINCIPAL_LENGTH)
            and DataApplication._valid_text(context.created_via, _MAX_CREATED_VIA_LENGTH)
        )

    @staticmethod
    def _valid_service_id(service_id: object) -> bool:
        return type(service_id) is UUID and service_id.int != 0

    @staticmethod
    def _valid_limit(limit: object) -> bool:
        return type(limit) is int and 1 <= limit <= _MAX_BOOTSTRAP_SERVICES

    @staticmethod
    def _valid_text(value: object, maximum: int) -> bool:
        return type(value) is str and bool(value) and value == value.strip() and len(value) <= maximum
