"""Events emitted by the stable Core execution loop."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, Literal, TypeAlias

CoreEventType: TypeAlias = Literal[
    "agent_start",
    "agent_end",
    "turn_start",
    "turn_end",
    "model_start",
    "message_start",
    "message_update",
    "message_end",
    "tool_execution_start",
    "tool_execution_end",
    "policy_decision",
    "policy_evaluation",
    "confirmation_request",
    "confirmation_response",
    "error",
]


@dataclass(slots=True, kw_only=True)
class CoreEvent:
    type: CoreEventType
    data: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


EventListener: TypeAlias = Callable[[CoreEvent], Awaitable[None] | None]


async def notify(listener: EventListener | None, event: CoreEvent) -> None:
    if listener is None:
        return
    result = listener(event)
    if inspect.isawaitable(result):
        await result


__all__ = ["CoreEvent", "CoreEventType", "EventListener", "notify"]
