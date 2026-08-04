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
    "turn_budget_applied",
    "model_start",
    "model_retry_start",
    "model_retry_end",
    "model_failover_start",
    "model_failover_end",
    "model_circuit_state_changed",
    "model_candidate_skipped",
    "message_start",
    "message_update",
    "message_end",
    "tool_execution_start",
    "tool_execution_end",
    "policy_decision",
    "policy_evaluation",
    "confirmation_request",
    "confirmation_response",
    "confirmation_state_changed",
    "interaction_queued",
    "interaction_delivered",
    "interaction_cleared",
    "session_start",
    "session_checkpoint",
    "session_error",
    "session_leaf_selected",
    "session_compaction_start",
    "session_compaction_end",
    "session_compaction_error",
    "session_merge_start",
    "session_merge_end",
    "session_merge_error",
    "memory_write_start",
    "memory_write_end",
    "memory_write_error",
    "skills_selected",
    "plugin_reload",
    "plugin_command_start",
    "plugin_command_end",
    "plugin_command_error",
    "plugin_handler_error",
    "plugin_prompt_applied",
    "plugin_tools_changed",
    "plugin_state_changed",
    "plugin_ui_request",
    "plugin_ui_response",
    "policy_runtime_reload_start",
    "policy_runtime_reload_end",
    "policy_runtime_reload_error",
    "policy_artifact_loaded",
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
