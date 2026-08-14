"""Evidence-binding tests for the Harness RPC v2 adapter."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from evopi.harness import ConfirmationBroker, InMemoryConfirmationStore
from evopi.harness.confirmation import ConfirmationRequest
from evopi.rpc import HarnessRpcHost, RpcHostError
from evopi.rpc.harness_host_v2 import HarnessRpcV2Host


@dataclass(frozen=True, slots=True, kw_only=True)
class _Receipt:
    input_id: str
    run_id: str
    kind: str
    origin: str
    position: int


@dataclass(frozen=True, slots=True, kw_only=True)
class _Snapshot:
    steering_mode: str = "one-at-a-time"
    follow_up_mode: str = "one-at-a-time"
    pending_steering_count: int = 0
    pending_follow_up_count: int = 0


class FakeHarness:
    def __init__(self, broker: ConfirmationBroker) -> None:
        self.confirmation_broker = broker
        self.session = SimpleNamespace(session_id="session-1")
        self.state = SimpleNamespace(status="running")
        self.last_run = None
        self.capabilities = SimpleNamespace(active_tool_names=(), policy_names=())
        self.interaction_snapshot = _Snapshot()
        self.steered: list[str] = []
        self.aborted = False
        self._listeners: list[Any] = []
        self._release = asyncio.Event()

    def subscribe(self, listener: Any) -> Any:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    async def prompt(self, content: str) -> None:
        del content
        from evopi.core.events import CoreEvent

        event = CoreEvent(type="agent_start", run_id="run-1")
        for listener in tuple(self._listeners):
            result = listener(event)
            if inspect.isawaitable(result):
                await result
        await self._release.wait()

    async def steer(self, content: str, *, origin: str = "api") -> _Receipt:
        self.steered.append(content)
        return _Receipt(
            input_id="input-1",
            run_id="run-1",
            kind="steer",
            origin=origin,
            position=1,
        )

    async def follow_up(self, content: str, *, origin: str = "api") -> _Receipt:
        del content
        return _Receipt(
            input_id="input-2",
            run_id="run-1",
            kind="follow_up",
            origin=origin,
            position=1,
        )

    def abort(self) -> None:
        self.aborted = True
        self._release.set()

    def close(self) -> None:
        self._release.set()


async def _started_host() -> tuple[HarnessRpcV2Host, FakeHarness]:
    broker = ConfirmationBroker(InMemoryConfirmationStore())
    harness = FakeHarness(broker)
    legacy = HarnessRpcHost(harness, broker)
    host = HarnessRpcV2Host(legacy)
    result = await host.run_start({"prompt": "hello"})
    assert result == {"run_id": "run-1", "start_sequence": 1}
    return host, harness


def test_v2_initialize_binds_stream_and_snapshot() -> None:
    async def scenario() -> None:
        broker = ConfirmationBroker(InMemoryConfirmationStore())
        harness = FakeHarness(broker)
        host = HarnessRpcV2Host(HarnessRpcHost(harness, broker))

        result = await host.initialize(
            {"client_name": "tests", "client_version": "1.0"}
        )

        assert result["protocol"] == "evopi.rpc.v2"
        assert result["schema_version"] == 2
        assert result["stream"]["stream_id"] == host.stream_id
        assert result["stream"]["cursor"] == 0
        await host.close()

    asyncio.run(scenario())


def test_v2_stale_run_handle_cannot_steer_or_abort() -> None:
    async def scenario() -> None:
        host, harness = await _started_host()

        with pytest.raises(RpcHostError) as steer_error:
            await host.run_steer({"run_id": "old-run", "content": "continue"})
        with pytest.raises(RpcHostError) as abort_error:
            await host.run_abort({"run_id": "old-run"})

        assert steer_error.value.code == "run_mismatch"
        assert abort_error.value.code == "run_mismatch"
        assert harness.steered == []
        assert harness.aborted is False
        harness.abort()
        await host.close()

    asyncio.run(scenario())


def test_v2_replay_rejects_foreign_and_future_cursors() -> None:
    async def scenario() -> None:
        host, harness = await _started_host()

        with pytest.raises(RpcHostError) as foreign:
            await host.events_replay(
                {"stream_id": "foreign-stream", "after_sequence": 0}
            )
        with pytest.raises(RpcHostError) as future:
            await host.events_replay(
                {"stream_id": host.stream_id, "after_sequence": 999}
            )

        assert foreign.value.code == "event_stream_mismatch"
        assert future.value.code == "event_cursor_invalid"
        harness.abort()
        await host.close()

    asyncio.run(scenario())


def test_v2_confirmation_response_requires_observed_revision() -> None:
    async def scenario() -> None:
        broker = ConfirmationBroker(InMemoryConfirmationStore())
        harness = FakeHarness(broker)
        host = HarnessRpcV2Host(HarnessRpcHost(harness, broker))
        request = ConfirmationRequest(hook="before_tool_call", reason="test")
        waiting = asyncio.create_task(broker.request(request))
        await asyncio.sleep(0)

        with pytest.raises(RpcHostError) as stale:
            await host.confirmation_respond(
                {
                    "request_id": request.id,
                    "expected_revision": 2,
                    "decision": "approve",
                    "reason": "",
                    "metadata": {},
                }
            )

        assert stale.value.code == "stale_revision"
        assert broker.list_pending()[0].revision == 1
        await host.confirmation_respond(
            {
                "request_id": request.id,
                "expected_revision": 1,
                "decision": "deny",
                "reason": "not now",
                "metadata": {},
            }
        )
        assert (await waiting).decision == "deny"
        await host.close()

    asyncio.run(scenario())


def test_v2_confirmation_batch_rejects_revision_drift_before_any_transition() -> None:
    async def scenario() -> None:
        broker = ConfirmationBroker(InMemoryConfirmationStore())
        harness = FakeHarness(broker)
        host = HarnessRpcV2Host(HarnessRpcHost(harness, broker))
        first = ConfirmationRequest(hook="before_tool_call", reason="first")
        second = ConfirmationRequest(hook="before_tool_call", reason="second")
        waiters = [
            asyncio.create_task(broker.request(first)),
            asyncio.create_task(broker.request(second)),
        ]
        await asyncio.sleep(0)

        with pytest.raises(RpcHostError) as stale:
            await host.confirmation_respond_batch(
                {
                    "responses": [
                        {
                            "request_id": first.id,
                            "expected_revision": 1,
                            "decision": "approve",
                            "reason": "",
                            "metadata": {},
                        },
                        {
                            "request_id": second.id,
                            "expected_revision": 2,
                            "decision": "deny",
                            "reason": "",
                            "metadata": {},
                        },
                    ]
                }
            )

        assert stale.value.code == "stale_revision"
        assert [item.revision for item in broker.list_pending()] == [1, 1]
        broker.close()
        await asyncio.gather(*waiters, return_exceptions=True)
        await host.close()

    asyncio.run(scenario())
