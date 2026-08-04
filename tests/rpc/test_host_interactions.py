"""Host-level tests for the interaction RPC surface (SFU-2 Task 1).

``HarnessRpcHost`` binds a structural stand-in for the Lane 1 interaction
surface (a fake Harness with the frozen receipt/snapshot shapes and the
frozen error class names). The real BaseHarness binding is proven in
Integration; these tests pin the Host behavior against the frozen contract.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import asdict, dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from evopi.core.events import CoreEvent
from evopi.core.types import JsonObject
from evopi.harness import ConfirmationBroker, InMemoryConfirmationStore
from evopi.rpc import HarnessRpcHost, RpcHostError, RpcRequest, RpcServer


class InteractionError(Exception):
    """Frozen base class name (CONTEXT.md section 3); Lane 1 defines the real class."""


class InteractionQueueClosedError(InteractionError):
    pass


class InteractionQueueFullError(InteractionError):
    pass


class InteractionContentError(InteractionError):
    pass


class InteractionContentTooLargeError(InteractionContentError):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class FakeReceipt:
    input_id: str
    run_id: str
    kind: str
    origin: str
    position: int


@dataclass(frozen=True, slots=True, kw_only=True)
class FakeSnapshot:
    steering_mode: str = "one-at-a-time"
    follow_up_mode: str = "all"
    pending_steering_count: int = 0
    pending_follow_up_count: int = 0


class FakeHarness:
    """Structural stand-in for the Lane 1 interaction-enabled BaseHarness."""

    def __init__(self, broker: ConfirmationBroker) -> None:
        self.confirmation_broker = broker
        self.session = SimpleNamespace(session_id="session-1")
        self.state = SimpleNamespace(status="running")
        self.last_run = None
        self.capabilities = SimpleNamespace(active_tool_names=(), policy_names=())
        self.prompted: list[str] = []
        self.steered: list[tuple[str, str]] = []
        self.followed_up: list[tuple[str, str]] = []
        self.snapshot = FakeSnapshot(pending_steering_count=2, pending_follow_up_count=1)
        self.queue_full = False
        self.queue_closed = False
        self.aborted = False
        self.closed = False
        self._listeners: list[Any] = []
        self._prompt_release = asyncio.Event()

    def subscribe(self, listener: Any) -> Any:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    async def prompt(self, content: str) -> None:
        self.prompted.append(content)
        await self._emit(CoreEvent(type="agent_start", run_id="run-1", data={}))
        await self._prompt_release.wait()

    async def steer(self, content: str, *, origin: str = "api") -> FakeReceipt:
        self.steered.append((content, origin))
        if not content.strip():
            raise InteractionContentError("content must be non-whitespace text")
        if len(content) > 100:
            raise InteractionContentTooLargeError("content exceeds the size limit")
        if self.queue_full:
            raise InteractionQueueFullError("queue is full")
        if self.queue_closed:
            raise InteractionQueueClosedError("queue is closed")
        await self._emit(
            CoreEvent(
                type="interaction_queued",
                run_id="run-1",
                data={"input_id": "input-1", "kind": "steer", "position": 1},
            )
        )
        return FakeReceipt(
            input_id="input-1",
            run_id="run-1",
            kind="steer",
            origin=origin,
            position=1,
        )

    async def follow_up(self, content: str, *, origin: str = "api") -> FakeReceipt:
        self.followed_up.append((content, origin))
        if self.queue_closed:
            raise InteractionQueueClosedError("queue is closed")
        return FakeReceipt(
            input_id="input-2",
            run_id="run-1",
            kind="follow_up",
            origin=origin,
            position=2,
        )

    @property
    def interaction_snapshot(self) -> FakeSnapshot:
        return self.snapshot

    def abort(self) -> None:
        self.aborted = True
        self._prompt_release.set()

    def close(self) -> None:
        self.closed = True
        self._prompt_release.set()

    async def _emit(self, event: CoreEvent) -> None:
        # The real Harness dispatches sync and async listeners alike
        # (CoreEvent.notify); mirror that here.
        for listener in list(self._listeners):
            result = listener(event)
            if inspect.isawaitable(result):
                await result


def _host(harness: FakeHarness) -> HarnessRpcHost:
    return HarnessRpcHost(harness, harness.confirmation_broker)


def _make_harness() -> tuple[FakeHarness, ConfirmationBroker]:
    broker = ConfirmationBroker(InMemoryConfirmationStore())
    return FakeHarness(broker), broker


async def _start_run(host: HarnessRpcHost, harness: FakeHarness) -> None:
    result = await host.run_start({"prompt": "hello"})
    assert result == {"run_id": "run-1"}
    assert harness.prompted == ["hello"]


def test_steer_active_run_returns_exact_receipt() -> None:
    async def scenario() -> None:
        harness, _ = _make_harness()
        host = _host(harness)
        await _start_run(host, harness)

        result = await host.run_steer({"content": "keep going"})

        assert result == {
            "input_id": "input-1",
            "kind": "steer",
            "run_id": "run-1",
            "position": 1,
        }
        assert harness.steered == [("keep going", "rpc")]
        harness.abort()
        await host.close()

    asyncio.run(scenario())


def test_follow_up_active_run_returns_exact_receipt() -> None:
    async def scenario() -> None:
        harness, _ = _make_harness()
        host = _host(harness)
        await _start_run(host, harness)

        result = await host.run_follow_up({"content": "one more"})

        assert result == {
            "input_id": "input-2",
            "kind": "follow_up",
            "run_id": "run-1",
            "position": 2,
        }
        assert harness.followed_up == [("one more", "rpc")]
        harness.abort()
        await host.close()

    asyncio.run(scenario())


def test_interaction_while_idle_returns_run_not_active() -> None:
    async def scenario() -> None:
        harness, _ = _make_harness()
        host = _host(harness)
        for method in (host.run_steer, host.run_follow_up):
            with pytest.raises(RpcHostError) as excinfo:
                await method({"content": "hello"})
            assert excinfo.value.code == "run_not_active"
        assert harness.steered == []
        assert harness.followed_up == []
        await host.close()

    asyncio.run(scenario())


def test_interaction_after_closed_host_returns_host_closed() -> None:
    async def scenario() -> None:
        harness, _ = _make_harness()
        host = _host(harness)
        await _start_run(host, harness)
        await host.close()
        with pytest.raises(RpcHostError) as excinfo:
            await host.run_steer({"content": "hello"})
        assert excinfo.value.code == "host_closed"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("params", "expected_code"),
    [
        ({"content": "   "}, "interaction_content_invalid"),
        ({"content": "x" * 101}, "interaction_content_too_large"),
    ],
)
def test_content_errors_map_to_frozen_codes(
    params: JsonObject,
    expected_code: str,
) -> None:
    async def scenario() -> None:
        harness, _ = _make_harness()
        host = _host(harness)
        await _start_run(host, harness)
        with pytest.raises(RpcHostError) as excinfo:
            await host.run_steer(params)
        assert excinfo.value.code == expected_code
        harness.abort()
        await host.close()

    asyncio.run(scenario())


def test_full_queue_maps_to_interaction_queue_full() -> None:
    async def scenario() -> None:
        harness, _ = _make_harness()
        harness.queue_full = True
        host = _host(harness)
        await _start_run(host, harness)
        with pytest.raises(RpcHostError) as excinfo:
            await host.run_steer({"content": "hello"})
        assert excinfo.value.code == "interaction_queue_full"
        harness.abort()
        await host.close()

    asyncio.run(scenario())


def test_closed_queue_maps_to_interaction_closed() -> None:
    async def scenario() -> None:
        harness, _ = _make_harness()
        harness.queue_closed = True
        host = _host(harness)
        await _start_run(host, harness)
        with pytest.raises(RpcHostError) as excinfo:
            await host.run_follow_up({"content": "hello"})
        assert excinfo.value.code == "interaction_closed"
        harness.abort()
        await host.close()

    asyncio.run(scenario())


def test_unmapped_harness_failure_is_redacted_by_server() -> None:
    """The abstract InteractionError has no frozen RPC code; the server redacts it."""

    async def scenario() -> None:
        harness, _ = _make_harness()
        original_steer = harness.steer

        async def failing_steer(content: str, *, origin: str = "api") -> FakeReceipt:
            del content, origin
            raise InteractionError("no frozen code for the abstract base")

        harness.steer = failing_steer  # type: ignore[method-assign]
        try:
            host = _host(harness)
            server = RpcServer(host)
            await _start_run(host, harness)
            response = await server.dispatch(
                RpcRequest(
                    request_id="11111111-2222-4333-8444-555555555555",
                    method="run.steer",
                    params={"content": "hello"},
                )
            )
            assert response.ok is False
            assert response.error is not None
            assert response.error.code == "internal_error"
            assert response.error.message == "internal error"
            harness.abort()
            await host.close()
        finally:
            harness.steer = original_steer  # type: ignore[method-assign]

    asyncio.run(scenario())


class _MinimalFakeHarness(FakeHarness):
    """Fake whose interaction surface is absent (pre-Lane-1 BaseHarness)."""

    @property
    def interaction_snapshot(self) -> FakeSnapshot:
        raise AttributeError("interaction surface not present")

    async def steer(self, content: str, *, origin: str = "api") -> FakeReceipt:
        raise AttributeError("steer not present")


def test_pre_integration_host_keeps_frozen_wire_contract() -> None:
    """Without the Lane 1 surface, initialize/status keep the v1 shapes."""

    async def scenario() -> None:
        broker = ConfirmationBroker(InMemoryConfirmationStore())
        harness = _MinimalFakeHarness(broker)
        host = _host(harness)

        init = await host.initialize({})
        assert "capabilities" not in init
        assert "steering_mode" not in init
        assert "follow_up_mode" not in init

        status = await host.runtime_status({})
        assert "pending_steering_count" not in status
        assert "pending_follow_up_count" not in status
        assert status["active_run_id"] is None

        # Interaction methods fail closed through the redacted server path.
        await _start_run(host, harness)
        server = RpcServer(host)
        response = await server.dispatch(
            RpcRequest(
                request_id="11111111-2222-4333-8444-555555555555",
                method="run.steer",
                params={"content": "hello"},
            )
        )
        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "internal_error"
        await host.close()

    asyncio.run(scenario())


def test_initialize_advertises_interactions_and_modes() -> None:
    async def scenario() -> None:
        harness, _ = _make_harness()
        host = _host(harness)
        result = await host.initialize({})
        assert result["capabilities"] == {"text_steering": True, "text_follow_up": True}
        assert result["steering_mode"] == "one-at-a-time"
        assert result["follow_up_mode"] == "all"
        await host.close()

    asyncio.run(scenario())


def test_runtime_status_shows_modes_and_counts_without_content() -> None:
    async def scenario() -> None:
        harness, _ = _make_harness()
        host = _host(harness)
        await _start_run(host, harness)
        await host.run_steer({"content": "secret-text"})

        status = await host.runtime_status({})

        assert status["steering_mode"] == "one-at-a-time"
        assert status["follow_up_mode"] == "all"
        assert status["pending_steering_count"] == 2
        assert status["pending_follow_up_count"] == 1
        serialized = json.dumps(status)
        assert "secret-text" not in serialized
        harness.abort()
        await host.close()

    asyncio.run(scenario())


def test_interaction_events_flow_through_replay() -> None:
    async def scenario() -> None:
        harness, _ = _make_harness()
        host = _host(harness)
        await _start_run(host, harness)
        await host.run_steer({"content": "hello"})

        result = await host.events_replay({"after_sequence": 0})

        types = [event["type"] for event in result["events"]]
        assert "interaction_queued" in types
        queued = next(
            event for event in result["events"] if event["type"] == "interaction_queued"
        )
        assert queued["run_id"] == "run-1"
        assert queued["data"]["input_id"] == "input-1"
        assert "hello" not in json.dumps(queued)
        harness.abort()
        await host.close()

    asyncio.run(scenario())


def test_full_dispatch_path_maps_frozen_error() -> None:
    """Server + Host + fake Harness: whitespace-only content is rejected."""

    async def scenario() -> None:
        harness, _ = _make_harness()
        host = _host(harness)
        server = RpcServer(host)
        await _start_run(host, harness)

        response = await server.dispatch(
            RpcRequest(
                request_id="11111111-2222-4333-8444-555555555555",
                method="run.steer",
                params={"content": "   "},
            )
        )

        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "interaction_content_invalid"
        assert "   " not in json.dumps(asdict(response.error))
        harness.abort()
        await host.close()

    asyncio.run(scenario())
