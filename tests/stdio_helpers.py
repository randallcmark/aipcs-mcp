"""Synthetic test-only helpers for real AIPCS MCP stdio processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import anyio
from anyio.streams.buffered import BufferedByteReceiveStream
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class RawProcess:
    """Test-only raw subprocess state with a newline-aware stdout reader."""

    process: anyio.abc.Process
    stdout: BufferedByteReceiveStream


def sqlite_parameters(root: Path, principal: str, *, debug: bool = False) -> StdioServerParameters:
    """Return source-test parameters with every persistence input explicit."""

    args = [
        "-m",
        "aipcs_mcp",
        "serve",
        "--profile",
        "sqlite",
        "--principal-id",
        principal,
        "--sqlite-data-root",
        str(root),
    ]
    if debug:
        args.extend(("--log-level", "debug"))
    environment = {"PYTHONPATH": str(ROOT / "src"), "PATH": os.environ.get("PATH", "")}
    return StdioServerParameters(command=sys.executable, args=args, cwd=ROOT, env=environment)


@asynccontextmanager
async def async_session(
    parameters: StdioServerParameters, errlog: TextIO | None = None
) -> AsyncIterator[ClientSession]:
    """Open an official client session and complete MCP initialization."""

    client = stdio_client(parameters) if errlog is None else stdio_client(parameters, errlog=errlog)
    async with (
        client as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield session


async def call_envelope(
    session: ClientSession, name: str, arguments: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Call one public tool and prove its redundant envelope representations agree."""

    result = await session.call_tool(name, arguments)
    assert len(result.content) == 1
    assert isinstance(result.content[0], types.TextContent)
    envelope = json.loads(result.content[0].text)
    assert result.structuredContent == envelope
    assert isinstance(envelope, dict)
    return envelope, result.isError is True


async def spawn_raw(parameters: StdioServerParameters) -> RawProcess:
    """Start an unwrapped process for raw JSON-RPC boundary probes."""

    environment = os.environ.copy()
    if parameters.env is not None:
        environment.update(parameters.env)
    process = await anyio.open_process(
        [parameters.command, *parameters.args],
        cwd=parameters.cwd,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    return RawProcess(process=process, stdout=BufferedByteReceiveStream(process.stdout))


async def send_line(raw: RawProcess, payload: object | bytes) -> None:
    """Write exactly one raw protocol line."""

    assert raw.process.stdin is not None
    line = (
        payload
        if isinstance(payload, bytes)
        else json.dumps(payload, separators=(",", ":")).encode()
    )
    await raw.process.stdin.send(line + b"\n")


async def read_json_until_id(raw: RawProcess, request_id: int) -> dict[str, Any]:
    """Read JSON lines until the matching response, ignoring notifications."""

    with anyio.fail_after(5):
        while True:
            line = await raw.stdout.receive_until(b"\n", 1_000_000)
            if not line:
                raise AssertionError("stdio process closed before the requested response")
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("id") == request_id:
                return value


async def read_available(stream: anyio.abc.ByteReceiveStream) -> bytes:
    """Collect bounded available bytes without requiring a malformed-input response."""

    chunks: list[bytes] = []
    with anyio.move_on_after(0.25):
        while True:
            try:
                value = await stream.receive(65_536)
            except anyio.EndOfStream:
                break
            if not value:
                break
            chunks.append(value)
    return b"".join(chunks)


async def close_process(raw: RawProcess) -> tuple[bytes, bytes]:
    """Close stdin, wait, then collect any remaining protocol and diagnostic bytes."""

    process = raw.process
    if process.stdin is not None:
        await process.stdin.aclose()
    with anyio.move_on_after(5):
        await process.wait()
    if process.returncode is None:
        process.terminate()
        with anyio.move_on_after(5):
            await process.wait()
    stdout = await read_available(raw.stdout)
    stderr = await read_available(process.stderr) if process.stderr is not None else b""
    return stdout, stderr


async def raw_initialize(raw: RawProcess) -> None:
    """Perform the fixed public JSON-RPC initialization sequence."""

    await send_line(
        raw,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": types.LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "aipcs-test", "version": "0"},
            },
        },
    )
    response = await read_json_until_id(raw, 1)
    assert "result" in response
    await send_line(raw, {"jsonrpc": "2.0", "method": "notifications/initialized"})


def assert_no_secrets(value: object, *secrets: str) -> None:
    rendered = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    lowered = rendered.lower()
    for secret in secrets:
        assert secret.lower() not in lowered


def successful(envelope: Mapping[str, Any]) -> dict[str, Any]:
    assert envelope["ok"] is True
    assert envelope["error"] is None
    assert isinstance(envelope["result"], dict)
    return envelope["result"]
