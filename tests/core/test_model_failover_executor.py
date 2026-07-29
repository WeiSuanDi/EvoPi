from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from evopi.core import (
    Agent,
    AgentContext,
    CoreEvent,
    ModelAttemptInfo,
    ModelAttemptSelection,
    ModelErrorInfo,
    ModelRetryConfig,
)
from evopi.core.model_errors import ModelError
from evopi.core.stream import ModelComplete, ModelStreamEvent, TextDelta
from evopi.core.messages import AssistantMessage


class _Model:
    context_window = 0

    def __init__(
        self,
        name: str,
        outcomes: list[AssistantMessage | Exception],
        *,
        partial: str = "",
    ) -> None:
        self.name = name
        self._outcomes = iter(outcomes)
        self.partial = partial
        self.calls = 0

    async def stream(
        self,
        context: AgentContext,
    ) -> AsyncIterator[ModelStreamEvent]:
        self.calls += 1
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            if self.partial:
                yield TextDelta(delta=self.partial)
            raise outcome
        yield ModelComplete(message=outcome)


def _error(kind: str = "connection") -> ModelError:
    return ModelError(
        ModelErrorInfo(
            kind=kind,  # type: ignore[arg-type]
            message=f"{kind} failure",
            provider="test",
            retryable=True,
        )
    )


def _info(
    candidate_id: str,
    model: _Model,
    *,
    attempt: int,
) -> ModelAttemptInfo:
    return ModelAttemptInfo(
        route_id="route-1",
        candidate_id=candidate_id,
        provider="test",
        model=model.name,
        failure_domain_id=f"domain-{candidate_id}",
        attempt=attempt,
        route_round=1,
    )


class _Router:
    def __init__(self, primary: _Model, fallback: _Model) -> None:
        self.primary = primary
        self.fallback = fallback
        self.successes: list[str] = []
        self.failures: list[str] = []

    async def select_initial(self, **kwargs: object) -> ModelAttemptSelection:
        return ModelAttemptSelection(
            model=self.primary,
            info=_info("primary", self.primary, attempt=1),
        )

    async def select_after_failure(
        self,
        *,
        previous: ModelAttemptSelection,
        error_info: ModelErrorInfo | None,
        next_attempt: int,
        **kwargs: object,
    ) -> ModelAttemptSelection | None:
        return ModelAttemptSelection(
            model=self.fallback,
            info=_info("fallback", self.fallback, attempt=next_attempt),
            delay=0,
        )

    async def record_failure(
        self,
        selection: ModelAttemptSelection,
        error: Exception,
        error_info: ModelErrorInfo | None,
    ) -> None:
        del error, error_info
        self.failures.append(selection.info.candidate_id)

    async def record_success(self, selection: ModelAttemptSelection) -> None:
        self.successes.append(selection.info.candidate_id)

    async def record_abandoned(self, selection: ModelAttemptSelection) -> None:
        del selection
        return None

    async def authorize_attempt(
        self,
        selection: ModelAttemptSelection,
        context: AgentContext,
        signal: object,
    ) -> None:
        del selection, context, signal
        return None

    async def close(self) -> None:
        return None


def test_executor_switches_models_without_committing_failed_partial_output() -> None:
    primary = _Model("primary-model", [_error()], partial="partial")
    fallback = _Model(
        "fallback-model",
        [AssistantMessage(content="fallback answer", stop_reason="stop")],
    )
    router = _Router(primary, fallback)
    events: list[CoreEvent] = []
    prepared: list[str] = []

    def prepare(
        context: AgentContext,
        *,
        attempt_info: ModelAttemptInfo | None = None,
    ) -> AgentContext:
        assert attempt_info is not None
        prepared.append(attempt_info.candidate_id)
        return context

    agent = Agent(
        model=primary,
        retry_config=ModelRetryConfig(enabled=True, max_retries=3, base_delay=30),
        prepare_context=prepare,
        model_attempt_router_factory=lambda run_id: router,
    )
    agent.subscribe(events.append)

    answer = asyncio.run(agent.prompt("go"))

    assert answer.content == "fallback answer"
    assert primary.calls == 1
    assert fallback.calls == 1
    assert prepared == ["primary", "fallback"]
    assert router.failures == ["primary"]
    assert router.successes == ["fallback"]
    starts = [event for event in events if event.type == "model_start"]
    assert [event.data["attempt_info"].candidate_id for event in starts] == [
        "primary",
        "fallback",
    ]
    retry = next(event for event in events if event.type == "model_retry_start")
    assert retry.data["delay"] == 0
    failed = [
        event.data["message"]
        for event in events
        if event.type == "message_end"
        and event.data.get("committed") is False
    ]
    assert failed[0].content == "partial"
    assert all(message.id != failed[0].id for message in agent.messages)
    assert answer.metadata["model_attempt"]["candidate_id"] == "fallback"


def test_failover_attempts_share_existing_total_retry_budget() -> None:
    primary = _Model("primary-model", [_error()])
    fallback = _Model("fallback-model", [_error()])
    router = _Router(primary, fallback)
    agent = Agent(
        model=primary,
        retry_config=ModelRetryConfig(enabled=True, max_retries=1, base_delay=0),
        model_attempt_router_factory=lambda run_id: router,
    )

    try:
        asyncio.run(agent.prompt("go"))
    except ModelError:
        pass
    else:  # pragma: no cover - assertion is clearer than pytest for this branch.
        raise AssertionError("expected model failure")

    assert primary.calls + fallback.calls == 2


def test_no_retry_disables_failover_attempts() -> None:
    primary = _Model("primary-model", [_error()])
    fallback = _Model(
        "fallback-model",
        [AssistantMessage(content="unused", stop_reason="stop")],
    )
    router = _Router(primary, fallback)
    agent = Agent(
        model=primary,
        retry_config=ModelRetryConfig(enabled=False, max_retries=3),
        model_attempt_router_factory=lambda run_id: router,
    )

    try:
        asyncio.run(agent.prompt("go"))
    except ModelError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected model failure")

    assert primary.calls == 1
    assert fallback.calls == 0
    assert router.failures == ["primary"]


def test_aborted_model_attempt_is_not_recorded_as_route_success() -> None:
    primary = _Model(
        "primary-model",
        [AssistantMessage(content="partial", stop_reason="aborted")],
    )
    fallback = _Model(
        "fallback-model",
        [AssistantMessage(content="unused", stop_reason="stop")],
    )
    router = _Router(primary, fallback)
    agent = Agent(
        model=primary,
        retry_config=ModelRetryConfig(enabled=True, max_retries=1, base_delay=0),
        model_attempt_router_factory=lambda run_id: router,
    )

    answer = asyncio.run(agent.prompt("go"))

    assert answer.stop_reason == "aborted"
    assert router.successes == []
