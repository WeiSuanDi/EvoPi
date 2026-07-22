"""Named governance slots exposed by BaseHarness."""

from evopi.policy.types import HookName

HOOKS: tuple[HookName, ...] = (
    "before_model_call",
    "after_model_call",
    "before_tool_call",
    "after_tool_call",
    "after_turn",
    "on_error",
)

__all__ = ["HOOKS", "HookName"]
