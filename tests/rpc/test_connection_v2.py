"""Full-duplex transport tests for the RPC v2 connection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Callable

from evopi.rpc.codec_v2 import (
    decode_v2_request,
    encode_v2_event,
    encode_v2_request,
    encode_v2_response,
)
from evopi.rpc.connection_v2 import JsonlRpcV2Connection
from evopi.rpc.protocol_v2 import RpcV2Event, RpcV2Request, RpcV2Response
from evopi.rpc.server_v2 import RpcV2Server

from test_server_v2 import _Host


class _QueueReader:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()

    async def readline(self) -> str:
        return await self.queue.get()

    def feed(self, line: str) -> None:
        self.queue.put_nowait(line + "\n")

    def eof(self) -> None:
        self.queue.put_nowait("")


class _Writer:
    def __init__(self, callback: Callable[[str], None] | None = None) -> None:
        self.lines: list[str] = []
        self.callback = callback

    async def write(self, text: str) -> None:
        self.lines.append(text)
        if self.callback is not None:
            self.callback(text.rstrip("\n"))

    async def flush(self) -> None:
        return None


async def _wait_for(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not met")


def test_v2_client_connection_correlates_responses() -> None:
    async def scenario() -> None:
        reader = _QueueReader()

        def respond(line: str) -> None:
            request = decode_v2_request(line)
            reader.feed(
                encode_v2_response(
                    RpcV2Response(
                        request_id=request.request_id,
                        ok=True,
                        result={"closed": True},
                    )
                )
            )

        connection = JsonlRpcV2Connection(reader, _Writer(respond))
        run_task = asyncio.create_task(connection.run())

        response = await connection.send_request("shutdown", {})

        assert response.result == {"closed": True}
        reader.eof()
        await run_task

    asyncio.run(scenario())


def test_v2_server_connection_dispatches_requests() -> None:
    async def scenario() -> None:
        reader = _QueueReader()
        writer = _Writer()
        connection = JsonlRpcV2Connection(reader, writer, RpcV2Server(_Host()))
        request = RpcV2Request(
            request_id="init-1",
            method="initialize",
            params={"client_name": "tests", "client_version": "1.0"},
        )
        run_task = asyncio.create_task(connection.run())
        reader.feed(encode_v2_request(request))
        await _wait_for(lambda: len(writer.lines) == 1)
        reader.eof()

        await run_task

        assert len(writer.lines) == 1
        assert '"ok":true' in writer.lines[0]

    asyncio.run(scenario())


def test_v2_connection_delivers_inbound_events_to_one_owner() -> None:
    async def scenario() -> None:
        reader = _QueueReader()
        connection = JsonlRpcV2Connection(reader, _Writer())
        event = RpcV2Event(
            event_id="11111111-2222-4333-8444-555555555555",
            stream_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            sequence=1,
            type="agent_start",
            data={},
            run_id="run-1",
            created_at=datetime(2026, 8, 11, tzinfo=UTC),
        )
        run_task = asyncio.create_task(connection.run())
        reader.feed(encode_v2_event(event))
        reader.eof()

        received = [item async for item in connection.received_events()]
        await run_task

        assert received == [event]

    asyncio.run(scenario())


def test_cancelled_request_does_not_poison_later_correlated_responses() -> None:
    async def scenario() -> None:
        reader = _QueueReader()
        written: list[RpcV2Request] = []

        def remember(line: str) -> None:
            written.append(decode_v2_request(line))

        connection = JsonlRpcV2Connection(reader, _Writer(remember))
        run_task = asyncio.create_task(connection.run())
        cancelled = asyncio.create_task(connection.send_request("runtime.status", {}))
        await _wait_for(lambda: len(written) == 1)
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        reader.feed(
            encode_v2_response(
                RpcV2Response(request_id=written[0].request_id, ok=True, result={})
            )
        )

        current = asyncio.create_task(connection.send_request("runtime.status", {}))
        await _wait_for(lambda: len(written) == 2)
        reader.feed(
            encode_v2_response(
                RpcV2Response(request_id=written[1].request_id, ok=True, result={})
            )
        )

        assert (await current).ok is True
        reader.eof()
        await run_task

    asyncio.run(scenario())
