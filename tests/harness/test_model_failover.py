from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from evopi.ai import (
    CircuitBreakerConfig,
    ModelCandidate,
    ModelFailoverConfig,
    ModelRoute,
    ModelRouteUnavailableError,
)
from evopi.core import AgentContext, CoreEvent, ModelError, ModelErrorInfo
from evopi.core.cancellation import AbortController
from evopi.core.messages import AssistantMessage, SystemMessage, UserMessage
from evopi.core.model_errors import ModelRetryConfig
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.core.tool import Tool, ToolCall
from evopi.harness.base import BaseHarness, PolicyBlockedError
from evopi.harness.confirmation import ConfirmationRequest, ConfirmationResponse
from evopi.harness.model_operation import GovernedModelOperation
from evopi.policy.decisions import PolicyDecision
from evopi.policy.types import PolicyContext
from evopi.session.compact import CompactionSettings
from evopi.trace.reader import read_trace


class _Model:
    def __init__(
        self,
        name: str,
        outcomes: list[AssistantMessage | Exception],
        *,
        context_window: int = 0,
    ) -> None:
        self.name = name
        self.context_window = context_window
        self._outcomes = iter(outcomes)
        self.calls = 0

    async def stream(
        self,
        context: AgentContext,
    ) -> AsyncIterator[ModelStreamEvent]:
        self.calls += 1
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        yield ModelComplete(message=outcome)


def _failure(kind: str = "connection") -> ModelError:
    return ModelError(
        ModelErrorInfo(
            kind=kind,  # type: ignore[arg-type]
            message=f"{kind} failure",
            provider="test",
            retryable=kind
            in {"rate_limited", "overloaded", "timeout", "connection", "server"},
        )
    )


def _route(primary: _Model, fallback: _Model) -> ModelRoute:
    return ModelRoute(
        candidates=(
            ModelCandidate(
                candidate_id="primary",
                provider="primary-provider",
                model=primary,
                failure_domain="primary-endpoint",
            ),
            ModelCandidate(
                candidate_id="fallback",
                provider="fallback-provider",
                model=fallback,
                failure_domain="fallback-endpoint",
            ),
        )
    )


@dataclass
class _FailoverPolicy:
    action: str
    contexts: list[PolicyContext] = field(default_factory=list)
    name: str = "failover_policy"
    version: str = "1"
    description: str = "Govern model failover"
    hooks: tuple = ("before_model_failover",)
    priority: int = 1
    enabled: bool = True
    source: str = "test"
    risk_level: str = "high"
    metadata: dict = field(default_factory=dict)

    def run(self, context: PolicyContext) -> PolicyDecision:
        self.contexts.append(context)
        return PolicyDecision(
            action=self.action,  # type: ignore[arg-type]
            reason=f"failover {self.action}",
        )


def test_harness_failover_runs_policy_and_preserves_event_order() -> None:
    primary = _Model("primary", [_failure()])
    fallback = _Model(
        "fallback",
        [AssistantMessage(content="ok", stop_reason="stop")],
    )
    policy = _FailoverPolicy("allow")
    harness = BaseHarness(
        model=primary,
        model_route=_route(primary, fallback),
        retry_config=ModelRetryConfig(enabled=True, max_retries=3, base_delay=2),
    )
    harness.register_policy(policy)  # type: ignore[arg-type]
    events: list[CoreEvent] = []
    harness.subscribe(events.append)

    answer = asyncio.run(harness.prompt("go"))

    assert answer.content == "ok"
    assert primary.calls == 1
    assert fallback.calls == 1
    assert len(policy.contexts) == 1
    context = policy.contexts[0]
    assert context.source_model_attempt is not None
    assert context.source_model_attempt.candidate_id == "primary"
    assert context.target_model_attempt is not None
    assert context.target_model_attempt.candidate_id == "fallback"
    assert context.error_info is not None
    assert context.error_info.kind == "connection"
    assert context.remaining_model_attempts == 2

    types = [event.type for event in events]
    failed_end = next(
        index
        for index, event in enumerate(events)
        if event.type == "message_end" and event.data.get("committed") is False
    )
    circuit = types.index("model_circuit_state_changed")
    failover_start = types.index("model_failover_start")
    failover_end = types.index("model_failover_end")
    retry_start = types.index("model_retry_start")
    next_model = next(
        index
        for index, event in enumerate(events)
        if event.type == "model_start"
        and event.data["attempt_info"].candidate_id == "fallback"
    )
    assert failed_end < circuit < retry_start < next_model < failover_start < failover_end
    assert events[retry_start].data["delay"] == 0


def test_failover_policy_block_stops_route_before_fallback_request() -> None:
    primary = _Model("primary", [_failure()])
    fallback = _Model(
        "fallback",
        [AssistantMessage(content="unused", stop_reason="stop")],
    )
    harness = BaseHarness(
        model=primary,
        model_route=_route(primary, fallback),
        retry_config=ModelRetryConfig(enabled=True, max_retries=3, base_delay=0),
    )
    harness.register_policy(_FailoverPolicy("block"))  # type: ignore[arg-type]

    with pytest.raises(PolicyBlockedError, match="failover block"):
        asyncio.run(harness.prompt("go"))

    assert primary.calls == 1
    assert fallback.calls == 0


def test_failover_policy_sees_exact_prepared_target_context() -> None:
    primary = _Model("primary", [_failure()])
    fallback = _Model(
        "fallback",
        [AssistantMessage(content="must not run", stop_reason="stop")],
    )
    @dataclass
    class SensitiveFailoverPolicy(_FailoverPolicy):
        def run(self, context: PolicyContext) -> PolicyDecision:
            self.contexts.append(context)
            contents = [message.content for message in context.agent_context.messages]
            return PolicyDecision(
                action="block" if "target-secret" in contents else "allow",
                reason="sensitive target context",
            )

    policy = SensitiveFailoverPolicy("allow")
    harness = BaseHarness(
        model=primary,
        model_route=_route(primary, fallback),
        retry_config=ModelRetryConfig(enabled=True, max_retries=1, base_delay=0),
    )

    def inject_secret(context: AgentContext) -> AgentContext:
        context.messages.append(SystemMessage(content="target-secret"))
        return context

    harness.add_context_provider(inject_secret)
    harness.register_policy(policy)  # type: ignore[arg-type]

    with pytest.raises(PolicyBlockedError, match="sensitive target context"):
        asyncio.run(harness.prompt("go"))

    assert fallback.calls == 0
    assert "target-secret" in [
        message.content for message in policy.contexts[0].agent_context.messages
    ]


def test_failover_policy_rejects_actions_without_route_semantics() -> None:
    primary = _Model("primary", [_failure()])
    fallback = _Model(
        "fallback",
        [AssistantMessage(content="unused", stop_reason="stop")],
    )
    harness = BaseHarness(
        model=primary,
        model_route=_route(primary, fallback),
        retry_config=ModelRetryConfig(enabled=True, max_retries=1, base_delay=0),
    )
    harness.register_policy(_FailoverPolicy("rewrite_args"))  # type: ignore[arg-type]

    with pytest.raises(PolicyBlockedError, match="does not support Policy action"):
        asyncio.run(harness.prompt("go"))

    assert fallback.calls == 0


def test_failover_confirmation_denial_fails_closed() -> None:
    primary = _Model("primary", [_failure()])
    fallback = _Model(
        "fallback",
        [AssistantMessage(content="unused", stop_reason="stop")],
    )
    requests: list[ConfirmationRequest] = []

    def deny(request: ConfirmationRequest) -> ConfirmationResponse:
        requests.append(request)
        return ConfirmationResponse(
            request_id=request.id,
            decision="deny",
            reason="user denied",
        )

    harness = BaseHarness(
        model=primary,
        model_route=_route(primary, fallback),
        retry_config=ModelRetryConfig(enabled=True, max_retries=3, base_delay=0),
        confirmation_handler=deny,
    )
    harness.register_policy(
        _FailoverPolicy("require_confirmation")  # type: ignore[arg-type]
    )

    with pytest.raises(PolicyBlockedError, match="user denied"):
        asyncio.run(harness.prompt("go"))

    assert requests[0].hook == "before_model_failover"
    assert fallback.calls == 0


def test_failover_confirmation_approval_continues_to_target() -> None:
    primary = _Model("primary", [_failure()])
    fallback = _Model(
        "fallback",
        [AssistantMessage(content="approved fallback", stop_reason="stop")],
    )
    requests: list[ConfirmationRequest] = []

    def approve(request: ConfirmationRequest) -> ConfirmationResponse:
        requests.append(request)
        return ConfirmationResponse(
            request_id=request.id,
            decision="approve",
        )

    harness = BaseHarness(
        model=primary,
        model_route=_route(primary, fallback),
        retry_config=ModelRetryConfig(enabled=True, max_retries=1, base_delay=0),
        confirmation_handler=approve,
    )
    harness.register_policy(
        _FailoverPolicy("require_confirmation")  # type: ignore[arg-type]
    )

    answer = asyncio.run(harness.prompt("go"))

    assert answer.content == "approved fallback"
    assert len(requests) == 1
    assert fallback.calls == 1


def test_non_failover_error_never_reaches_fallback_policy_or_model() -> None:
    primary = _Model("primary", [_failure("authentication")])
    fallback = _Model(
        "fallback",
        [AssistantMessage(content="unused", stop_reason="stop")],
    )
    policy = _FailoverPolicy("allow")
    harness = BaseHarness(
        model=primary,
        model_route=_route(primary, fallback),
        retry_config=ModelRetryConfig(enabled=True, max_retries=3, base_delay=0),
    )
    harness.register_policy(policy)  # type: ignore[arg-type]

    with pytest.raises(ModelError):
        asyncio.run(harness.prompt("go"))

    assert fallback.calls == 0
    assert policy.contexts == []


def test_successful_fallback_has_run_affinity_for_next_tool_turn() -> None:
    primary = _Model("primary", [_failure()])
    fallback = _Model(
        "fallback",
        [
            AssistantMessage(
                content="",
                tool_calls=[ToolCall(id="call-1", name="echo", arguments={})],
                stop_reason="tool_use",
            ),
            AssistantMessage(content="summary", stop_reason="stop"),
        ],
    )
    harness = BaseHarness(
        model=primary,
        model_route=_route(primary, fallback),
        retry_config=ModelRetryConfig(enabled=True, max_retries=3, base_delay=0),
    )
    harness.register_tool(
        Tool(
            name="echo",
            description="echo",
            parameters={"type": "object", "properties": {}},
            handler=lambda: "result",
        )
    )

    answer = asyncio.run(harness.prompt("go"))

    assert answer.content == "summary"
    assert primary.calls == 1
    assert fallback.calls == 2


def test_affinity_candidate_becoming_unavailable_requires_new_authorization() -> None:
    primary = _Model(
        "primary",
        [
            _failure(),
            AssistantMessage(content="must not run", stop_reason="stop"),
        ],
    )
    fallback = _Model(
        "fallback",
        [
            AssistantMessage(
                content="",
                tool_calls=[ToolCall(id="trip-1", name="trip_circuit", arguments={})],
                stop_reason="tool_use",
            ),
        ],
    )
    route = _route(primary, fallback)

    @dataclass
    class SequencedPolicy(_FailoverPolicy):
        actions: list[str] = field(default_factory=lambda: ["allow", "block"])

        def run(self, context: PolicyContext) -> PolicyDecision:
            self.contexts.append(context)
            action = self.actions.pop(0)
            return PolicyDecision(
                action=action,  # type: ignore[arg-type]
                reason=f"failover {action}",
            )

    policy = SequencedPolicy("allow")
    harness = BaseHarness(
        model=primary,
        model_route=route,
        retry_config=ModelRetryConfig(enabled=True, max_retries=2, base_delay=0),
    )

    def trip_circuit() -> str:
        route.record_failure("fallback", _failure().info)
        route.record_failure("fallback", _failure().info)
        return "tripped"

    harness.register_tool(
        Tool(
            name="trip_circuit",
            description="Open the fallback circuit",
            parameters={"type": "object", "properties": {}},
            handler=trip_circuit,
        )
    )
    harness.register_policy(policy)  # type: ignore[arg-type]

    with pytest.raises(PolicyBlockedError, match="failover block"):
        asyncio.run(harness.prompt("go"))

    assert primary.calls == 1
    assert fallback.calls == 1
    assert len(policy.contexts) == 2
    second = policy.contexts[1]
    assert second.source_model_attempt is not None
    assert second.source_model_attempt.candidate_id == "fallback"
    assert second.target_model_attempt is not None
    assert second.target_model_attempt.candidate_id == "primary"
    assert second.metadata["selection_reason"] == "circuit_open"


def test_context_overflow_fails_over_to_a_larger_candidate() -> None:
    primary = _Model("small", [_failure("context_overflow")])
    fallback = _Model(
        "large",
        [AssistantMessage(content="fits", stop_reason="stop")],
    )
    route = _route(primary, fallback)
    harness = BaseHarness(
        model=primary,
        model_route=route,
        retry_config=ModelRetryConfig(enabled=True, max_retries=1, base_delay=0),
    )

    answer = asyncio.run(harness.prompt("large prepared context"))

    assert answer.content == "fits"
    assert primary.calls == 1
    assert fallback.calls == 1
    assert route.circuit_snapshot("primary").state == "open"


def test_disabling_failover_preserves_same_candidate_retry() -> None:
    primary = _Model(
        "primary",
        [
            _failure(),
            AssistantMessage(content="recovered", stop_reason="stop"),
        ],
    )
    route = ModelRoute(
        candidates=(
            ModelCandidate(
                candidate_id="primary",
                provider="test",
                model=primary,
            ),
        ),
        failover_config=ModelFailoverConfig(enabled=False),
    )
    harness = BaseHarness(
        model=primary,
        model_route=route,
        retry_config=ModelRetryConfig(enabled=True, max_retries=1, base_delay=0),
    )

    answer = asyncio.run(harness.prompt("go"))

    assert answer.content == "recovered"
    assert primary.calls == 2


def test_disabling_failover_blocks_initial_context_fallback() -> None:
    primary = _Model(
        "tiny",
        [AssistantMessage(content="unused", stop_reason="stop")],
        context_window=1,
    )
    fallback = _Model(
        "large",
        [AssistantMessage(content="must not run", stop_reason="stop")],
        context_window=100_000,
    )
    route = ModelRoute(
        candidates=(
            ModelCandidate(candidate_id="primary", provider="test", model=primary),
            ModelCandidate(candidate_id="fallback", provider="test", model=fallback),
        ),
        failover_config=ModelFailoverConfig(enabled=False),
    )
    harness = BaseHarness(model=primary, model_route=route)

    with pytest.raises(ModelRouteUnavailableError):
        asyncio.run(harness.prompt("context larger than one token"))

    assert primary.calls == 0
    assert fallback.calls == 0


def test_disabling_failover_blocks_initial_circuit_fallback() -> None:
    primary = _Model(
        "primary",
        [AssistantMessage(content="unused", stop_reason="stop")],
    )
    fallback = _Model(
        "fallback",
        [AssistantMessage(content="must not run", stop_reason="stop")],
    )
    route = ModelRoute(
        candidates=(
            ModelCandidate(candidate_id="primary", provider="test", model=primary),
            ModelCandidate(candidate_id="fallback", provider="test", model=fallback),
        ),
        circuit_config=CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout=60,
        ),
    )
    route.record_failure("primary", _failure().info)
    route.failover_config = ModelFailoverConfig(enabled=False)
    harness = BaseHarness(model=primary, model_route=route)

    with pytest.raises(ModelRouteUnavailableError):
        asyncio.run(harness.prompt("go"))

    assert primary.calls == 0
    assert fallback.calls == 0


def test_known_incompatible_candidate_is_skipped_without_consuming_attempt() -> None:
    primary = _Model(
        "tiny",
        [AssistantMessage(content="unused", stop_reason="stop")],
        context_window=1,
    )
    fallback = _Model(
        "large",
        [AssistantMessage(content="ok", stop_reason="stop")],
        context_window=100_000,
    )
    events: list[CoreEvent] = []
    harness = BaseHarness(
        model=primary,
        model_route=ModelRoute(
            candidates=(
                ModelCandidate(
                    candidate_id="tiny",
                    provider="test",
                    model=primary,
                    output_reserve=1,
                ),
                ModelCandidate(
                    candidate_id="large",
                    provider="test",
                    model=fallback,
                ),
            )
        ),
        retry_config=ModelRetryConfig(enabled=True, max_retries=1, base_delay=0),
    )
    harness.subscribe(events.append)

    answer = asyncio.run(harness.prompt("a prompt that exceeds one token"))

    assert answer.content == "ok"
    assert primary.calls == 0
    assert fallback.calls == 1
    skipped = next(
        event for event in events if event.type == "model_candidate_skipped"
    )
    assert skipped.data["candidate_id"] == "tiny"
    assert skipped.data["reason"] == "context_incompatible"


def test_initial_fallback_requires_policy_authorization() -> None:
    primary = _Model(
        "tiny",
        [AssistantMessage(content="unused", stop_reason="stop")],
        context_window=1,
    )
    fallback = _Model(
        "large",
        [AssistantMessage(content="must not run", stop_reason="stop")],
        context_window=100_000,
    )
    policy = _FailoverPolicy("block")
    route = ModelRoute(
        candidates=(
            ModelCandidate(
                candidate_id="tiny",
                provider="primary-provider",
                model=primary,
                output_reserve=1,
            ),
            ModelCandidate(
                candidate_id="large",
                provider="fallback-provider",
                model=fallback,
            ),
        ),
        circuit_config=CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout=0,
        ),
    )
    route.record_failure("large", _failure().info)
    assert route.circuit_snapshot("large").state == "half_open"
    harness = BaseHarness(
        model=primary,
        model_route=route,
        retry_config=ModelRetryConfig(enabled=True, max_retries=1, base_delay=0),
    )
    harness.register_policy(policy)  # type: ignore[arg-type]

    with pytest.raises(PolicyBlockedError, match="failover block"):
        asyncio.run(harness.prompt("a prompt that exceeds one token"))

    assert primary.calls == 0
    assert fallback.calls == 0
    assert len(policy.contexts) == 1
    context = policy.contexts[0]
    assert context.source_model_attempt is None
    assert context.target_model_attempt is not None
    assert context.target_model_attempt.candidate_id == "large"
    assert context.metadata["selection_reason"] == "context_incompatible"
    assert route.try_acquire("large") is True
    route.release("large")


def test_router_close_releases_unsettled_half_open_probe() -> None:
    primary = _Model(
        "primary",
        [AssistantMessage(content="partial", stop_reason="aborted")],
    )
    route = ModelRoute(
        candidates=(
            ModelCandidate(
                candidate_id="primary",
                provider="test",
                model=primary,
            ),
        ),
        circuit_config=CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout=0,
        ),
    )
    route.record_failure("primary", _failure().info)
    assert route.circuit_snapshot("primary").state == "half_open"
    harness = BaseHarness(
        model=primary,
        model_route=route,
        retry_config=ModelRetryConfig(enabled=True, max_retries=0, base_delay=0),
    )

    answer = asyncio.run(harness.prompt("go"))

    assert answer.stop_reason == "aborted"
    assert route.circuit_snapshot("primary").state == "half_open"
    assert route.try_acquire("primary") is True
    route.release("primary")


def test_all_open_circuits_fail_without_consuming_attempt() -> None:
    primary = _Model(
        "primary",
        [AssistantMessage(content="unused", stop_reason="stop")],
    )
    fallback = _Model(
        "fallback",
        [AssistantMessage(content="unused", stop_reason="stop")],
    )
    route = _route(primary, fallback)
    for candidate in route.candidates:
        route.record_failure(candidate.candidate_id, _failure().info)
        route.record_failure(candidate.candidate_id, _failure().info)
    harness = BaseHarness(
        model=primary,
        model_route=route,
        retry_config=ModelRetryConfig(enabled=True, max_retries=3, base_delay=0),
    )

    with pytest.raises(ModelRouteUnavailableError):
        asyncio.run(harness.prompt("go"))

    assert primary.calls == 0
    assert fallback.calls == 0
    assert harness.agent.last_run is not None
    assert harness.agent.last_run.error_info is not None
    assert harness.agent.last_run.error_info.kind == "route_unavailable"


def test_router_terminal_error_closes_existing_retry_lifecycle() -> None:
    primary = _Model("primary", [_failure(), _failure()])
    route = ModelRoute(
        candidates=(
            ModelCandidate(candidate_id="primary", provider="test", model=primary),
        ),
        circuit_config=CircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout=60,
        ),
    )
    harness = BaseHarness(
        model=primary,
        model_route=route,
        retry_config=ModelRetryConfig(enabled=True, max_retries=3, base_delay=0),
    )
    events: list[CoreEvent] = []
    harness.subscribe(events.append)

    with pytest.raises(ModelRouteUnavailableError):
        asyncio.run(harness.prompt("go"))

    retry_events = [
        event.type for event in events if event.type.startswith("model_retry_")
    ]
    assert retry_events == ["model_retry_start", "model_retry_end"]
    retry_end = next(event for event in events if event.type == "model_retry_end")
    assert retry_end.data["success"] is False
    assert retry_end.data["attempts"] == 2
    assert retry_end.data["error_info"].kind == "route_unavailable"


def test_expired_open_circuit_emits_half_open_transition() -> None:
    clock = [0.0]
    primary = _Model(
        "primary",
        [AssistantMessage(content="probe succeeded", stop_reason="stop")],
    )
    route = ModelRoute(
        candidates=(
            ModelCandidate(candidate_id="primary", provider="test", model=primary),
        ),
        circuit_config=CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout=5,
        ),
        clock=lambda: clock[0],
    )
    route.record_failure("primary", _failure().info)
    assert route.circuit_snapshot("primary").state == "open"
    clock[0] = 5
    harness = BaseHarness(model=primary, model_route=route)
    events: list[CoreEvent] = []
    harness.subscribe(events.append)

    answer = asyncio.run(harness.prompt("go"))

    assert answer.content == "probe succeeded"
    transitions = [
        (event.data["before"].state, event.data["after"].state)
        for event in events
        if event.type == "model_circuit_state_changed"
    ]
    assert transitions == [("open", "half_open"), ("half_open", "closed")]


def test_compaction_failover_forwards_policy_and_confirmation_trace(tmp_path) -> None:
    primary = _Model("primary", [_failure()])
    fallback = _Model(
        "fallback",
        [AssistantMessage(content="summary", stop_reason="stop")],
    )
    requests: list[ConfirmationRequest] = []

    def approve(request: ConfirmationRequest) -> ConfirmationResponse:
        requests.append(request)
        return ConfirmationResponse(request_id=request.id, decision="approve")

    trace_path = tmp_path / "compaction-failover.jsonl"
    parent = BaseHarness(
        model=primary,
        model_route=_route(primary, fallback),
        trace_path=trace_path,
        confirmation_handler=approve,
        retry_config=ModelRetryConfig(enabled=True, max_retries=1, base_delay=0),
        compaction_settings=CompactionSettings(enabled=False),
    )
    parent.register_policy(
        _FailoverPolicy("require_confirmation")  # type: ignore[arg-type]
    )

    async def scenario() -> str:
        operation = GovernedModelOperation(
            parent=parent,
            model=primary,
            kind="session_compaction",
            signal_controller=AbortController(loop=asyncio.get_running_loop()),
        )
        context = AgentContext(
            messages=[
                SystemMessage(content="compact"),
                UserMessage(content="summarize"),
            ]
        )
        _ = [event async for event in operation.stream(context)]
        return operation.operation_id

    operation_id = asyncio.run(scenario())
    records = list(read_trace(trace_path))
    governed_types = {
        "policy_decision",
        "policy_evaluation",
        "confirmation_request",
        "confirmation_response",
        "model_failover_end",
    }
    forwarded = [record for record in records if record["type"] in governed_types]

    assert requests
    assert {record["type"] for record in forwarded} == governed_types
    assert all(record["data"]["operation_id"] == operation_id for record in forwarded)
