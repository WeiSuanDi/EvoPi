"""Require human confirmation for selected tools."""

from __future__ import annotations

from collections.abc import Set
from dataclasses import dataclass, field

from evopi.policy.decisions import PolicyDecision
from evopi.policy.types import HookName, PolicyContext, RiskLevel


@dataclass(slots=True)
class ToolConfirmationPolicy:
    tool_names: Set[str] = field(default_factory=frozenset)
    name: str = "tool_confirmation"
    version: str = "1.0.0"
    description: str = "Require human confirmation before selected tools execute."
    hooks: tuple[HookName, ...] = ("before_tool_call",)
    priority: int = 50
    enabled: bool = True
    source: str = "builtins"
    risk_level: RiskLevel = "medium"
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.tool_names = frozenset(self.tool_names)

    def run(self, context: PolicyContext) -> PolicyDecision:
        if context.tool_call is None or context.tool_call.name not in self.tool_names:
            return PolicyDecision()
        return PolicyDecision(
            action="require_confirmation",
            reason=f"Tool '{context.tool_call.name}' requires human confirmation",
            risk_level=self.risk_level,
            metadata={"tool_name": context.tool_call.name},
        )


__all__ = ["ToolConfirmationPolicy"]
