"""SYNTHETIC_FIXTURE. Client-visible orientation and tool-effect contracts."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import anyio
import pytest
from application.fakes import FakeRegistry, FixedClock, SequentialIds
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_client_server_memory_streams
from stdio_helpers import sqlite_parameters

from aipcs_mcp.application.data import DataApplication
from aipcs_mcp.application.services import ServiceApplication
from aipcs_mcp.mcp_server import create_server

# Independently specified public effects: updates include metadata replacement
# and membership removal/moves, not only record deletion.
READS = {
    "aipcs_server_info",
    "aipcs_service_list",
    "aipcs_service_inspect",
    "aipcs_record_get",
    "aipcs_record_list",
    "aipcs_record_search",
    "aipcs_record_history",
    "aipcs_bootstrap",
    "aipcs_service_summary",
    "aipcs_branch_list",
    "aipcs_maintenance_scan",
}
ADDITIONS = {
    "aipcs_service_seed",
    "aipcs_service_design",
    "aipcs_service_materialise",
    "aipcs_record_create",
    "aipcs_branch_create",
}
CHANGES = {
    "aipcs_service_evolve",
    "aipcs_record_update",
    "aipcs_record_delete",
    "aipcs_branch_update",
    "aipcs_branch_assign_records",
}


def assert_client_guidance(
    initialized: types.InitializeResult, catalogue: types.ListToolsResult, count: int
) -> None:
    tools = {tool.name: tool for tool in catalogue.tools}
    assert len(tools) == count
    instructions = initialized.instructions
    assert isinstance(instructions, str) and 0 < len(instructions) < 2_000
    assert set(re.findall(r"aipcs_[a-z_]+", instructions)) <= tools.keys()
    assert "tools/list" in instructions
    if count == 1:
        assert "capability information only" in instructions
    else:
        assert "current manifest" in instructions
        assert "exact request and idempotency key" in instructions
    if count == 21:
        assert tools.keys() == READS | ADDITIONS | CHANGES
        assert "selectively" in instructions
        assert "no task schema is prescribed" in instructions
        assert "uncertainty" in instructions
        assert "not evidence authority" in instructions
        assert "adjacent additive manifest" in instructions
    for name, tool in tools.items():
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is (name in READS)
        assert tool.annotations.destructiveHint is (name in CHANGES)
        assert tool.annotations.idempotentHint is True
        assert tool.annotations.openWorldHint is False
        if name not in READS:
            assert "exact same request and idempotency key" in tool.description


class NoExecution:
    def execute(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("Discovery must not execute lifecycle or data operations")


@pytest.mark.parametrize("count", [1, 5, 7, 21])
def test_initialization_guidance_matches_bound_capabilities_without_storage(count: int) -> None:
    registry = FakeRegistry()
    registry.fail_at = "uow"
    server = create_server(
        application=ServiceApplication(registry.uow, FixedClock(), SequentialIds())
        if count > 1
        else None,
        principal_id="synthetic-principal" if count > 1 else None,
        registry_lifecycle=count > 1,
        lifecycle_executor=NoExecution() if count >= 7 else None,
        data_application=DataApplication(registry.uow, NoExecution()) if count == 21 else None,
    )

    async def exercise() -> None:
        async with (
            create_client_server_memory_streams() as (client_streams, server_streams),
            anyio.create_task_group() as tasks,
        ):
            tasks.start_soon(server.run, *server_streams, server.create_initialization_options())
            async with ClientSession(*client_streams) as session:
                initialized = await session.initialize()
                assert_client_guidance(initialized, await session.list_tools(), count)
            tasks.cancel_scope.cancel()

    anyio.run(exercise)
    assert registry.uows == []


@pytest.mark.parametrize(
    "persistent", [False, pytest.param(True, marks=pytest.mark.requires_sqlite)]
)
def test_stdio_client_receives_profile_guidance(tmp_path: Path, persistent: bool) -> None:
    if persistent:
        os.chmod(tmp_path, 0o700)
        parameters = sqlite_parameters(tmp_path / "memory", "synthetic-principal")
    else:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "aipcs_mcp", "serve"],
            env={"PYTHONDONTWRITEBYTECODE": "1"},
        )

    async def exercise() -> None:
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            assert_client_guidance(initialized, await session.list_tools(), 21 if persistent else 1)
            if persistent:
                bootstrap = await session.call_tool("aipcs_bootstrap", {})
                assert bootstrap.structuredContent["result"] == {"services": [], "truncated": False}

    anyio.run(exercise)
