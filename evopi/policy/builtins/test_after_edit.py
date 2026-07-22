"""Evolution-ready validation hint after file edits."""

from __future__ import annotations

from dataclasses import dataclass, field

from evopi.policy.decisions import PolicyDecision
from evopi.policy.types import HookName, PolicyContext, RiskLevel


@dataclass(slots=True)
class TestAfterEditPolicy:
    name: str = "test_after_edit"
    version: str = "0.1.0"
    description: str = "Request validation after a turn that edited files."
    hooks: tuple[HookName, ...] = ("after_turn",)
    priority: int = 10
    enabled: bool = False
    source: str = "builtins"
    risk_level: RiskLevel = "low"
    metadata: dict = field(default_factory=lambda: {"interface_only": True})

    def run(self, context: PolicyContext) -> PolicyDecision:
        if context.metadata.get("edited_files"):
            return PolicyDecision(
                action="trigger_validation",
                reason="Files were edited during this turn",
            )
        return PolicyDecision()


__all__ = ["TestAfterEditPolicy"]
