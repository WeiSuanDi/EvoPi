"""Events emitted by the stable Core execution loop."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, Literal, TypeAlias

from evopi.core.cancellation import AbortSignal, call_with_optional_signal

CoreEventType: TypeAlias = Literal[
    "agent_start",
    "agent_end",
    "abort_requested",
    "turn_start",
    "turn_end",
    "model_start",
    "model_retry_start",
    "model_retry_end",
    "message_start",
    "message_update",
    "message_end",
    "tool_execution_start",
    "tool_execution_end",
    "policy_decision",
    "policy_evaluation",
    "confirmation_request",
    "confirmation_response",
    "session_start",
    "session_checkpoint",
    "session_error",
    "error",
]


@dataclass(slots=True, kw_only=True)
class CoreEvent:
    type: CoreEventType
    data: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


EventListener: TypeAlias = Callable[..., Awaitable[None] | None]


async def notify(
    listener: EventListener | None,
    event: CoreEvent,
    *,
    signal: AbortSignal | None = None,
) -> None:
    if listener is None:
        return
    result = call_with_optional_signal(listener, event, signal=signal)
    if inspect.isawaitable(result):
        await result


__all__ = ["CoreEvent", "CoreEventType", "EventListener", "notify"]
