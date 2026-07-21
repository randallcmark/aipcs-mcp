"""SYNTHETIC_FIXTURE. Subprocess transport proof with no persistence payload."""

from __future__ import annotations

import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from aipcs_mcp.contracts import public_server_info

ROOT = Path(__file__).resolve().parents[1]


async def _stdio_smoke() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "aipcs_mcp", "serve", "--transport", "stdio"],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src")},
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        initialized = await session.initialize()
        assert initialized.serverInfo.name == "aipcs-mcp"

        tools = await session.list_tools()
        assert [tool.name for tool in tools.tools] == ["aipcs_server_info"]

        result = await session.call_tool("aipcs_server_info", {})
        assert result.isError is not True
        assert result.structuredContent == {
            "ok": True,
            "result": public_server_info().model_dump(mode="json"),
            "error": None,
        }


def test_stdio_server_info_smoke() -> None:
    anyio.run(_stdio_smoke)
