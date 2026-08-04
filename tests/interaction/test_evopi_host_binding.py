"""Bind the independent host-interaction kit to EvoPi production components."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent
from evopi.core.messages import AssistantMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.core.tool import Tool, ToolCall
from evopi.harness import BaseHarness, ConfirmationBroker, InMemoryConfirmationStore
from evopi.policy.decisions import PolicyDecision
from evopi.policy.types import HookName, PolicyContext, RiskLevel
from evopi.rpc import (
    EventCursorExpiredError,
    EventSubscriberDroppedError,
    EventStream,
    HarnessRpcHost,
    RpcCodecError,
    RpcEventDataError,
    RpcRequest as ProductionRequest,
    RpcResponse as ProductionResponse,
    RpcServer,
    decode_event,
    decode_request,
    decode_response,
    encode_event,
    encode_response,
)
from evopi.rpc.protocol import RpcEvent as ProductionEvent

from .conformance import (
    RPC_SCENARIOS,
    DispatchedCall,
    ProtocolViolationError,
    ReplayResult,
    RpcErrorInfo,
    RpcEvent,
    RpcRequest,
    RpcResponse,
    WireError,
    WireResult,
)


class _BlockingModel:
    name = "blocking"
    provider = "test"

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        await asyncio.Event().wait()
        yield ModelComplete(
            message=AssistantMessage(content="unreachable", stop_reason="stop")
        )


class _ScriptedModel:
    name = "scripted"
    provider = "test"

    def __init__(self) -> None:
        self._messages = iter(
            [
                AssistantMessage(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            name="echo",
                            arguments={"value": "original"},
                        )
                    ],
                    stop_reason="tool_use",
                ),
                AssistantMessage(content="done", stop_reason="stop"),
            ]
        )

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        yield ModelComplete(message=next(self._messages))


@dataclass(slots=True)
class _ConfirmEcho:
    name: str = "confirm_echo"
    version: str = "1"
    description: str = "Require confirmation before echo"
    hooks: tuple[HookName, ...] = ("before_tool_call",)
    priority: int = 10
    enabled: bool = True
    source: str = "test"
    risk_level: RiskLevel = "medium"
    metadata: dict[str, Any] = field(default_factory=dict)

    def run(self, context: PolicyContext) -> PolicyDecision:
        if context.tool_call is None or context.tool_call.name != "echo":
            return PolicyDecision()
        return PolicyDecision(
            action="require_confirmation",
            reason="Echo requires approval",
            risk_level="medium",
            rewritten_args={"value": "rewritten"},
        )


class _ArmedHarnessRpcHost(HarnessRpcHost):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._armed: set[str] = set()

    def arm_failure(self, method: str) -> None:
        self._armed.add(method)

    async def runtime_status(self, params: dict[str, Any]) -> dict[str, Any]:
        if "runtime.status" in self._armed:
            self._armed.remove("runtime.status")
            raise RuntimeError("boom kit-redact-secret")
        return await super().runtime_status(params)


def _kit_event(event: ProductionEvent) -> RpcEvent:
    return RpcEvent(
        event_id=event.event_id,
        sequence=event.sequence,
        type=event.type,
        data=dict(event.data),
        run_id=event.run_id,
        created_at=event.created_at,
        schema_version=event.schema_version,
    )


def _kit_response(response: ProductionResponse) -> RpcResponse:
    error = response.error
    return RpcResponse(
        request_id=response.request_id,
        ok=response.ok,
        result=response.result,
        error=(
            RpcErrorInfo(
                code=error.code,
                message=error.message,
                details=dict(error.details),
            )
            if error is not None
            else None
        ),
        schema_version=response.schema_version,
    )


def _wire_error(exc: Exception, *, line: str | None = None) -> WireError:
    message = str(exc)
    cause = str(exc.__cause__ or "")
    text = f"{message} {cause}".lower()
    payload: Any = None
    if line is not None:
        try:
            payload = json.loads(line)
        except (TypeError, ValueError):
            pass
    if "duplicate json key" in text:
        code = "duplicate_json_key"
    elif (
        "non-json constant" in text
        or "non-finite" in text
        or "out of range float" in text
    ):
        code = "non_finite_number"
    elif "invalid json" in text:
        code = "malformed_json"
    elif "schema version" in text:
        code = "invalid_schema_version"
    elif "timestamp" in text:
        code = "invalid_timestamp"
    elif "event id" in text or "event sequence" in text:
        code = "invalid_event"
    elif "non-empty string" in text:
        if isinstance(payload, dict) and "sequence" in payload:
            code = "invalid_event"
        elif isinstance(payload, dict) and "ok" in payload:
            code = "invalid_response"
        else:
            code = "invalid_envelope"
    elif "unsupported" in text or "not json serializable" in text:
        code = "unsupported_value"
    elif "params must be an object" in text:
        code = "invalid_params"
    elif "data must be an object" in text:
        code = "invalid_data"
    elif "unrecognized or malformed envelope" in text:
        code = "invalid_envelope"
        if isinstance(payload, dict):
            keys = set(payload)
            shapes = (
                {"request_id", "method", "params", "schema_version"},
                {"request_id", "ok", "result", "error", "schema_version"},
                {
                    "event_id",
                    "sequence",
                    "type",
                    "data",
                    "run_id",
                    "created_at",
                    "schema_version",
                },
            )
            if any(required < keys for required in shapes):
                code = "invalid_envelope_key"
    elif "response" in text or "error" in text:
        code = "invalid_response"
    else:
        code = "invalid_envelope"
    return WireError(code=code, message="invalid wire value", details={})


class _Subscriber:
    def __init__(self, iterator: AsyncIterator[ProductionEvent]) -> None:
        self._iterator = iterator
        self._failure: WireError | None = None

    async def next_event(self) -> RpcEvent | None:
        try:
            return _kit_event(await anext(self._iterator))
        except StopAsyncIteration:
            return None
        except EventSubscriberDroppedError:
            self._failure = WireError(
                code="subscriber_queue_overflow",
                message="subscriber queue overflow",
                details={},
            )
            return None

    def failure(self) -> WireError | None:
        return self._failure

    async def close(self) -> None:
        close = getattr(self._iterator, "aclose", None)
        if close is not None:
            await close()


class _ProductionRpcAdapter:
    retained_capacity = 8

    def __init__(self, *, subscriber_queue_capacity: int = 64) -> None:
        broker = ConfirmationBroker(InMemoryConfirmationStore())
        harness = BaseHarness(
            model=_BlockingModel(),
            confirmation_broker=broker,
            max_turns=2,
        )
        self._events = EventStream(
            capacity=self.retained_capacity,
            subscriber_queue_capacity=subscriber_queue_capacity,
        )
        self._host = _ArmedHarnessRpcHost(harness, broker, event_stream=self._events)
        self._server = RpcServer(self._host)
        self._dispatched: list[DispatchedCall] = []
        self._seen: set[str] = set()

    async def publish(self, type_: str, data: dict[str, Any]) -> RpcEvent:
        try:
            event = self._events.publish(
                CoreEvent(type=cast(Any, type_), data=data)
            )
        except (RpcCodecError, RpcEventDataError) as exc:
            error = _wire_error(exc)
            if error.code == "invalid_envelope":
                error = WireError(
                    code="invalid_event",
                    message=error.message,
                    details=error.details,
                )
            raise ProtocolViolationError(error) from None
        return _kit_event(event)

    def replay(self, *, after_sequence: int) -> ReplayResult:
        try:
            events = self._events.replay(after_sequence=after_sequence)
        except EventCursorExpiredError:
            return ReplayResult(
                ok=False,
                error=WireError(
                    code="event_cursor_expired",
                    message="event cursor expired",
                    details={},
                ),
            )
        return ReplayResult(ok=True, events=tuple(_kit_event(event) for event in events))

    async def subscribe(
        self,
        *,
        after_sequence: int,
        max_queue: int = 64,
    ) -> _Subscriber:
        try:
            iterator = await self._events.subscribe(after_sequence=after_sequence)
        except EventCursorExpiredError:
            raise ProtocolViolationError(
                WireError(
                    code="event_cursor_expired",
                    message="event cursor expired",
                    details={},
                )
            ) from None
        return _Subscriber(iterator)

    async def call(self, request: RpcRequest) -> RpcResponse:
        if request.request_id not in self._seen:
            self._seen.add(request.request_id)
            self._dispatched.append(
                DispatchedCall(
                    request_id=request.request_id,
                    method=request.method,
                    params=dict(request.params),
                )
            )
        response = await self._server.dispatch(
            ProductionRequest(
                request_id=request.request_id,
                method=request.method,
                params=dict(request.params),
                schema_version=request.schema_version,
            )
        )
        return _kit_response(response)

    async def send_wire(self, line: str) -> WireResult:
        try:
            request = decode_request(line)
        except RpcCodecError as exc:
            return WireResult(ok=False, error=_wire_error(exc, line=line))
        response = await self.call(
            RpcRequest(
                request_id=request.request_id,
                method=request.method,
                params=dict(request.params),
                schema_version=request.schema_version,
            )
        )
        if not response.ok and response.error is not None:
            return WireResult(
                ok=False,
                error=WireError(
                    code=response.error.code,
                    message=response.error.message,
                    details=response.error.details,
                ),
            )
        return WireResult(ok=True, response=response)

    def event_wire(self, event: RpcEvent) -> str | WireError:
        try:
            return encode_event(
                ProductionEvent(
                    event_id=event.event_id,
                    sequence=event.sequence,
                    type=event.type,
                    data=dict(event.data),
                    run_id=event.run_id,
                    created_at=event.created_at,
                    schema_version=event.schema_version,
                )
            )
        except (RpcCodecError, RpcEventDataError) as exc:
            error = _wire_error(exc)
            if error.code == "invalid_envelope":
                return WireError(
                    code="invalid_event",
                    message=error.message,
                    details=error.details,
                )
            return error

    def parse_wire_event(self, line: str) -> RpcEvent | WireError:
        try:
            return _kit_event(decode_event(line))
        except RpcCodecError as exc:
            return _wire_error(exc, line=line)

    def response_wire(self, response: RpcResponse) -> str | WireError:
        from evopi.rpc import RpcErrorInfo as ProductionErrorInfo

        try:
            error = response.error
            return encode_response(
                ProductionResponse(
                    request_id=response.request_id,
                    ok=response.ok,
                    result=cast(dict[str, Any] | None, response.result),
                    error=(
                        ProductionErrorInfo(
                            code=error.code,
                            message=error.message,
                            details=dict(error.details),
                        )
                        if error is not None
                        else None
                    ),
                    schema_version=response.schema_version,
                )
            )
        except RpcCodecError as exc:
            return _wire_error(exc)

    def parse_wire_response(self, line: str) -> RpcResponse | WireError:
        try:
            return _kit_response(decode_response(line))
        except RpcCodecError as exc:
            return _wire_error(exc, line=line)

    def arm_failure(self, method: str) -> None:
        self._host.arm_failure(method)

    def dispatched(self) -> tuple[DispatchedCall, ...]:
        return tuple(self._dispatched)

    async def close(self) -> None:
        await self._host.close()
        await self._server.close()


@pytest.mark.parametrize("scenario_name", tuple(RPC_SCENARIOS))
def test_production_rpc_binding_passes_independent_scenarios(scenario_name: str) -> None:
    async def scenario() -> None:
        queue_capacity = 1 if scenario_name == "slow subscriber failure" else 64
        adapter = _ProductionRpcAdapter(subscriber_queue_capacity=queue_capacity)
        try:
            await RPC_SCENARIOS[scenario_name](adapter)
        finally:
            await adapter.close()

    asyncio.run(scenario())


def test_rpc_confirmation_resumes_exactly_one_governed_tool() -> None:
    async def scenario() -> None:
        executed: list[str] = []
        broker = ConfirmationBroker(InMemoryConfirmationStore())
        harness = BaseHarness(
            model=_ScriptedModel(),
            confirmation_broker=broker,
        )
        harness.register_tool(
            Tool(
                name="echo",
                description="Echo one value",
                parameters={"type": "object"},
                handler=lambda value: executed.append(value) or value,
            )
        )
        harness.register_policy(_ConfirmEcho())
        host = HarnessRpcHost(harness, broker)
        server = RpcServer(host)

        started = await server.dispatch(
            ProductionRequest(
                request_id="start-1",
                method="run.start",
                params={"prompt": "echo"},
            )
        )
        assert started.ok is True
        assert started.result is not None
        run_id = started.result["run_id"]

        for _ in range(50):
            pending = broker.list_pending()
            if pending:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("confirmation did not become pending")
        request_id = pending[0].request.id
        assert pending[0].request.run_id == run_id
        assert pending[0].request.session_id == harness.session.session_id

        approved = await server.dispatch(
            ProductionRequest(
                request_id="confirm-1",
                method="confirmation.respond",
                params={"request_id": request_id, "decision": "approve"},
            )
        )
        assert approved.ok is True
        await harness.wait_for_idle()
        assert executed == ["rewritten"]

        duplicate = await server.dispatch(
            ProductionRequest(
                request_id="confirm-2",
                method="confirmation.respond",
                params={"request_id": request_id, "decision": "approve"},
            )
        )
        assert duplicate.ok is False
        assert duplicate.error is not None
        assert duplicate.error.code == "duplicate_response"
        assert executed == ["rewritten"]

        replay = host.events.replay(after_sequence=0)
        event_types = [event.type for event in replay]
        assert "confirmation_request" in event_types
        assert "confirmation_response" in event_types
        state_events = [
            event for event in replay if event.type == "confirmation_state_changed"
        ]
        assert [event.data["status"] for event in state_events] == [
            "pending",
            "approved",
        ]
        assert all("arguments" not in event.data for event in state_events)
        await host.close()
        await server.close()

    asyncio.run(scenario())


def test_rpc_confirmation_denial_never_executes_governed_tool() -> None:
    async def scenario() -> None:
        executed: list[str] = []
        broker = ConfirmationBroker(InMemoryConfirmationStore())
        harness = BaseHarness(
            model=_ScriptedModel(),
            confirmation_broker=broker,
        )
        harness.register_tool(
            Tool(
                name="echo",
                description="Echo one value",
                parameters={"type": "object"},
                handler=lambda value: executed.append(value) or value,
            )
        )
        harness.register_policy(_ConfirmEcho())
        host = HarnessRpcHost(harness, broker)
        server = RpcServer(host)

        started = await server.dispatch(
            ProductionRequest(
                request_id="deny-start",
                method="run.start",
                params={"prompt": "echo"},
            )
        )
        assert started.ok is True

        for _ in range(50):
            pending = broker.list_pending()
            if pending:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("confirmation did not become pending")

        denied = await server.dispatch(
            ProductionRequest(
                request_id="deny-response",
                method="confirmation.respond",
                params={
                    "request_id": pending[0].request.id,
                    "decision": "deny",
                },
            )
        )
        assert denied.ok is True
        await harness.wait_for_idle()
        assert executed == []

        state_events = [
            event
            for event in host.events.replay(after_sequence=0)
            if event.type == "confirmation_state_changed"
        ]
        assert [event.data["status"] for event in state_events] == [
            "pending",
            "denied",
        ]
        await host.close()
        await server.close()

    asyncio.run(scenario())
