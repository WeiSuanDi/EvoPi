"""Typed asynchronous RPC v2 client tests."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

from evopi.core.types import JsonObject
from evopi.core.tool import ToolCall
from evopi.harness.confirmation import ConfirmationRecord, ConfirmationRequest
from evopi.harness.confirmation_codec import encode_record
from evopi.rpc.client import EvoPiRpcClient
from evopi.rpc.client_types import (
    RpcConfirmationAnswer,
    RpcConfirmationRecord,
    RpcRunEvent,
    RpcUnknownEvent,
)
from evopi.rpc.codec_v2 import encode_v2_event
from evopi.rpc.connection_v2 import JsonlRpcV2Connection
from evopi.rpc.protocol_v2 import RpcV2Event
from evopi.rpc.server_v2 import RpcV2Server


class _Reader:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()

    async def readline(self) -> str:
        return await self.queue.get()


class _Writer:
    def __init__(self, peer: _Reader) -> None:
        self.peer = peer

    async def write(self, text: str) -> None:
        self.peer.queue.put_nowait(text)

    async def flush(self) -> None:
        return None


class _ClientHost:
    def __init__(self) -> None:
        self.stream_id = str(uuid4())
        self.connection: JsonlRpcV2Connection | None = None
        self.sequence = 0
        self.events: list[RpcV2Event] = []

    async def initialize(self, params: JsonObject) -> JsonObject:
        return {
            "protocol": "evopi.rpc.v2",
            "schema_version": 2,
            "host_id": str(uuid4()),
            "session_id": "session-1",
            "stream": {
                "stream_id": self.stream_id,
                "cursor": 0,
                "oldest_sequence": 0,
                "latest_sequence": 0,
                "capacity": 100,
            },
            "active_tool_names": ["read_file"],
            "policy_names": ["safe"],
            "capabilities": {
                "event_replay": True,
                "confirmation": True,
                "text_steering": True,
                "text_follow_up": True,
            },
            "steering_mode": "one-at-a-time",
            "follow_up_mode": "one-at-a-time",
        }

    async def runtime_status(self, params: JsonObject) -> JsonObject:
        return {
            "active_run_id": None,
            "lifecycle": "completed",
            "session_id": "session-1",
            "pending_confirmation_count": 0,
            "last_end_reason": "completed",
            "last_run_error": None,
            "steering_mode": "one-at-a-time",
            "follow_up_mode": "one-at-a-time",
            "pending_steering_count": 0,
            "pending_follow_up_count": 0,
        }

    async def run_start(self, params: JsonObject) -> JsonObject:
        run_id = "run-1"
        await self._event("agent_start", {}, run_id)
        await self._event(
            "custom_extension_event",
            {"safe": True},
            run_id,
        )
        await self._event(
            "agent_end",
            {
                "reason": "completed",
                "turns_used": 1,
                "max_turns": 20,
                "messages": [
                    {
                        "id": "assistant-1",
                        "role": "assistant",
                        "content": "done",
                        "created_at": datetime.now(UTC).isoformat(),
                        "metadata": {},
                        "tool_calls": [],
                        "stop_reason": "stop",
                    }
                ],
                "error": None,
                "error_info": None,
            },
            run_id,
        )
        return {"run_id": run_id, "start_sequence": 1}

    async def run_steer(self, params: JsonObject) -> JsonObject:
        return {
            "input_id": "input-1",
            "kind": "steer",
            "run_id": params["run_id"],
            "position": 1,
        }

    async def run_follow_up(self, params: JsonObject) -> JsonObject:
        return {
            "input_id": "input-2",
            "kind": "follow_up",
            "run_id": params["run_id"],
            "position": 1,
        }

    async def run_abort(self, params: JsonObject) -> JsonObject:
        return {"run_id": params["run_id"], "aborted": True}

    async def confirmation_list(self, params: JsonObject) -> JsonObject:
        return {"pending": []}

    async def confirmation_respond(self, params: JsonObject) -> JsonObject:
        return {
            "request_id": params["request_id"],
            "status": "approved",
            "revision": params["expected_revision"] + 1,
        }

    async def confirmation_respond_batch(self, params: JsonObject) -> JsonObject:
        return {"applied": []}

    async def events_replay(self, params: JsonObject) -> JsonObject:
        after_sequence = params["after_sequence"]
        return {
            "stream_id": self.stream_id,
            "after_sequence": after_sequence,
            "oldest_sequence": self.events[0].sequence if self.events else 0,
            "latest_sequence": self.sequence,
            "events": [
                json.loads(encode_v2_event(event))
                for event in self.events
                if event.sequence > after_sequence
            ],
        }

    async def shutdown(self, params: JsonObject) -> JsonObject:
        return {"closed": True}

    async def _event(self, type_: str, data: JsonObject, run_id: str) -> None:
        assert self.connection is not None
        self.sequence += 1
        event = RpcV2Event(
            event_id=str(uuid4()),
            stream_id=self.stream_id,
            sequence=self.sequence,
            type=type_,
            data=data,
            run_id=run_id,
            created_at=datetime.now(UTC),
        )
        self.events.append(event)
        await self.connection.publish_event(event)


class _ConfirmationHost(_ClientHost):
    def __init__(self) -> None:
        super().__init__()
        now = datetime.now(UTC)
        self.record = ConfirmationRecord(
            request=ConfirmationRequest(
                id="confirmation-1",
                hook="before_tool_call",
                reason="shell needs approval",
                risk_level="medium",
                policy_names=("tool_confirmation",),
                tool_call=ToolCall(id="call-1", name="shell_command", arguments={}),
                arguments={},
                run_id="run-1",
                session_id="session-1",
                created_at=now,
            ),
            status="pending",
            runtime_id="runtime-1",
            revision=1,
            updated_at=now,
        )
        self.answered = False

    async def run_start(self, params: JsonObject) -> JsonObject:
        await self._event("agent_start", {}, "run-1")
        await self._event(
            "confirmation_state_changed",
            {"request_id": "confirmation-1", "status": "pending", "revision": 1},
            "run-1",
        )
        return {"run_id": "run-1", "start_sequence": 1}

    async def confirmation_list(self, params: JsonObject) -> JsonObject:
        return {"pending": [encode_record(self.record)] if not self.answered else []}

    async def confirmation_respond(self, params: JsonObject) -> JsonObject:
        assert params["expected_revision"] == 1
        self.answered = True
        await self._event(
            "confirmation_state_changed",
            {"request_id": "confirmation-1", "status": "approved", "revision": 2},
            "run-1",
        )
        await self._event(
            "agent_end",
            {
                "reason": "completed",
                "turns_used": 1,
                "max_turns": 20,
                "messages": [],
                "error": None,
                "error_info": None,
            },
            "run-1",
        )
        return {"request_id": "confirmation-1", "status": "approved", "revision": 2}


async def _connected(
    host: _ClientHost | None = None,
    *,
    confirmation_handler=None,
) -> tuple[EvoPiRpcClient, JsonlRpcV2Connection, asyncio.Task[None]]:
    client_reader = _Reader()
    server_reader = _Reader()
    host = host or _ClientHost()
    server_connection = JsonlRpcV2Connection(
        server_reader,
        _Writer(client_reader),
        RpcV2Server(host),
    )
    host.connection = server_connection
    server_task = asyncio.create_task(server_connection.run())
    client = await EvoPiRpcClient.connect(
        client_reader,
        _Writer(server_reader),
        confirmation_handler=confirmation_handler,
    )
    return client, server_connection, server_task


async def _cleanup(
    client: EvoPiRpcClient,
    server: JsonlRpcV2Connection,
    server_task: asyncio.Task[None],
) -> None:
    await client.aclose()
    await server.close()
    server_task.cancel()
    await asyncio.gather(server_task, return_exceptions=True)


def test_client_initializes_and_builds_run_result_after_fast_completion() -> None:
    async def scenario() -> None:
        client, server, server_task = await _connected()

        assert client.server_info.session_id == "session-1"
        run = await client.start_run("hello")
        result = await run.wait()

        assert result.run_id == "run-1"
        assert result.end_reason == "completed"
        assert result.final_assistant is not None
        assert result.final_assistant.content == "done"
        events = [event async for event in run.events()]
        assert isinstance(events[0], RpcRunEvent)
        assert isinstance(events[1], RpcUnknownEvent)
        await _cleanup(client, server, server_task)

    asyncio.run(scenario())


def test_run_wait_is_repeatable_and_interactions_keep_run_identity() -> None:
    async def scenario() -> None:
        client, server, server_task = await _connected()
        run = await client.start_run("hello")

        first, second = await asyncio.gather(run.wait(), run.wait())
        receipt = await run.steer("adjust")

        assert first is second
        assert receipt.run_id == run.run_id
        await _cleanup(client, server, server_task)

    asyncio.run(scenario())


def test_multiple_event_consumers_receive_independent_ordered_streams() -> None:
    async def scenario() -> None:
        client, server, server_task = await _connected()
        run = await client.start_run("hello")

        async def collect() -> list[int]:
            return [event.cursor.sequence async for event in run.events()]

        first, second = await asyncio.gather(collect(), collect())

        assert first == [1, 2, 3]
        assert second == first
        await _cleanup(client, server, server_task)

    asyncio.run(scenario())


def test_external_transport_is_not_closed_by_client() -> None:
    async def scenario() -> None:
        client, server, server_task = await _connected()
        writer = client._writer  # noqa: SLF001 - transport ownership assertion

        await client.aclose()

        assert not hasattr(writer, "closed")
        await server.close()
        server_task.cancel()
        await asyncio.gather(server_task, return_exceptions=True)

    asyncio.run(scenario())


def test_async_confirmation_handler_uses_observed_revision() -> None:
    async def scenario() -> None:
        observed: list[RpcConfirmationRecord] = []

        async def approve(record: RpcConfirmationRecord) -> RpcConfirmationAnswer:
            observed.append(record)
            return RpcConfirmationAnswer(
                request_id=record.request_id,
                expected_revision=record.revision,
                decision="approve",
            )

        host = _ConfirmationHost()
        client, server, server_task = await _connected(
            host,
            confirmation_handler=approve,
        )
        result = await (await client.start_run("confirm")).wait()

        assert result.end_reason == "completed"
        assert [item.revision for item in observed] == [1]
        assert host.answered is True
        await _cleanup(client, server, server_task)

    asyncio.run(scenario())


def test_confirmation_handler_failure_stays_pending_and_is_observable() -> None:
    async def scenario() -> None:
        async def broken(record: RpcConfirmationRecord) -> RpcConfirmationAnswer:
            raise RuntimeError(record.request_id)

        host = _ConfirmationHost()
        client, server, server_task = await _connected(
            host,
            confirmation_handler=broken,
        )
        run = await client.start_run("confirm")
        error = await anext(client.background_errors())
        pending = await client.list_confirmations()

        assert error.code == "client_error"
        assert [item.request_id for item in pending] == ["confirmation-1"]
        await client.respond_confirmation(
            RpcConfirmationAnswer(
                request_id="confirmation-1",
                expected_revision=1,
                decision="approve",
            )
        )
        assert (await run.wait()).end_reason == "completed"
        await _cleanup(client, server, server_task)

    asyncio.run(scenario())


def test_confirmation_handler_none_defers_to_manual_response() -> None:
    async def scenario() -> None:
        called = asyncio.Event()

        async def defer(record: RpcConfirmationRecord) -> None:
            assert record.request_id == "confirmation-1"
            called.set()
            return None

        host = _ConfirmationHost()
        client, server, server_task = await _connected(
            host,
            confirmation_handler=defer,
        )
        run = await client.start_run("confirm")
        await asyncio.wait_for(called.wait(), timeout=1)
        assert await client.list_confirmations()
        assert host.answered is False

        await client.respond_confirmation(
            RpcConfirmationAnswer(
                request_id="confirmation-1",
                expected_revision=1,
                decision="approve",
            )
        )
        assert (await run.wait()).end_reason == "completed"
        await _cleanup(client, server, server_task)

    asyncio.run(scenario())
