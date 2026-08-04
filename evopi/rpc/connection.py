"""JSONL RPC connection over injected async text reader/writer abstractions.

The connection is full-duplex. Server side: ``run()`` reads request lines,
dispatches each as an independent task (so completions can be out of order),
and writes responses; ``publish_event`` interleaves event lines with
responses. Client side: ``send_request`` writes a request line and awaits the
matching response. Writer access is serialized so every record is a whole
JSONL line. EOF and ``close`` are clean and idempotent; caller-owned streams
are never closed by this connection. A line that cannot be parsed at all ends
the session with ``RpcConnectionProtocolError``; a parseable request that
fails envelope validation receives an ``invalid_request`` response and the
session continues.
"""

from __future__ import annotations

import asyncio
import uuid as uuid_module
from typing import Protocol

from evopi.core.types import JsonObject

from .codec import (
    decode_envelope,
    encode_event,
    encode_request,
    encode_response,
    extract_request_id,
)
from .errors import RpcCodecError, RpcConnectionClosedError, RpcConnectionProtocolError
from .protocol import RpcEvent, RpcRequest, RpcResponse
from .server import RpcServer, error_response


class AsyncTextReader(Protocol):
    """Async text line reader; returns an empty string at EOF."""

    async def readline(self) -> str: ...


class AsyncTextWriter(Protocol):
    """Async text writer with explicit flush."""

    async def write(self, text: str) -> None: ...
    async def flush(self) -> None: ...


class JsonlRpcConnection:
    """One JSONL session between an ``RpcServer`` and a peer."""

    def __init__(
        self,
        reader: AsyncTextReader,
        writer: AsyncTextWriter,
        server: RpcServer,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._server = server
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._running = False
        self._pending: dict[str, asyncio.Future[RpcResponse]] = {}
        self._dispatch_tasks: set[asyncio.Task[None]] = set()

    @property
    def closed(self) -> bool:
        return self._closed

    async def run(self) -> None:
        """Drive the read loop until EOF, explicit close, or clean failure.

        EOF drains in-flight dispatches so fully received requests still get
        their responses; explicit ``close`` cancels them immediately.
        """
        if self._running:
            raise RuntimeError("run() may only be called once")
        self._running = True
        try:
            while not self._closed:
                line = await self._reader.readline()
                if line == "":
                    break  # EOF
                if not line.strip():
                    continue  # tolerate blank lines between records
                await self._handle_line(line)
        finally:
            await self._finish(await_dispatch=True)

    async def send_request(self, method: str, params: JsonObject) -> RpcResponse:
        """Write a request line and await its matching response."""
        if self._closed:
            raise RpcConnectionClosedError("connection is closed")
        request = RpcRequest(request_id=str(uuid_module.uuid4()), method=method, params=params)
        future: asyncio.Future[RpcResponse] = asyncio.get_running_loop().create_future()
        self._pending[request.request_id] = future
        try:
            await self._write_line(encode_request(request))
            return await future
        except BaseException:
            self._pending.pop(request.request_id, None)
            raise

    async def publish_event(self, event: RpcEvent) -> None:
        """Write one event line, serialized with responses."""
        if self._closed:
            raise RpcConnectionClosedError("connection is closed")
        await self._write_line(encode_event(event))

    async def close(self) -> None:
        """Cancel in-flight work and shut down exactly once.

        Never closes caller-owned streams.
        """
        await self._finish(await_dispatch=False)

    async def _handle_line(self, line: str) -> None:
        try:
            envelope = decode_envelope(line)
        except RpcCodecError:
            request_id = extract_request_id(line)
            if request_id is None:
                raise RpcConnectionProtocolError("malformed wire line") from None
            await self._write_response(
                error_response(
                    request_id,
                    "invalid_request",
                    "invalid request envelope",
                    {"request_id": request_id},
                )
            )
            return
        if isinstance(envelope, RpcRequest):
            self._dispatch_request(envelope)
        elif isinstance(envelope, RpcResponse):
            self._resolve_pending(envelope)
        # RpcEvent lines from the peer are unsolicited in v1 and are ignored.

    def _dispatch_request(self, request: RpcRequest) -> None:
        task = asyncio.create_task(self._handle_request(request))
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._done)

    async def _handle_request(self, request: RpcRequest) -> None:
        response = await self._server.dispatch(request)
        if not self._closed:
            await self._write_response(response)

    def _done(self, task: asyncio.Task[None]) -> None:
        self._dispatch_tasks.discard(task)
        if not task.cancelled():
            task.exception()  # retrieve so a failure never warns at GC

    def _resolve_pending(self, response: RpcResponse) -> None:
        future = self._pending.pop(response.request_id, None)
        if future is not None and not future.done():
            future.set_result(response)

    async def _finish(self, *, await_dispatch: bool) -> None:
        if self._closed:
            return
        dispatch_tasks = list(self._dispatch_tasks)
        if await_dispatch and dispatch_tasks:
            # EOF: fully received requests still deserve their responses, so
            # the connection stays open (writable) until the drain completes.
            await asyncio.gather(*dispatch_tasks, return_exceptions=True)
        self._closed = True
        for task in dispatch_tasks:
            task.cancel()
        if dispatch_tasks:
            await asyncio.gather(*dispatch_tasks, return_exceptions=True)
        self._dispatch_tasks.clear()
        await self._server.close()
        for request_id, future in list(self._pending.items()):
            if not future.done():
                future.set_result(
                    error_response(
                        request_id,
                        "connection_closed",
                        "connection closed",
                        {},
                    )
                )
        self._pending.clear()

    async def _write_response(self, response: RpcResponse) -> None:
        await self._write_line(encode_response(response))

    async def _write_line(self, text: str) -> None:
        async with self._write_lock:
            await self._writer.write(text + "\n")
            await self._writer.flush()


__all__ = ["AsyncTextReader", "AsyncTextWriter", "JsonlRpcConnection"]
