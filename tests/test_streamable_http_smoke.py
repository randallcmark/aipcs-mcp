"""SYNTHETIC_FIXTURE. Subprocess proof for the service transport listener."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from aipcs_mcp.contracts import public_server_info

ROOT = Path(__file__).resolve().parents[1]


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _wait_until_listening(url: str) -> None:
    async with httpx.AsyncClient(timeout=0.2) as client:
        for _ in range(100):
            try:
                response = await client.get(url)
            except httpx.ConnectError:
                await anyio.sleep(0.05)
                continue
            assert response.status_code == 406
            return
    raise AssertionError("Streamable HTTP process did not begin listening")


async def _streamable_http_smoke() -> None:
    port = _available_loopback_port()
    url = f"http://127.0.0.1:{port}/mcp"
    process = await anyio.open_process(
        [
            sys.executable,
            "-m",
            "aipcs_mcp",
            "serve",
            "--transport",
            "streamable-http",
            "--http-port",
            str(port),
        ],
        cwd=ROOT,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await _wait_until_listening(url)
        async with (
            streamable_http_client(url) as (read_stream, write_stream, _),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "aipcs-mcp"
            result = await session.call_tool("aipcs_server_info", {})
            assert result.isError is not True
            assert result.structuredContent == {
                "ok": True,
                "result": public_server_info().model_dump(mode="json"),
                "error": None,
            }
    finally:
        process.terminate()
        with anyio.move_on_after(5):
            await process.wait()
        if process.returncode is None:
            process.kill()
            await process.wait()


def test_streamable_http_server_info_smoke() -> None:
    anyio.run(_streamable_http_smoke)
