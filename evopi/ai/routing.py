"""Provider-neutral ordered model routes and process-local circuit state."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from threading import RLock
from types import MappingProxyType
from typing import Any, Literal, TypeAlias

from evopi.core.model import Model
from evopi.core.model_errors import ModelError, ModelErrorInfo, ModelErrorKind

CircuitState: TypeAlias = Literal["closed", "open", "half_open"]

_DEFAULT_FAILOVER_KINDS: frozenset[ModelErrorKind] = frozenset(
    {
        "rate_limited",
        "overloaded",
        "context_overflow",
        "timeout",
        "connection",
        "server",
        "quota_exhausted",
    }
)
_DEFAULT_MODEL_UNAVAILABLE_CODES = frozenset(
    {"model_not_found", "model_unavailable", "deployment_not_found"}
)


def _json_safe_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    copied = dict(value)
    try:
        json.dumps(
            copied,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata must be JSON-safe") from exc
    return json.loads(
        json.dumps(
            copied,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(slots=True, frozen=True, kw_only=True)
class ModelCandidate:
    """One explicitly authorized model endpoint in an ordered route."""

    candidate_id: str
    provider: str
    model: Model
    failure_domain: str | None = None
    context_window: int | None = None
    output_reserve: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    failure_domain_id: str = field(init=False)

    def __post_init__(self) -> None:
        candidate_id = self.candidate_id.strip()
        provider = self.provider.strip()
        if not candidate_id:
            raise ValueError("candidate_id cannot be empty")
        if not provider:
            raise ValueError("provider cannot be empty")
        context_window = (
            self.model.context_window
            if self.context_window is None
            else self.context_window
        )
        if context_window < 0:
            raise ValueError("context_window cannot be negative")
        if self.output_reserve < 0:
            raise ValueError("output_reserve cannot be negative")
        failure_domain = (self.failure_domain or provider).strip()
        if not failure_domain:
            raise ValueError("failure_domain cannot be empty")
        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "failure_domain", failure_domain)
        object.__setattr__(self, "failure_domain_id", _digest(failure_domain))
        object.__setattr__(self, "context_window", context_window)
        object.__setattr__(
            self,
            "metadata",
            _freeze_json(_json_safe_copy(self.metadata)),
        )


@dataclass(slots=True, frozen=True, kw_only=True)
class ModelFailoverConfig:
    """Deterministic error eligibility for candidate switching."""

    enabled: bool = True
    failover_error_kinds: frozenset[ModelErrorKind] = _DEFAULT_FAILOVER_KINDS
    model_unavailable_codes: frozenset[str] = _DEFAULT_MODEL_UNAVAILABLE_CODES

    def can_failover(self, error: ModelErrorInfo) -> bool:
        if not self.enabled:
            return False
        if error.kind in self.failover_error_kinds:
            return True
        return (
            error.kind == "not_found"
            and error.code is not None
            and error.code in self.model_unavailable_codes
        )

    def is_candidate_unavailable(self, error: ModelErrorInfo) -> bool:
        return error.kind == "context_overflow" or (
            error.kind == "not_found"
            and error.code is not None
            and error.code in self.model_unavailable_codes
        )


@dataclass(slots=True, frozen=True, kw_only=True)
class CircuitBreakerConfig:
    failure_threshold: int = 2
    recovery_timeout: float = 30.0

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if self.recovery_timeout < 0:
            raise ValueError("recovery_timeout cannot be negative")


@dataclass(slots=True, frozen=True, kw_only=True)
class CircuitStateSnapshot:
    failure_domain_id: str
    state: CircuitState
    consecutive_failures: int
    remaining_cooldown: float
    candidate_id: str | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class CircuitAcquireResult:
    acquired: bool
    before: CircuitStateSnapshot
    after: CircuitStateSnapshot


class ModelRouteUnavailableError(ModelError):
    """Raised when no candidate can consume an attempt right now."""

    def __init__(
        self,
        *,
        route_id: str,
        earliest_retry_after: float | None,
        reason: str,
    ) -> None:
        metadata: dict[str, Any] = {"route_id": route_id}
        if earliest_retry_after is not None:
            metadata["earliest_retry_after"] = earliest_retry_after
        super().__init__(
            ModelErrorInfo(
                kind="route_unavailable",
                message=reason,
                provider="model-route",
                retryable=False,
                retry_after=None,
                metadata=metadata,
            )
        )


@dataclass(slots=True)
class _CircuitRecord:
    state: CircuitState = "closed"
    consecutive_failures: int = 0
    opened_until: float = 0.0
    probe_in_flight: bool = False


class ModelRoute:
    """Ordered candidates plus shared process-local health and Run affinity."""

    def __init__(
        self,
        *,
        candidates: tuple[ModelCandidate, ...] | list[ModelCandidate],
        failover_config: ModelFailoverConfig | None = None,
        circuit_config: CircuitBreakerConfig | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        resolved = tuple(candidates)
        if not resolved:
            raise ValueError("ModelRoute requires at least one candidate")
        ids = [candidate.candidate_id for candidate in resolved]
        if len(set(ids)) != len(ids):
            raise ValueError("ModelRoute candidate_id values must be unique")
        self.candidates = resolved
        self.failover_config = failover_config or ModelFailoverConfig()
        self.circuit_config = circuit_config or CircuitBreakerConfig()
        self._clock = clock or time.monotonic
        self._by_id = {candidate.candidate_id: candidate for candidate in resolved}
        self._domain_circuits: dict[str, _CircuitRecord] = {}
        self._candidate_circuits: dict[str, _CircuitRecord] = {}
        self._run_affinity: dict[str, str] = {}
        self._lock = RLock()
        canonical = json.dumps(
            {
                "candidates": [
                    {
                        "candidate_id": candidate.candidate_id,
                        "provider": candidate.provider,
                        "model": candidate.model.name,
                        "failure_domain_id": candidate.failure_domain_id,
                        "context_window": candidate.context_window,
                        "output_reserve": candidate.output_reserve,
                        "metadata": _plain_json(candidate.metadata),
                    }
                    for candidate in resolved
                ],
                "failover_error_kinds": sorted(
                    self.failover_config.failover_error_kinds
                ),
                "failover_enabled": self.failover_config.enabled,
                "model_unavailable_codes": sorted(
                    self.failover_config.model_unavailable_codes
                ),
                "failure_threshold": self.circuit_config.failure_threshold,
                "recovery_timeout": float(self.circuit_config.recovery_timeout),
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        self.fingerprint = _digest(canonical)
        self.route_id = self.fingerprint

    @property
    def primary(self) -> ModelCandidate:
        return self.candidates[0]

    def candidate(self, candidate_id: str) -> ModelCandidate:
        try:
            return self._by_id[candidate_id]
        except KeyError as exc:
            raise KeyError(f"Unknown model candidate: {candidate_id}") from exc

    def affinity_for(self, run_id: str) -> str | None:
        with self._lock:
            return self._run_affinity.get(run_id)

    def set_affinity(self, run_id: str, candidate_id: str) -> None:
        self.candidate(candidate_id)
        with self._lock:
            self._run_affinity[run_id] = candidate_id

    def clear_affinity(self, run_id: str) -> None:
        with self._lock:
            self._run_affinity.pop(run_id, None)

    def circuit_snapshot(self, candidate_id: str) -> CircuitStateSnapshot:
        with self._lock:
            candidate = self.candidate(candidate_id)
            now = self._clock()
            return self._snapshot_locked(candidate, now, refresh=True)

    def try_acquire(self, candidate_id: str) -> bool:
        return self.acquire(candidate_id).acquired

    def acquire(self, candidate_id: str) -> CircuitAcquireResult:
        with self._lock:
            candidate = self.candidate(candidate_id)
            now = self._clock()
            before = self._snapshot_locked(candidate, now, refresh=False)
            records = (
                self._candidate_circuits.get(candidate_id),
                self._domain_circuits.get(candidate.failure_domain_id),
            )
            acquired: list[_CircuitRecord] = []
            for record in records:
                if record is None:
                    continue
                self._refresh_record(record, now)
                if record.state == "open":
                    self._release_probes(acquired)
                    return CircuitAcquireResult(
                        acquired=False,
                        before=before,
                        after=self._snapshot_locked(candidate, now, refresh=False),
                    )
                if record.state == "half_open":
                    if record.probe_in_flight:
                        self._release_probes(acquired)
                        return CircuitAcquireResult(
                            acquired=False,
                            before=before,
                            after=self._snapshot_locked(candidate, now, refresh=False),
                        )
                    record.probe_in_flight = True
                    acquired.append(record)
            return CircuitAcquireResult(
                acquired=True,
                before=before,
                after=self._snapshot_locked(candidate, now, refresh=False),
            )

    def _snapshot_locked(
        self,
        candidate: ModelCandidate,
        now: float,
        *,
        refresh: bool,
    ) -> CircuitStateSnapshot:
        candidate_record = self._candidate_circuits.get(candidate.candidate_id)
        domain_record = self._domain_circuits.get(candidate.failure_domain_id)
        record = self._effective_record(candidate_record, domain_record, now)
        if record is None:
            return CircuitStateSnapshot(
                failure_domain_id=candidate.failure_domain_id,
                state="closed",
                consecutive_failures=0,
                remaining_cooldown=0.0,
                candidate_id=candidate.candidate_id,
            )
        if refresh:
            self._refresh_record(record, now)
        return CircuitStateSnapshot(
            failure_domain_id=candidate.failure_domain_id,
            state=record.state,
            consecutive_failures=record.consecutive_failures,
            remaining_cooldown=max(0.0, record.opened_until - now),
            candidate_id=candidate.candidate_id,
        )

    def release(self, candidate_id: str) -> None:
        with self._lock:
            candidate = self.candidate(candidate_id)
            for record in (
                self._candidate_circuits.get(candidate_id),
                self._domain_circuits.get(candidate.failure_domain_id),
            ):
                if record is not None and record.state == "half_open":
                    record.probe_in_flight = False

    def record_success(self, candidate_id: str) -> None:
        with self._lock:
            candidate = self.candidate(candidate_id)
            for record in (
                self._candidate_circuits.get(candidate_id),
                self._domain_circuits.get(candidate.failure_domain_id),
            ):
                if record is not None:
                    self._close(record)

    def record_failure(
        self,
        candidate_id: str,
        error: ModelErrorInfo,
    ) -> None:
        with self._lock:
            candidate = self.candidate(candidate_id)
            if not self.failover_config.can_failover(error):
                self.release(candidate_id)
                return
            if self.failover_config.is_candidate_unavailable(error):
                self.release(candidate_id)
                record = self._candidate_circuits.setdefault(
                    candidate_id, _CircuitRecord()
                )
                self._open(
                    record,
                    max(
                        self.circuit_config.recovery_timeout,
                        error.retry_after or 0.0,
                    ),
                )
                return
            record = self._domain_circuits.setdefault(
                candidate.failure_domain_id, _CircuitRecord()
            )
            was_half_open = record.state == "half_open"
            record.probe_in_flight = False
            record.consecutive_failures += 1
            if (
                error.retry_after is not None
                or was_half_open
                or record.consecutive_failures
                >= self.circuit_config.failure_threshold
            ):
                delay = max(
                    (
                        self.circuit_config.recovery_timeout
                        if record.consecutive_failures
                        >= self.circuit_config.failure_threshold
                        or was_half_open
                        else 0.0
                    ),
                    error.retry_after or 0.0,
                )
                self._open(record, delay)

    @staticmethod
    def _effective_record(
        candidate_record: _CircuitRecord | None,
        domain_record: _CircuitRecord | None,
        now: float,
    ) -> _CircuitRecord | None:
        records = [
            record for record in (candidate_record, domain_record) if record is not None
        ]
        if not records:
            return None
        return max(
            records,
            key=lambda item: (
                item.state != "closed",
                item.opened_until > now,
                item.opened_until,
            ),
        )

    @staticmethod
    def _release_probes(records: list[_CircuitRecord]) -> None:
        for record in records:
            record.probe_in_flight = False

    @staticmethod
    def _close(record: _CircuitRecord) -> None:
        record.state = "closed"
        record.consecutive_failures = 0
        record.opened_until = 0.0
        record.probe_in_flight = False

    def _open(self, record: _CircuitRecord, delay: float) -> None:
        record.state = "open"
        record.opened_until = self._clock() + delay
        record.probe_in_flight = False

    @staticmethod
    def _refresh_record(record: _CircuitRecord, now: float) -> None:
        if record.state == "open" and now >= record.opened_until:
            record.state = "half_open"
            record.probe_in_flight = False


__all__ = [
    "CircuitAcquireResult",
    "CircuitBreakerConfig",
    "CircuitState",
    "CircuitStateSnapshot",
    "ModelCandidate",
    "ModelFailoverConfig",
    "ModelRoute",
    "ModelRouteUnavailableError",
]
