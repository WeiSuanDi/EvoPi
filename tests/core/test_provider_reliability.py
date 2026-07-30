from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from evopi.ai.api.base import (
    ModelRequestError,
    classify_model_error,
    normalize_model_exception,
    parse_retry_after,
    raise_for_model_status,
)
from evopi.core.agent import Agent
from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent
from evopi.core.messages import AssistantMessage
from evopi.core.model_errors import ModelErrorInfo, ModelRetryConfig
from evopi.core.stream import ModelComplete, ModelStreamEvent, TextDelta
from evopi.core.tool import Tool, ToolCall
from evopi.harness.base import BaseHarness
from evopi.policy.decisions import PolicyDecision
from evopi.policy.types import PolicyContext
from evopi.trace.reader import read_trace


def model_failure(
    kind: str = "connection",
    *,
    retry_after: float | None = None,
) -> ModelRequestError:
    return ModelRequestError(
        f"{kind} failure",
        kind=kind,  # type: ignore[arg-type]
        provider="test",
        retry_after=retry_after,
    )


class SequencedModel:
    name = "sequenced"

    def __init__(self, outcomes: list[Exception | AssistantMessage]) -> None:
        self.outcomes = iter(outcomes)
        self.calls = 0
        self.contexts: list[AgentContext] = []

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        self.calls += 1
        self.contexts.append(context)
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        yield ModelComplete(message=outcome)


class PartialFailureModel(SequencedModel):
    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        self.calls += 1
        self.contexts.append(context)
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            yield TextDelta(delta="partial")
            raise outcome
        yield ModelComplete(message=outcome)


@dataclass
class ErrorRecordingPolicy:
    infos: list[ModelErrorInfo | None]
    name: str = "error_recorder"
    version: str = "1"
    description: str = "Record final model failures"
    hooks: tuple = ("on_error",)
    priority: int = 1
    enabled: bool = True
    source: str = "test"
    risk_level: str = "low"
    metadata: dict = field(default_factory=dict)

    def run(self, context: PolicyContext) -> PolicyDecision:
        self.infos.append(context.error_info)
        return PolicyDecision(action="allow")


@pytest.mark.parametrize(
    ("status", "code", "message", "kind"),
    [
        (401, None, "bad key", "authentication"),
        (403, None, "denied", "permission"),
        (400, None, "bad request", "invalid_request"),
        (404, None, "missing", "not_found"),
        (400, "context_length_exceeded", "too long", "context_overflow"),
        (429, "insufficient_quota", "billing", "quota_exhausted"),
        (429, None, "slow down", "rate_limited"),
        (529, None, "busy", "overloaded"),
        (503, None, "unavailable", "server"),
        (None, None, "mystery", "unknown"),
    ],
)
def test_provider_neutral_error_classification(
    status: int | None,
    code: str | None,
    message: str,
    kind: str,
) -> None:
    assert classify_model_error(status_code=status, code=code, message=message) == kind


def test_retry_after_supports_seconds_and_http_date() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_retry_after("2.5", now=now) == 2.5
    header = format_datetime(now + timedelta(seconds=12), usegmt=True)
    assert parse_retry_after(header, now=now) == 12


def test_http_and_transport_failures_are_structured() -> None:
    async def scenario() -> None:
        request = httpx.Request("POST", "https://example.test")
        response = httpx.Response(
            429,
            request=request,
            json={"error": {"message": "slow down", "code": "rate_limit"}},
            headers={"retry-after": "3", "x-request-id": "req-1"},
        )
        with pytest.raises(ModelRequestError) as caught:
            await raise_for_model_status(response, provider="test")
        assert caught.value.info.kind == "rate_limited"
        assert caught.value.info.retry_after == 3
        assert caught.value.info.request_id == "req-1"

        timeout = normalize_model_exception(httpx.ReadTimeout("idle"), provider="test")
        connection = normalize_model_exception(
            httpx.ConnectError("offline"), provider="test"
        )
        assert timeout.info.kind == "timeout"
        assert connection.info.kind == "connection"

    asyncio.run(scenario())


def test_transient_failure_retries_without_polluting_context() -> None:
    model = PartialFailureModel(
        [model_failure(), AssistantMessage(content="success", stop_reason="stop")]
    )
    events: list[CoreEvent] = []
    prepare_calls = 0
    after_calls = 0

    def prepare(context: AgentContext) -> AgentContext:
        nonlocal prepare_calls
        prepare_calls += 1
        return context

    def after(context: AgentContext, message: AssistantMessage) -> AssistantMessage:
        nonlocal after_calls
        after_calls += 1
        return message

    agent = Agent(
        model=model,
        retry_config=ModelRetryConfig(enabled=True, base_delay=0),
        prepare_context=prepare,
        after_model_call=after,
    )
    agent.subscribe(events.append)

    answer = asyncio.run(agent.prompt("go"))

    assert answer.content == "success"
    assert model.calls == 2
    assert prepare_calls == 2
    assert after_calls == 1
    assert [event.data["attempt"] for event in events if event.type == "model_start"] == [
        1,
        2,
    ]
    failed = [
        event.data["message"]
        for event in events
        if event.type == "message_end"
        and getattr(event.data.get("message"), "stop_reason", None) == "error"
    ]
    assert failed[0].content == "partial"
    assert failed[0].metadata["committed"] is False
    assert all(message.stop_reason != "error" for message in agent.messages if hasattr(message, "stop_reason"))
    assert [event.type for event in events].count("model_retry_start") == 1
    assert [event.type for event in events].count("model_retry_end") == 1
    assert [event.type for event in events].count("turn_start") == 1
    assert agent.last_run is not None
    assert agent.last_run.turns_used == 1
    assert agent.last_run.max_turns == 20


def test_retry_exhaustion_reports_once_to_policy_run_state_and_trace(tmp_path) -> None:
    model = SequencedModel([model_failure(), model_failure()])
    infos: list[ModelErrorInfo | None] = []
    harness = BaseHarness(
        model=model,
        trace_path=tmp_path / "retry.jsonl",
        retry_config=ModelRetryConfig(enabled=True, max_retries=1, base_delay=0),
    )
    harness.register_policy(ErrorRecordingPolicy(infos))  # type: ignore[arg-type]

    with pytest.raises(ModelRequestError):
        asyncio.run(harness.prompt("fail"))

    assert model.calls == 2
    assert len(infos) == 1
    assert infos[0] is not None and infos[0].kind == "connection"
    assert harness.agent.last_run is not None
    assert harness.agent.last_run.error_info == infos[0]
    trace = list(read_trace(tmp_path / "retry.jsonl"))
    assert all(record["schema_version"] == 2 for record in trace)
    assert [record["type"] for record in trace].count("error") == 1
    error_record = next(record for record in trace if record["type"] == "error")
    assert error_record["data"]["error_info"]["kind"] == "connection"
    agent_end = next(record for record in trace if record["type"] == "agent_end")
    assert all(
        message.get("stop_reason") != "error"
        for message in agent_end["data"]["messages"]
    )


@pytest.mark.parametrize(
    "kind",
    [
        "authentication",
        "quota_exhausted",
        "context_overflow",
        "protocol",
        "unknown",
    ],
)
def test_non_retryable_failures_do_not_retry(kind: str) -> None:
    model = SequencedModel([model_failure(kind)])
    agent = Agent(
        model=model,
        retry_config=ModelRetryConfig(enabled=True, base_delay=0),
    )

    with pytest.raises(ModelRequestError):
        asyncio.run(agent.prompt("fail"))

    assert model.calls == 1
    assert agent.last_run is not None
    assert agent.last_run.error_info is not None
    assert agent.last_run.error_info.kind == kind


def test_retry_after_over_max_delay_fails_immediately() -> None:
    model = SequencedModel([model_failure("rate_limited", retry_after=61)])
    agent = Agent(
        model=model,
        retry_config=ModelRetryConfig(enabled=True, base_delay=0, max_delay=60),
    )

    with pytest.raises(ModelRequestError):
        asyncio.run(agent.prompt("fail"))

    assert model.calls == 1


def test_prepare_context_failure_cannot_be_bypassed_by_retry() -> None:
    model = SequencedModel([AssistantMessage(content="unused", stop_reason="stop")])
    prepare_calls = 0

    def blocked(context: AgentContext) -> AgentContext:
        nonlocal prepare_calls
        prepare_calls += 1
        raise RuntimeError("before_model_call blocked")

    agent = Agent(
        model=model,
        prepare_context=blocked,
        retry_config=ModelRetryConfig(enabled=True, base_delay=0),
    )

    with pytest.raises(RuntimeError, match="blocked"):
        asyncio.run(agent.prompt("go"))

    assert prepare_calls == 1
    assert model.calls == 0


def test_abort_during_retry_backoff_stops_future_attempts() -> None:
    async def scenario() -> None:
        model = SequencedModel(
            [model_failure(), AssistantMessage(content="too late", stop_reason="stop")]
        )
        agent = Agent(
            model=model,
            retry_config=ModelRetryConfig(enabled=True, base_delay=30),
        )
        retrying = asyncio.Event()

        def listener(event: CoreEvent) -> None:
            if event.type == "model_retry_start":
                retrying.set()

        agent.subscribe(listener)
        task = asyncio.create_task(agent.prompt("go"))
        await retrying.wait()
        agent.abort()
        answer = await task

        assert model.calls == 1
        assert answer.stop_reason == "aborted"
        assert agent.last_run is not None
        assert agent.last_run.end_reason == "aborted"

    asyncio.run(scenario())


def test_retry_success_continues_through_tool_result_and_summary() -> None:
    model = SequencedModel(
        [
            model_failure("server"),
            AssistantMessage(
                content="",
                tool_calls=[ToolCall(id="call-1", name="echo", arguments={})],
                stop_reason="tool_use",
            ),
            AssistantMessage(content="summary", stop_reason="stop"),
        ]
    )
    agent = Agent(
        model=model,
        tools=[
            Tool(
                name="echo",
                description="Return a value",
                parameters={"type": "object", "properties": {}},
                handler=lambda: "tool result",
            )
        ],
        retry_config=ModelRetryConfig(enabled=True, base_delay=0),
    )

    answer = asyncio.run(agent.prompt("go"))

    assert answer.content == "summary"
    assert model.calls == 3
    assert any(getattr(message, "content", None) == "tool result" for message in agent.messages)


def test_external_prompt_cancellation_cleans_up_retry_wait() -> None:
    async def scenario() -> None:
        model = SequencedModel([model_failure()])
        agent = Agent(
            model=model,
            retry_config=ModelRetryConfig(enabled=True, base_delay=30),
        )
        retrying = asyncio.Event()

        def listener(event: CoreEvent) -> None:
            if event.type == "model_retry_start":
                retrying.set()

        agent.subscribe(listener)
        task = asyncio.create_task(agent.prompt("go"))
        await retrying.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert agent.is_running is False
        assert agent.last_run is not None
        assert agent.last_run.end_reason == "aborted"

    asyncio.run(scenario())
