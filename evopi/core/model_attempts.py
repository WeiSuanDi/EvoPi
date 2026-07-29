"""Provider-neutral model-attempt routing contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from evopi.core.cancellation import AbortSignal
from evopi.core.context import AgentContext
from evopi.core.model import Model
from evopi.core.model_errors import ModelErrorInfo


@dataclass(slots=True, frozen=True, kw_only=True)
class ModelAttemptInfo:
    """Safe identity and ordering data for one actual model request."""

    route_id: str
    candidate_id: str
    provider: str
    model: str
    failure_domain_id: str
    attempt: int
    route_round: int


@dataclass(slots=True, frozen=True, kw_only=True)
class ModelAttemptSelection:
    model: Model
    info: ModelAttemptInfo
    delay: float = 0.0

    def __post_init__(self) -> None:
        if self.delay < 0:
            raise ValueError("model attempt delay cannot be negative")


class ModelAttemptRouter(Protocol):
    async def select_initial(
        self,
        *,
        context: AgentContext,
        attempt: int,
        run_id: str | None,
        turn: int,
        signal: AbortSignal | None,
    ) -> ModelAttemptSelection: ...

    async def select_after_failure(
        self,
        *,
        context: AgentContext,
        previous: ModelAttemptSelection,
        error: Exception,
        error_info: ModelErrorInfo | None,
        next_attempt: int,
        max_attempts: int,
        run_id: str | None,
        turn: int,
        signal: AbortSignal | None,
    ) -> ModelAttemptSelection | None: ...

    async def record_failure(
        self,
        selection: ModelAttemptSelection,
        error: Exception,
        error_info: ModelErrorInfo | None,
    ) -> None: ...

    async def record_success(self, selection: ModelAttemptSelection) -> None: ...

    async def record_abandoned(self, selection: ModelAttemptSelection) -> None: ...

    async def authorize_attempt(
        self,
        selection: ModelAttemptSelection,
        context: AgentContext,
        signal: AbortSignal | None,
    ) -> None: ...

    async def close(self) -> None: ...


__all__ = [
    "ModelAttemptInfo",
    "ModelAttemptRouter",
    "ModelAttemptSelection",
]
