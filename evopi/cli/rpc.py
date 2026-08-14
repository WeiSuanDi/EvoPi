"""Local stdio JSONL host for ``evopi rpc``."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import suppress
from typing import TextIO

from evopi.harness import BaseHarness, ConfirmationBroker
from evopi.rpc import EventStream, HarnessRpcHost, JsonlRpcConnection, RpcServer
from evopi.rpc.connection import AsyncTextReader
from evopi.rpc.connection_v2 import JsonlRpcV2Connection
from evopi.rpc.harness_host_v2 import HarnessRpcV2Host
from evopi.rpc.server_v2 import RpcV2Server


class StdioTextReader:
    """Async line reader over a caller-owned text stream."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    async def readline(self) -> str:
        return await asyncio.to_thread(self._stream.readline)


class StdioTextWriter:
    """Async writer over a caller-owned text stream."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    async def write(self, text: str) -> None:
        self._stream.write(text)

    async def flush(self) -> None:
        self._stream.flush()


class _PrefixedReader:
    def __init__(self, first_line: str, reader: AsyncTextReader) -> None:
        self._first_line: str | None = first_line
        self._reader = reader

    async def readline(self) -> str:
        if self._first_line is not None:
            line = self._first_line
            self._first_line = None
            return line
        return await self._reader.readline()


async def _first_protocol_line(reader: AsyncTextReader) -> tuple[int, str] | None:
    while True:
        line = await reader.readline()
        if line == "":
            return None
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except (TypeError, ValueError):
            return 1, line
        if isinstance(payload, dict) and payload.get("schema_version") == 2:
            return 2, line
        return 1, line


async def run_stdio_rpc(
    harness: BaseHarness,
    broker: ConfirmationBroker,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Run one local JSONL connection until EOF or protocol failure."""

    reader = StdioTextReader(stdin or sys.stdin)
    writer = StdioTextWriter(stdout or sys.stdout)
    selected = await _first_protocol_line(reader)
    if selected is None:
        return 0
    protocol_version, first_line = selected

    event_stream = EventStream()
    host = HarnessRpcHost(harness, broker, event_stream=event_stream)
    prefixed_reader = _PrefixedReader(first_line, reader)
    if protocol_version == 2:
        versioned_host = HarnessRpcV2Host(host)
        versioned_server = RpcV2Server(versioned_host)
        connection: JsonlRpcConnection | JsonlRpcV2Connection = JsonlRpcV2Connection(
            prefixed_reader,
            writer,
            versioned_server,
        )
    else:
        versioned_host = None
        versioned_server = None
        connection = JsonlRpcConnection(prefixed_reader, writer, RpcServer(host))
    subscription = await event_stream.subscribe(after_sequence=0)

    async def forward_events() -> None:
        async for event in subscription:
            if versioned_host is None:
                assert isinstance(connection, JsonlRpcConnection)
                await connection.publish_event(event)
            elif versioned_server is not None and versioned_server.initialized:
                assert isinstance(connection, JsonlRpcV2Connection)
                await connection.publish_event(versioned_host.project_event(event))

    forwarder = asyncio.create_task(forward_events())
    try:
        await connection.run()
    finally:
        await host.close()
        if not forwarder.done():
            forwarder.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await forwarder
        close = getattr(subscription, "aclose", None)
        if close is not None:
            await close()
    return 0


__all__ = ["StdioTextReader", "StdioTextWriter", "run_stdio_rpc"]
