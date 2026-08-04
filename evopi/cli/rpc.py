"""Local stdio JSONL host for ``evopi rpc``."""

from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from typing import TextIO

from evopi.harness import BaseHarness, ConfirmationBroker
from evopi.rpc import EventStream, HarnessRpcHost, JsonlRpcConnection, RpcServer


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


async def run_stdio_rpc(
    harness: BaseHarness,
    broker: ConfirmationBroker,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Run one local JSONL connection until EOF or protocol failure."""

    event_stream = EventStream()
    host = HarnessRpcHost(harness, broker, event_stream=event_stream)
    server = RpcServer(host)
    connection = JsonlRpcConnection(
        StdioTextReader(stdin or sys.stdin),
        StdioTextWriter(stdout or sys.stdout),
        server,
    )
    subscription = await event_stream.subscribe(after_sequence=0)

    async def forward_events() -> None:
        async for event in subscription:
            await connection.publish_event(event)

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
