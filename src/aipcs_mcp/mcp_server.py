"""Minimal public FastMCP adapter for the stdio transport proof."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .contracts import public_server_info
from .errors import SuccessEnvelope, success


def create_server() -> FastMCP:
    """Create the public server without listener configuration or storage state."""

    server = FastMCP("aipcs-mcp")

    @server.tool(
        name="aipcs_server_info",
        description="Return the public AIPCS MCP capability contract.",
        structured_output=True,
    )
    def aipcs_server_info() -> SuccessEnvelope:
        return success(public_server_info().model_dump(mode="json"))

    return server
