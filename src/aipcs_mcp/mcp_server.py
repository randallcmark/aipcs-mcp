"""Low-level MCP transport adapter for the deliberately narrow public surface."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from pydantic import TypeAdapter

from . import __version__
from .application.data import DataApplication, SummaryView
from .application.errors import Conflict, InternalFailure, InvalidCommand, InvalidState, NotFound
from .application.models import (
    ApplicationContext,
    DesignCommand,
    LifecycleExecutionResult,
    SeedCommand,
    project,
)
from .application.services import ServiceApplication
from .contracts import (
    ServerInfo,
    ServiceDesignRequest,
    ServiceEvolveRequest,
    ServiceInspectRequest,
    ServiceListRequest,
    ServiceListResult,
    ServiceMaterialiseRequest,
    ServiceMetadata,
    ServiceSeedRequest,
    parse_service_design,
    parse_service_evolve,
    parse_service_inspect,
    parse_service_list,
    parse_service_materialise,
    parse_service_seed,
    public_server_info,
)
from .data_requests import (
    BootstrapRequest,
    BranchAssignRecordsRequest,
    BranchCreateRequest,
    BranchListRequest,
    BranchUpdateRequest,
    MaintenanceScanRequest,
    RecordCreateRequest,
    RecordDeleteRequest,
    RecordGetRequest,
    RecordHistoryRequest,
    RecordListRequest,
    RecordSearchRequest,
    RecordUpdateRequest,
    ServiceSummaryRequest,
    parse_bootstrap,
    parse_branch_assign_records,
    parse_branch_create,
    parse_branch_list,
    parse_branch_update,
    parse_maintenance_scan,
    parse_record_create,
    parse_record_delete,
    parse_record_get,
    parse_record_history,
    parse_record_list,
    parse_record_search,
    parse_record_update,
    parse_service_summary,
)
from .data_results import (
    BootstrapResult as PublicBootstrapResult,
)
from .data_results import (
    BranchAssignmentResult as PublicBranchAssignmentResult,
)
from .data_results import (
    BranchMutationResult as PublicBranchMutationResult,
)
from .data_results import (
    BranchPageResult as PublicBranchPageResult,
)
from .data_results import (
    MaintenanceResult as PublicMaintenanceResult,
)
from .data_results import (
    RecordDeleteResult as PublicRecordDeleteResult,
)
from .data_results import (
    RecordDocument,
    project_bootstrap,
    project_branch_assignment,
    project_branch_mutation,
    project_branch_page,
    project_maintenance,
    project_record_delete,
    project_record_document,
    project_record_history,
    project_record_mutation,
    project_record_page,
    project_service_summary,
)
from .data_results import (
    RecordHistoryResult as PublicRecordHistoryResult,
)
from .data_results import (
    RecordMutationResult as PublicRecordMutationResult,
)
from .data_results import (
    RecordPageResult as PublicRecordPageResult,
)
from .data_results import (
    ServiceSummaryResult as PublicServiceSummaryResult,
)
from .errors import (
    DATA_ERROR_CODES,
    AipcsContractError,
    ErrorCode,
    FailureEnvelope,
    PublicModel,
    SuccessEnvelope,
    success,
)
from .lifecycle import (
    EvolveCommand,
    LifecycleCommand,
    LifecycleResultCategory,
    MaterialiseCommand,
    RecoveryState,
)
from .records import (
    AssignBranchRecordsCommand,
    BranchAssignmentTarget,
    CreateBranchCommand,
    CreateRecordCommand,
    DataFailure,
    DeleteRecordCommand,
    FrozenJsonObject,
    GetRecordQuery,
    ListBranchesQuery,
    ListRecordsQuery,
    MaintenanceQuery,
    PageCursor,
    PageRequest,
    RecordHistoryQuery,
    UpdateBranchCommand,
    UpdateRecordCommand,
)


class LifecycleExecutor(Protocol):
    """Narrow transport seam for the private, already-composed lifecycle coordinator."""

    def execute(self, command: LifecycleCommand) -> LifecycleExecutionResult: ...


class _TypedSuccessEnvelope[ResultT](PublicModel):
    """Schema-only success envelope with a concrete per-tool result shape."""

    ok: Literal[True] = True
    result: ResultT
    error: None = None


_EMPTY_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}
_DATA_TOOL_NAMES = frozenset(
    {
        "aipcs_record_create",
        "aipcs_record_get",
        "aipcs_record_list",
        "aipcs_record_search",
        "aipcs_record_update",
        "aipcs_record_delete",
        "aipcs_record_history",
        "aipcs_bootstrap",
        "aipcs_service_summary",
        "aipcs_branch_create",
        "aipcs_branch_list",
        "aipcs_branch_update",
        "aipcs_branch_assign_records",
        "aipcs_maintenance_scan",
    }
)
_INTERNAL_RESULT = types.CallToolResult(
    content=[
        types.TextContent(
            type="text",
            text='{"ok":false,"result":null,"error":{"code":"internal_error","message":"The request could not be completed safely.","issues":[],"retryable":false}}',
        )
    ],
    structuredContent={
        "ok": False,
        "result": None,
        "error": {
            "code": "internal_error",
            "message": "The request could not be completed safely.",
            "issues": [],
            "retryable": False,
        },
    },
    isError=True,
)


def create_server(
    *,
    application: ServiceApplication | None = None,
    principal_id: str | None = None,
    registry_lifecycle: bool = False,
    lifecycle_executor: LifecycleExecutor | None = None,
    data_application: DataApplication | None = None,
) -> Server:
    """Build the finite server catalogue; configuration is composed elsewhere."""

    if registry_lifecycle != (application is not None and principal_id is not None):
        raise ValueError("Incomplete lifecycle binding.")
    if registry_lifecycle and not _valid_principal(principal_id):
        raise ValueError("Invalid lifecycle binding.")
    if lifecycle_executor is not None and (
        not registry_lifecycle or not callable(getattr(lifecycle_executor, "execute", None))
    ):
        raise ValueError("Incomplete materialisation lifecycle binding.")
    materialisation_lifecycle = lifecycle_executor is not None
    if data_application is not None and (not registry_lifecycle or not materialisation_lifecycle):
        raise ValueError("Incomplete data runtime binding.")
    data_runtime = data_application is not None
    server = Server("aipcs", version=__version__)
    tools = _tools(registry_lifecycle, materialisation_lifecycle, data_runtime)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return tools

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        # This catches every failure before the SDK's wrapper can serialize exception text.
        try:
            envelope = _dispatch(
                name,
                arguments,
                application,
                principal_id,
                registry_lifecycle,
                lifecycle_executor,
                data_application,
            )
            return _result(envelope)
        except Exception:
            return _safe_internal_result()

    return server


async def run_stdio(server: Server) -> None:
    """Run one already-composed low-level server over the public stdio seam."""

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _tools(
    registry_lifecycle: bool,
    materialisation_lifecycle: bool = False,
    data_runtime: bool = False,
) -> list[types.Tool]:
    values = [
        _tool(
            "aipcs_server_info",
            "Return the public AIPCS MCP capability contract.",
            _EMPTY_SCHEMA,
            ServerInfo,
        )
    ]
    if registry_lifecycle:
        values.extend(
            [
                _tool(
                    "aipcs_service_seed",
                    "Create or replay a durable service seed.",
                    ServiceSeedRequest,
                    ServiceMetadata,
                ),
                _tool(
                    "aipcs_service_list",
                    "List durable services for the configured principal.",
                    ServiceListRequest,
                    ServiceListResult,
                ),
                _tool(
                    "aipcs_service_inspect",
                    "Inspect one durable service projection.",
                    ServiceInspectRequest,
                    ServiceMetadata,
                ),
                _tool(
                    "aipcs_service_design",
                    "Validate and store an initial service design.",
                    ServiceDesignRequest,
                    ServiceMetadata,
                ),
            ]
        )
    if materialisation_lifecycle:
        values.extend(
            [
                _tool(
                    "aipcs_service_materialise",
                    "Materialise a designed service using an exact lifecycle precondition.",
                    ServiceMaterialiseRequest,
                    ServiceMetadata,
                ),
                _tool(
                    "aipcs_service_evolve",
                    "Evolve a materialised service with one complete adjacent manifest.",
                    ServiceEvolveRequest,
                    ServiceMetadata,
                ),
            ]
        )
    if data_runtime:
        values.extend(
            [
                _tool("aipcs_record_create", "Create one record in a materialised service.", RecordCreateRequest, PublicRecordMutationResult),
                _tool("aipcs_record_get", "Get one record by entity and identifier.", RecordGetRequest, RecordDocument),
                _tool("aipcs_record_list", "List records with bounded exact branch scope.", RecordListRequest, PublicRecordPageResult),
                _tool("aipcs_record_search", "Search records using declared exact filters.", RecordSearchRequest, PublicRecordPageResult),
                _tool("aipcs_record_update", "Update one record with an exact version precondition.", RecordUpdateRequest, PublicRecordMutationResult),
                _tool("aipcs_record_delete", "Delete one record with an exact version precondition.", RecordDeleteRequest, PublicRecordDeleteResult),
                _tool("aipcs_record_history", "List bounded record history.", RecordHistoryRequest, PublicRecordHistoryResult),
                _tool("aipcs_bootstrap", "Orient to service topology without reading service stores.", BootstrapRequest, PublicBootstrapResult),
                _tool("aipcs_service_summary", "Summarise one materialised service with bounded discovery facts.", ServiceSummaryRequest, PublicServiceSummaryResult),
                _tool("aipcs_branch_create", "Create one durable memory branch.", BranchCreateRequest, PublicBranchMutationResult),
                _tool("aipcs_branch_list", "List direct memory branches with bounded pagination.", BranchListRequest, PublicBranchPageResult),
                _tool("aipcs_branch_update", "Update one memory branch with an exact revision precondition.", BranchUpdateRequest, PublicBranchMutationResult),
                _tool("aipcs_branch_assign_records", "Assign or unassign bounded records from one branch.", BranchAssignRecordsRequest, PublicBranchAssignmentResult),
                _tool("aipcs_maintenance_scan", "Return bounded, read-only mechanical maintenance signals.", MaintenanceScanRequest, PublicMaintenanceResult),
            ]
        )
    return values


def _tool(
    name: str,
    description: str,
    request: type[Any] | dict[str, object],
    result: type[Any],
) -> types.Tool:
    schema = request if isinstance(request, dict) else request.model_json_schema(by_alias=True)
    schema.setdefault("required", [])
    schema["additionalProperties"] = False
    output_schema = TypeAdapter(_TypedSuccessEnvelope[result] | FailureEnvelope).json_schema(
        by_alias=True
    )
    return types.Tool(
        name=name, description=description, inputSchema=schema, outputSchema=output_schema
    )


def _dispatch(
    name: object,
    arguments: object,
    application: ServiceApplication | None,
    principal_id: str | None,
    registry_lifecycle: bool,
    lifecycle_executor: LifecycleExecutor | None = None,
    data_application: DataApplication | None = None,
) -> SuccessEnvelope | FailureEnvelope:
    if not isinstance(name, str):
        return _failure(ErrorCode.UNSUPPORTED_OPERATION, "The requested MCP tool is unavailable.")
    if not isinstance(arguments, Mapping):
        return _failure(ErrorCode.VALIDATION_FAILED, "Request arguments must be a JSON object.")
    if name == "aipcs_server_info":
        if arguments:
            return _failure(
                ErrorCode.VALIDATION_FAILED, "Request failed public contract validation."
            )
        return success(
            public_server_info(
                registry_lifecycle=registry_lifecycle,
                materialisation_lifecycle=lifecycle_executor is not None,
                record_runtime=data_application is not None,
                discovery_topology=data_application is not None,
            ).model_dump(mode="json")
        )
    if not registry_lifecycle or application is None or principal_id is None:
        return _failure(ErrorCode.UNSUPPORTED_OPERATION, "The requested MCP tool is unavailable.")
    context = ApplicationContext(principal_id=principal_id, created_via="mcp")
    try:
        if name in _DATA_TOOL_NAMES:
            if data_application is None:
                return _failure(ErrorCode.UNSUPPORTED_OPERATION, "The requested MCP tool is unavailable.")
            return _data_envelope(_dispatch_data(name, arguments, data_application, context))
        if name == "aipcs_service_seed":
            request = parse_service_seed(arguments)
            result = application.seed(
                context,
                SeedCommand(
                    domain_name=request.domain_name,
                    domain_class=request.domain_class,
                    intent_description=request.intent_description,
                    idempotency_key=request.idempotency_key,
                ),
            )
        elif name == "aipcs_service_list":
            request = parse_service_list(arguments)
            result = ServiceListResult(services=application.list(context, request.limit))
        elif name == "aipcs_service_inspect":
            request = parse_service_inspect(arguments)
            result = application.inspect(context, request.service_id)
        elif name == "aipcs_service_design":
            request = parse_service_design(arguments)
            result = application.design(
                context,
                DesignCommand(
                    service_id=request.service_id,
                    manifest=request.schema_,
                    idempotency_key=request.idempotency_key,
                ),
            )
        elif name == "aipcs_service_materialise":
            if lifecycle_executor is None:
                return _failure(
                    ErrorCode.UNSUPPORTED_OPERATION, "The requested MCP tool is unavailable."
                )
            request = parse_service_materialise(arguments)
            try:
                command = MaterialiseCommand(
                    principal_id=context.principal_id,
                    created_via=context.created_via,
                    service_id=request.service_id,
                    expected_service_revision=request.expected_service_revision,
                    expected_schema_version=request.expected_schema_version,
                    idempotency_key=request.idempotency_key,
                )
            except Exception:
                return _failure(ErrorCode.VALIDATION_FAILED, "Request failed public contract validation.")
            return _lifecycle_envelope(lifecycle_executor.execute(command))
        elif name == "aipcs_service_evolve":
            if lifecycle_executor is None:
                return _failure(
                    ErrorCode.UNSUPPORTED_OPERATION, "The requested MCP tool is unavailable."
                )
            request = parse_service_evolve(arguments)
            try:
                command = EvolveCommand(
                    principal_id=context.principal_id,
                    created_via=context.created_via,
                    service_id=request.service_id,
                    expected_service_revision=request.expected_service_revision,
                    expected_schema_version=request.expected_schema_version,
                    idempotency_key=request.idempotency_key,
                    target_manifest=request.schema_,
                )
            except Exception:
                return _failure(ErrorCode.VALIDATION_FAILED, "Request failed public contract validation.")
            return _lifecycle_envelope(lifecycle_executor.execute(command))
        else:
            return _failure(
                ErrorCode.UNSUPPORTED_OPERATION, "The requested MCP tool is unavailable."
            )
    except AipcsContractError as error:
        return error.envelope()
    except NotFound:
        return _failure(ErrorCode.NOT_FOUND, "The requested service was not found.")
    except Conflict:
        return _failure(
            ErrorCode.CONFLICT, "The request conflicts with existing application state."
        )
    except InvalidState:
        return _failure(
            ErrorCode.INVALID_STATE, "The service is not in a state that permits this operation."
        )
    except InvalidCommand:
        return _failure(ErrorCode.VALIDATION_FAILED, "The application command is invalid.")
    except InternalFailure:
        return _failure(
            ErrorCode.INTERNAL_ERROR, "The application operation could not be completed."
        )
    except Exception:
        return _failure(ErrorCode.INTERNAL_ERROR, "The request could not be completed safely.")
    try:
        return success(result.model_dump(mode="json", by_alias=True, warnings="error"))
    except Exception:
        return _failure(ErrorCode.INTERNAL_ERROR, "The request could not be completed safely.")


def _dispatch_data(
    name: str,
    arguments: Mapping[str, object],
    application: DataApplication,
    context: ApplicationContext,
) -> object:
    """Parse strict request JSON and construct only detached pure data values."""

    if name == "aipcs_record_create":
        request = parse_record_create(arguments)
        return _project_data(application.create_record(
            context, request.service_id,
            CreateRecordCommand(request.entity_name, FrozenJsonObject.from_mapping(request.record), request.idempotency_key),
        ), project_record_mutation)
    if name == "aipcs_record_get":
        request = parse_record_get(arguments)
        return _project_data(
            application.get_record(context, request.service_id, GetRecordQuery(request.entity_name, request.record_id)),
            project_record_document,
        )
    if name == "aipcs_record_list":
        request = parse_record_list(arguments)
        return _project_data(
            application.list_records(
                context, request.service_id,
                ListRecordsQuery(request.entity_name, request.branch_id, request.branch_role, _page(request)),
            ),
            project_record_page,
        )
    if name == "aipcs_record_search":
        request = parse_record_search(arguments)
        return _project_data(
            application.search_records_from_filters(
                context, request.service_id, request.entity_name, request.filters,
                branch_id=request.branch_id, branch_role=request.branch_role, page=_page(request),
            ),
            project_record_page,
        )
    if name == "aipcs_record_update":
        request = parse_record_update(arguments)
        return _project_data(application.update_record(
            context, request.service_id,
            UpdateRecordCommand(
                request.entity_name, request.record_id, FrozenJsonObject.from_mapping(request.updates),
                request.expected_record_version, request.idempotency_key,
            ),
        ), project_record_mutation)
    if name == "aipcs_record_delete":
        request = parse_record_delete(arguments)
        return _project_data(application.delete_record(
            context, request.service_id,
            DeleteRecordCommand(
                request.entity_name, request.record_id, request.expected_record_version, request.idempotency_key,
            ),
        ), project_record_delete)
    if name == "aipcs_record_history":
        request = parse_record_history(arguments)
        return _project_data(application.record_history(
            context, request.service_id, RecordHistoryQuery(request.entity_name, request.record_id, _page(request)),
        ), project_record_history)
    if name == "aipcs_bootstrap":
        request = parse_bootstrap(arguments)
        value = application.bootstrap(context, request.limit)
        if type(value) is DataFailure:
            return value
        return project_bootstrap(value.services, value.truncated)
    if name == "aipcs_service_summary":
        request = parse_service_summary(arguments)
        value = application.service_summary_sample(context, request.service_id, request.sample)
        if type(value) is DataFailure:
            return value
        if type(value) is not SummaryView:
            raise ValueError
        return project_service_summary(value.summary, value.specification)
    if name == "aipcs_branch_create":
        request = parse_branch_create(arguments)
        return _project_data(application.create_branch(
            context, request.service_id,
            CreateBranchCommand(
                request.slug, request.title, request.intent, request.branch_type,
                request.parent_branch_id, request.retrieval_summary, request.idempotency_key,
            ),
        ), project_branch_mutation)
    if name == "aipcs_branch_list":
        request = parse_branch_list(arguments)
        return _project_data(application.list_branches(
            context, request.service_id,
            ListBranchesQuery(request.status, request.branch_type, request.parent_branch_id, _page(request)),
        ), project_branch_page)
    if name == "aipcs_branch_update":
        request = parse_branch_update(arguments)
        updates = request.updates.model_dump(exclude_unset=True)
        if "parent_branch_id" in updates and updates["parent_branch_id"] is not None:
            updates["parent_branch_id"] = str(updates["parent_branch_id"])
        return _project_data(application.update_branch(
            context, request.service_id,
            UpdateBranchCommand(
                request.branch_id, FrozenJsonObject.from_mapping(updates), request.expected_branch_revision,
                request.idempotency_key,
            ),
        ), project_branch_mutation)
    if name == "aipcs_branch_assign_records":
        request = parse_branch_assign_records(arguments)
        targets = tuple(
            BranchAssignmentTarget(target.entity_name, target.record_id, target.expected_record_version)
            for target in request.records
        )
        return _project_data(application.assign_branch_records(
            context, request.service_id,
            AssignBranchRecordsCommand(
                request.branch_id, request.role, request.operation, targets, request.idempotency_key,
            ),
        ), project_branch_assignment)
    if name == "aipcs_maintenance_scan":
        request = parse_maintenance_scan(arguments)
        scans = tuple(request.scan_types or (
            "expired", "stale", "low_confidence", "superseded", "missing_authority", "unbranched",
            "duplicate_authority", "blob_candidate",
        ))
        return _project_data(application.maintenance_scan(
            context, request.service_id,
            MaintenanceQuery(
                scans, request.entity_name, request.branch_id, request.limit,
                request.stale_after_days, request.low_confidence_below,
            ),
        ), project_maintenance)
    raise ValueError


def _page(request: Any) -> PageRequest:
    cursor = request.cursor
    limit = request.limit
    return PageRequest(limit, None if cursor is None else PageCursor(cursor))


def _project_data(value: object, projector: object) -> object:
    if type(value) is DataFailure:
        return value
    if not callable(projector):
        raise ValueError
    return projector(value)


_DATA_FAILURES: dict[str, tuple[ErrorCode, str, bool]] = {
    "validation_failed": (ErrorCode.VALIDATION_FAILED, "Request failed public contract validation.", False),
    "not_found": (ErrorCode.NOT_FOUND, "The requested record or branch was not found.", False),
    "already_exists": (ErrorCode.ALREADY_EXISTS, "The requested record or branch already exists.", False),
    "changed_fingerprint": (ErrorCode.CHANGED_FINGERPRINT, "The idempotency key cannot be reused for a different request.", False),
    "stale_record_version": (ErrorCode.STALE_RECORD_VERSION, "The record version precondition is stale.", False),
    "stale_branch_revision": (ErrorCode.STALE_BRANCH_REVISION, "The branch revision precondition is stale.", False),
    "constraint_violation": (ErrorCode.CONSTRAINT_VIOLATION, "The requested data relationship is not permitted.", False),
    "storage_migration_required": (ErrorCode.STORAGE_MIGRATION_REQUIRED, "Service storage requires migration.", False),
    "storage_busy": (ErrorCode.STORAGE_BUSY, "Storage is temporarily busy.", True),
    "operation_uncertain": (ErrorCode.OPERATION_UNCERTAIN, "The operation outcome is uncertain.", True),
    "storage_unavailable": (ErrorCode.STORAGE_UNAVAILABLE, "Storage is unavailable.", False),
    "internal_error": (ErrorCode.INTERNAL_ERROR, "The request could not be completed safely.", False),
}


def _data_envelope(value: object) -> SuccessEnvelope | FailureEnvelope:
    if type(value) is DataFailure:
        mapped = _DATA_FAILURES.get(value.code)
        if mapped is None or mapped[0] not in DATA_ERROR_CODES:
            return _failure(ErrorCode.INTERNAL_ERROR, "The request could not be completed safely.")
        code, message, retryable = mapped
        return FailureEnvelope(error={"code": code, "message": message, "retryable": retryable})
    try:
        return success(value.model_dump(mode="json", by_alias=True, warnings="error"))
    except Exception:
        return _failure(ErrorCode.INTERNAL_ERROR, "The request could not be completed safely.")


def _failure(code: ErrorCode, message: str) -> FailureEnvelope:
    return FailureEnvelope(error={"code": code, "message": message})


_LIFECYCLE_FAILURES: dict[LifecycleResultCategory, tuple[ErrorCode, str, bool]] = {
    LifecycleResultCategory.MALFORMED_INPUT: (
        ErrorCode.VALIDATION_FAILED,
        "Request failed public contract validation.",
        False,
    ),
    LifecycleResultCategory.UNSUPPORTED_TRANSITION: (
        ErrorCode.UNSUPPORTED_TRANSITION,
        "The service lifecycle transition is not supported.",
        False,
    ),
    LifecycleResultCategory.STALE_REVISION: (
        ErrorCode.STALE_REVISION,
        "The service lifecycle precondition is stale.",
        False,
    ),
    LifecycleResultCategory.CHANGED_FINGERPRINT: (
        ErrorCode.CHANGED_FINGERPRINT,
        "The idempotency key cannot be reused for a different request.",
        False,
    ),
    LifecycleResultCategory.OPERATION_IN_PROGRESS: (
        ErrorCode.OPERATION_IN_PROGRESS,
        "A lifecycle operation is already in progress.",
        True,
    ),
    LifecycleResultCategory.RECOVERY_REQUIRED: (
        ErrorCode.RECOVERY_REQUIRED,
        "The service requires recovery before lifecycle work can continue.",
        False,
    ),
    LifecycleResultCategory.STORAGE_BUSY: (
        ErrorCode.STORAGE_BUSY,
        "Storage is temporarily busy.",
        True,
    ),
    LifecycleResultCategory.OPERATION_UNCERTAIN: (
        ErrorCode.OPERATION_UNCERTAIN,
        "The lifecycle operation outcome is uncertain.",
        True,
    ),
    LifecycleResultCategory.STORAGE_UNAVAILABLE: (
        ErrorCode.STORAGE_UNAVAILABLE,
        "Storage is unavailable.",
        False,
    ),
    LifecycleResultCategory.INTERNAL_FAILURE: (
        ErrorCode.INTERNAL_ERROR,
        "The request could not be completed safely.",
        False,
    ),
}


def _lifecycle_envelope(result: object) -> SuccessEnvelope | FailureEnvelope:
    """Project only completed services; all other coordinator outcomes are bounded failures."""

    if type(result) is not LifecycleExecutionResult:
        return _failure(ErrorCode.INTERNAL_ERROR, "The request could not be completed safely.")
    if result.category == "completed":
        try:
            return success(project(result.service, RecoveryState.CLEAR).model_dump(mode="json"))
        except Exception:
            return _failure(ErrorCode.INTERNAL_ERROR, "The request could not be completed safely.")
    mapped = _LIFECYCLE_FAILURES.get(result.category)
    if mapped is None:
        return _failure(ErrorCode.INTERNAL_ERROR, "The request could not be completed safely.")
    code, message, retryable = mapped
    return FailureEnvelope(
        error={"code": code, "message": message, "retryable": retryable},
    )


def _result(envelope: SuccessEnvelope | FailureEnvelope) -> types.CallToolResult:
    try:
        payload = envelope.model_dump(mode="json", warnings="error")
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            structuredContent=payload,
            isError=not envelope.ok,
        )
    except Exception:
        return _safe_internal_result()


def _safe_internal_result() -> types.CallToolResult:
    return _INTERNAL_RESULT


def _valid_principal(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= 128
        and value.isprintable()
    )
