from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from evopi.ai import (
    CircuitAcquireResult,
    CircuitBreakerConfig,
    ModelCandidate,
    ModelFailoverConfig,
    ModelRoute,
    ModelRouteUnavailableError,
)
from evopi.core import AgentContext, ModelErrorInfo
from evopi.core.stream import ModelStreamEvent


class _Model:
    def __init__(self, name: str, *, context_window: int = 0) -> None:
        self.name = name
        self.context_window = context_window

    async def stream(
        self,
        context: AgentContext,
    ) -> AsyncIterator[ModelStreamEvent]:
        if False:
            yield


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _failure(
    kind: str = "connection",
    *,
    code: str | None = None,
    retry_after: float | None = None,
) -> ModelErrorInfo:
    return ModelErrorInfo(
        kind=kind,  # type: ignore[arg-type]
        message=f"{kind} failure",
        provider="test",
        retryable=kind not in {"not_found", "route_unavailable"},
        code=code,
        retry_after=retry_after,
    )


def _route(clock: _Clock) -> ModelRoute:
    return ModelRoute(
        candidates=(
            ModelCandidate(
                candidate_id="primary",
                provider="openai-responses",
                model=_Model("gpt-primary", context_window=128_000),
                failure_domain="openai|https://api.example/v1",
                output_reserve=4096,
                metadata={"region": "global"},
            ),
            ModelCandidate(
                candidate_id="fallback",
                provider="anthropic",
                model=_Model("claude-fallback", context_window=200_000),
                failure_domain="anthropic|https://api.example",
            ),
        ),
        circuit_config=CircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout=30,
        ),
        clock=clock,
    )


def test_route_has_stable_fingerprint_and_hides_failure_domain() -> None:
    clock = _Clock()
    first = _route(clock)
    second = _route(clock)

    assert first.route_id == second.route_id
    assert first.fingerprint == second.fingerprint
    assert len(first.route_id) == 64
    assert first.candidates[0].failure_domain_id != "openai|https://api.example/v1"
    assert len(first.candidates[0].failure_domain_id) == 64
    assert first.candidates[0].context_window == 128_000


def test_route_fingerprint_changes_when_failover_is_disabled() -> None:
    clock = _Clock()
    enabled = _route(clock)
    disabled = ModelRoute(
        candidates=enabled.candidates,
        failover_config=ModelFailoverConfig(enabled=False),
        circuit_config=enabled.circuit_config,
        clock=clock,
    )

    assert enabled.fingerprint != disabled.fingerprint


def test_candidate_metadata_must_be_json_safe() -> None:
    with pytest.raises(ValueError, match="JSON-safe"):
        ModelCandidate(
            candidate_id="bad",
            provider="test",
            model=_Model("bad"),
            metadata={"value": object()},
        )


def test_candidate_metadata_is_an_immutable_snapshot() -> None:
    source = {"nested": {"labels": ["approved"]}}
    candidate = ModelCandidate(
        candidate_id="stable",
        provider="test",
        model=_Model("stable"),
        metadata=source,
    )

    source["nested"]["labels"].append("mutated")

    assert candidate.metadata["nested"]["labels"] == ("approved",)
    with pytest.raises(TypeError):
        candidate.metadata["new"] = "value"  # type: ignore[index]


def test_two_health_failures_open_circuit_then_half_open_allows_one_probe() -> None:
    clock = _Clock()
    route = _route(clock)

    route.record_failure("primary", _failure())
    assert route.circuit_snapshot("primary").state == "closed"
    route.record_failure("primary", _failure())
    opened = route.circuit_snapshot("primary")
    assert opened.state == "open"
    assert opened.consecutive_failures == 2
    assert opened.remaining_cooldown == 30
    assert route.try_acquire("primary") is False

    clock.now += 30
    acquired = route.acquire("primary")
    assert isinstance(acquired, CircuitAcquireResult)
    assert acquired.acquired is True
    assert acquired.before.state == "open"
    assert acquired.after.state == "half_open"
    assert route.circuit_snapshot("primary").state == "half_open"
    assert route.try_acquire("primary") is False

    route.record_success("primary")
    recovered = route.circuit_snapshot("primary")
    assert recovered.state == "closed"
    assert recovered.consecutive_failures == 0


def test_retry_after_immediately_pauses_shared_failure_domain() -> None:
    clock = _Clock()
    shared_domain = "openai|https://api.example/v1"
    route = ModelRoute(
        candidates=(
            ModelCandidate(
                candidate_id="model-a",
                provider="openai",
                model=_Model("a"),
                failure_domain=shared_domain,
            ),
            ModelCandidate(
                candidate_id="model-b",
                provider="openai",
                model=_Model("b"),
                failure_domain=shared_domain,
            ),
        ),
        clock=clock,
    )

    route.record_failure("model-a", _failure("rate_limited", retry_after=45))

    assert route.circuit_snapshot("model-a").state == "open"
    assert route.circuit_snapshot("model-b").state == "open"
    assert route.circuit_snapshot("model-b").remaining_cooldown == 45


def test_explicit_model_unavailable_only_opens_candidate_circuit() -> None:
    clock = _Clock()
    shared_domain = "openai|https://api.example/v1"
    route = ModelRoute(
        candidates=(
            ModelCandidate(
                candidate_id="model-a",
                provider="openai",
                model=_Model("a"),
                failure_domain=shared_domain,
            ),
            ModelCandidate(
                candidate_id="model-b",
                provider="openai",
                model=_Model("b"),
                failure_domain=shared_domain,
            ),
        ),
        clock=clock,
    )

    route.record_failure(
        "model-a",
        _failure("not_found", code="model_not_found"),
    )

    assert route.circuit_snapshot("model-a").state == "open"
    assert route.circuit_snapshot("model-b").state == "closed"


def test_unavailable_error_is_structured_and_not_retryable() -> None:
    error = ModelRouteUnavailableError(
        route_id="route",
        earliest_retry_after=12.5,
        reason="All model candidates are unavailable",
    )

    assert error.info.kind == "route_unavailable"
    assert error.info.retryable is False
    assert error.info.metadata == {
        "route_id": "route",
        "earliest_retry_after": 12.5,
    }


def test_failover_config_only_switches_explicit_model_not_found_codes() -> None:
    config = ModelFailoverConfig()

    assert config.can_failover(_failure("quota_exhausted")) is True
    assert config.can_failover(_failure("context_overflow")) is True
    assert config.can_failover(
        _failure("not_found", code="model_unavailable")
    ) is True
    assert config.can_failover(_failure("not_found", code="resource_missing")) is False
    assert config.can_failover(_failure("authentication")) is False


def test_context_overflow_opens_only_the_incompatible_candidate() -> None:
    clock = _Clock()
    route = _route(clock)

    route.record_failure("primary", _failure("context_overflow"))

    assert route.circuit_snapshot("primary").state == "open"
    assert route.circuit_snapshot("fallback").state == "closed"
