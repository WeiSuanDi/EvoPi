"""Tests for the JSONL RPC connection over injected async reader/writer."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from evopi.core.types import JsonObject
from evopi.rpc import (
    RpcConnectionClosedError,
    RpcConnectionProtocolError,
    RpcEvent,
    RpcRequest,
    RpcResponse,
    decode_event,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)
from evopi.rpc.connection import JsonlRpcConnection
from evopi.rpc.server import RpcServer

from test_server import FakeHost

_ID = "11111111-2222-4333-8444-555555555555"


class FakeReader:
    def __init__(self, lines: list[str] | None = None) -> None:
        self._lines: deque[str] = deque(lines or [])

    async def readline(self) -> str:
        if not self._lines:
            return ""
        return self._lines.popleft()


class FakeWriter:
    """Records writes; deliberately has no close() to prove streams are caller-owned."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.flushes = 0

    async def write(self, text: str) -> None:
        self.lines.append(text)

    async def flush(self) -> None:
        self.flushes += 1


class BlockingReader:
    """Reader that blocks until released; EOF when released with no lines."""

    def __init__(self) -> None:
        self._lines: deque[str] = deque()
        self._released = asyncio.Event()

    async def readline(self) -> str:
        if not self._lines:
            await self._released.wait()
        return self._lines.popleft() if self._lines else ""

    def release(self) -> None:
        self._released.set()


def _request(method: str, params: JsonObject | None = None, request_id: str = _ID) -> RpcRequest:
    return RpcRequest(request_id=request_id, method=method, params=params if params is not None else {})


def test_request_response_round_trip_through_connection() -> None:
    async def scenario() -> None:
        server = RpcServer(FakeHost())
        reader = FakeReader([encode_request(_request("runtime.status"))])
        writer = FakeWriter()
        connection = JsonlRpcConnection(reader, writer, server)
        await connection.run()
        assert len(writer.lines) == 1
        response = decode_response(writer.lines[0].rstrip("\n"))
        assert response.ok is True
        assert response.request_id == _ID

    asyncio.run(scenario())


def test_out_of_order_completion_keeps_whole_records() -> None:
    async def scenario() -> None:
        host = FakeHost()
        host.delays["run.start"] = 0.05
        server = RpcServer(host)
        reader = FakeReader(
            [
                encode_request(_request("run.start", request_id="slow-1")),
                encode_request(_request("run.abort", request_id="fast-2")),
            ]
        )
        writer = FakeWriter()
        connection = JsonlRpcConnection(reader, writer, server)
        await connection.run()
        assert len(writer.lines) == 2
        first = decode_response(writer.lines[0].rstrip("\n"))
        second = decode_response(writer.lines[1].rstrip("\n"))
        assert first.request_id == "fast-2"  # completed first
        assert second.request_id == "slow-1"

    asyncio.run(scenario())


def test_event_multiplexing_between_responses() -> None:
    async def scenario() -> None:
        host = FakeHost()
        host.delays["run.start"] = 0.05
        server = RpcServer(host)
        reader = FakeReader([encode_request(_request("run.start", request_id="slow-1"))])
        writer = FakeWriter()
        connection = JsonlRpcConnection(reader, writer, server)
        run_task = asyncio.create_task(connection.run())
        await asyncio.sleep(0.01)  # let the request dispatch and start running
        event = RpcEvent(
            event_id=str(uuid4()),
            sequence=1,
            type="agent_start",
            data={"i": 1},
            run_id=None,
            created_at=datetime(2026, 8, 4, tzinfo=UTC),
        )
        await connection.publish_event(event)
        await run_task
        assert len(writer.lines) == 2
        decoded_event = decode_event(writer.lines[0].rstrip("\n"))
        decoded_response = decode_response(writer.lines[1].rstrip("\n"))
        assert decoded_event == event
        assert decoded_response.request_id == "slow-1"

    asyncio.run(scenario())


def test_duplicate_request_ids_through_connection() -> None:
    async def scenario() -> None:
        host = FakeHost()
        host.delays["run.start"] = 0.05
        server = RpcServer(host)
        reader = FakeReader(
            [
                encode_request(_request("run.start", request_id="dup-1")),
                encode_request(_request("run.start", request_id="dup-1")),
            ]
        )
        writer = FakeWriter()
        connection = JsonlRpcConnection(reader, writer, server)
        await connection.run()
        assert len(writer.lines) == 2
        responses = [decode_response(line.rstrip("\n")) for line in writer.lines]
        assert sorted(r.error.code for r in responses if not r.ok and r.error) == ["duplicate_request"]
        assert len(host.calls) == 1

    asyncio.run(scenario())


def test_protocol_invalid_request_gets_response_and_connection_continues() -> None:
    async def scenario() -> None:
        server = RpcServer(FakeHost())
        reader = FakeReader(
            [
                '{"request_id":"bad-1","method":"x","params":{},"schema_version":2}',
                encode_request(_request("runtime.status", request_id="ok-2")),
            ]
        )
        writer = FakeWriter()
        connection = JsonlRpcConnection(reader, writer, server)
        await connection.run()
        assert len(writer.lines) == 2
        error_response = decode_response(writer.lines[0].rstrip("\n"))
        assert error_response.ok is False
        assert error_response.error is not None
        assert error_response.error.code == "invalid_request"
        assert error_response.request_id == "bad-1"
        ok_response = decode_response(writer.lines[1].rstrip("\n"))
        assert ok_response.ok is True
        assert ok_response.request_id == "ok-2"

    asyncio.run(scenario())


def test_unparseable_line_causes_clean_connection_failure() -> None:
    async def scenario() -> None:
        server = RpcServer(FakeHost())
        reader = FakeReader(["this is not json at all\n"])
        writer = FakeWriter()
        connection = JsonlRpcConnection(reader, writer, server)
        with pytest.raises(RpcConnectionProtocolError):
            await connection.run()
        assert connection.closed is True
        assert writer.lines == []

    asyncio.run(scenario())


def test_blank_lines_are_tolerated() -> None:
    async def scenario() -> None:
        server = RpcServer(FakeHost())
        reader = FakeReader(["\n", "   \n", encode_request(_request("runtime.status"))])
        writer = FakeWriter()
        connection = JsonlRpcConnection(reader, writer, server)
        await connection.run()
        assert len(writer.lines) == 1
        assert decode_response(writer.lines[0].rstrip("\n")).ok is True

    asyncio.run(scenario())


def test_send_request_writes_and_awaits_matching_response() -> None:
    async def scenario() -> None:
        server = RpcServer(FakeHost())
        writer = FakeWriter()

        class EchoReader:
            """Waits for the first request line, echoes one matching response, then EOF."""

            def __init__(self) -> None:
                self._echoed = False

            async def readline(self) -> str:
                while not writer.lines:
                    await asyncio.sleep(0.01)
                if self._echoed:
                    return ""
                self._echoed = True
                sent = decode_request(writer.lines[-1].rstrip("\n"))
                return encode_response(
                    RpcResponse(request_id=sent.request_id, ok=True, result={"echo": sent.method})
                )

        connection = JsonlRpcConnection(EchoReader(), writer, server)
        run_task = asyncio.create_task(connection.run())
        response = await asyncio.wait_for(connection.send_request("runtime.status", {"n": 1}), timeout=2.0)
        await run_task
        assert response.ok is True
        assert response.result == {"echo": "runtime.status"}
        sent = decode_request(writer.lines[0].rstrip("\n"))
        assert sent.method == "runtime.status"
        assert sent.params == {"n": 1}

    asyncio.run(scenario())


def test_pending_send_resolved_with_connection_closed_on_shutdown() -> None:
    async def scenario() -> None:
        server = RpcServer(FakeHost())
        reader = BlockingReader()
        writer = FakeWriter()
        connection = JsonlRpcConnection(reader, writer, server)
        run_task = asyncio.create_task(connection.run())
        send_task = asyncio.create_task(connection.send_request("runtime.status", {}))
        await asyncio.sleep(0.01)  # send is pending; reader is blocked
        await connection.close()
        response = await asyncio.wait_for(send_task, timeout=2.0)
        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "connection_closed"
        with pytest.raises(RpcConnectionClosedError):
            await connection.send_request("runtime.status", {})
        reader.release()
        await run_task

    asyncio.run(scenario())


def test_eof_ends_run_and_rejects_further_sends() -> None:
    async def scenario() -> None:
        server = RpcServer(FakeHost())
        reader = FakeReader([])
        writer = FakeWriter()
        connection = JsonlRpcConnection(reader, writer, server)
        await connection.run()
        assert connection.closed is True
        with pytest.raises(RpcConnectionClosedError):
            await connection.send_request("runtime.status", {})
        with pytest.raises(RpcConnectionClosedError):
            await connection.publish_event(_rpc_event(1))

    asyncio.run(scenario())


def test_close_is_idempotent_and_cleans_up_dispatch_tasks() -> None:
    async def scenario() -> None:
        host = FakeHost()
        host.delays["run.start"] = 5.0
        server = RpcServer(host)
        reader = BlockingReader()
        writer = FakeWriter()
        connection = JsonlRpcConnection(reader, writer, server)
        reader._lines.append(encode_request(_request("run.start", request_id="slow-1")))
        baseline = set(asyncio.all_tasks())
        run_task = asyncio.create_task(connection.run())
        await asyncio.sleep(0.01)  # dispatch is now running (slow host)
        await connection.close()
        await connection.close()
        reader.release()
        await asyncio.wait_for(run_task, timeout=2.0)
        await asyncio.sleep(0.01)
        assert connection.closed is True
        assert set(asyncio.all_tasks()) <= baseline  # no leaked tasks
        assert writer.lines == []  # the cancelled dispatch never wrote

    asyncio.run(scenario())


def test_never_closes_caller_owned_streams() -> None:
    async def scenario() -> None:
        server = RpcServer(FakeHost())
        reader = FakeReader([encode_request(_request("runtime.status"))])
        writer = FakeWriter()  # no .close() method: an attempt would raise AttributeError
        connection = JsonlRpcConnection(reader, writer, server)
        await connection.close()
        # run() after close is a clean no-op; streams stay untouched.
        await connection.run()
        assert connection.closed is True
        assert writer.lines == []

    asyncio.run(scenario())


def _rpc_event(sequence: int) -> RpcEvent:
    return RpcEvent(
        event_id=str(uuid4()),
        sequence=sequence,
        type="agent_start",
        data={},
        run_id=None,
        created_at=datetime(2026, 8, 4, tzinfo=UTC),
    )
