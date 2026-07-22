"""Bound tool output before it is fed back to the model."""

from __future__ import annotations

from dataclasses import dataclass, field

from evopi.core.tool import ToolResult
from evopi.policy.decisions import PolicyDecision
from evopi.policy.types import HookName, PolicyContext, RiskLevel


@dataclass(slots=True)
class OutputTruncationPolicy:
    max_chars: int = 20_000
    name: str = "output_truncation"
    version: str = "1.0.0"
    description: str = "Truncate oversized tool output before model feedback."
    hooks: tuple[HookName, ...] = ("after_tool_call",)
    priority: int = 50
    enabled: bool = True
    source: str = "builtins"
    risk_level: RiskLevel = "low"
    metadata: dict = field(default_factory=dict)

    def run(self, context: PolicyContext) -> PolicyDecision:
        result = context.tool_result
        if result is None or len(result.content) <= self.max_chars:
            return PolicyDecision(action="allow", reason="Tool output is within the limit")
        omitted = len(result.content) - self.max_chars
        replacement = ToolResult(
            content=(
                result.content[: self.max_chars]
                + f"\n\n[output truncated: {omitted} characters omitted]"
            ),
            is_error=result.is_error,
            terminate=result.terminate,
            metadata={**result.metadata, "truncated": True, "omitted_characters": omitted},
        )
        return PolicyDecision(
            action="allow",
            reason="Tool output was truncated",
            replacement_result=replacement,
        )


__all__ = ["OutputTruncationPolicy"]
