"""Named governance slots exposed by BaseHarness."""

from evopi.policy.types import HookName

HOOKS: tuple[HookName, ...] = (
    "before_model_call",
    "before_model_failover",
    "after_model_call",
    "before_tool_call",
    "after_tool_call",
    "after_turn",
    "before_subagent_spawn",
    "after_subagent_run",
    "before_session_compact",
    "before_memory_write",
    "after_memory_write",
    "on_error",
)

__all__ = ["HOOKS", "HookName"]
