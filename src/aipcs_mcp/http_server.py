"""Streamable HTTP adapter for the fixed low-level AIPCS MCP catalogue.

This module owns only listener and MCP-session mechanics.  It deliberately does
not translate HTTP identity into the AIPCS process principal: deployments that
need authentication or multi-tenancy must enforce that boundary at a trusted
gateway before traffic reaches this process.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Route

from .configuration.models import ResolvedConfiguration


class _McpEndpoint:
    """Exact ASGI endpoint delegated to the SDK session manager."""

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self._manager = manager

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        await self._manager.handle_request(scope, receive, send)


def create_streamable_http_app(server: Server, config: ResolvedConfiguration) -> Starlette:
    """Create one single-endpoint, stateful Streamable HTTP application.

    The SDK assigns opaque transport session identifiers and reaps idle sessions.
    AIPCS data remains process-principal scoped; the HTTP session ID is never an
    AIPCS identity, tenancy, or authorization mechanism.
    """

    if config.transport != "streamable-http":
        raise ValueError("Streamable HTTP requires the streamable-http transport.")
    settings = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(config.http_allowed_hosts),
        allowed_origins=list(config.http_allowed_origins),
    )
    manager = StreamableHTTPSessionManager(
        server,
        json_response=False,
        stateless=False,
        security_settings=settings,
        session_idle_timeout=float(config.http_session_idle_timeout_seconds),
    )

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    return Starlette(
        routes=[Route(config.http_path, _McpEndpoint(manager), methods=["GET", "POST", "DELETE"])],
        lifespan=lifespan,
    )


async def run_streamable_http(server: Server, config: ResolvedConfiguration) -> None:
    """Serve the configured endpoint without trusting proxy-supplied headers."""

    app = create_streamable_http_app(server, config)
    runtime = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.http_host,
            port=config.http_port,
            log_level=config.log_level,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            date_header=False,
            lifespan="on",
        )
    )
    await runtime.serve()
