"""Keep write_file targets inside the configured workspace."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from evopi.policy.decisions import PolicyDecision
from evopi.policy.types import HookName, PolicyContext, RiskLevel


@dataclass(slots=True)
class FileWriteGuardPolicy:
    workspace: Path
    name: str = "file_write_guard"
    version: str = "1.0.0"
    description: str = "Block writes that escape the workspace."
    hooks: tuple[HookName, ...] = ("before_tool_call",)
    priority: int = 90
    enabled: bool = True
    source: str = "builtins"
    risk_level: RiskLevel = "high"
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).resolve()

    def run(self, context: PolicyContext) -> PolicyDecision:
        if context.tool_call is None or context.tool_call.name != "write_file":
            return PolicyDecision()
        value = (context.arguments or {}).get("path")
        if not isinstance(value, str) or not value:
            return PolicyDecision(action="block", reason="write_file requires a path", risk_level="high")
        target = (self.workspace / value).resolve()
        if not target.is_relative_to(self.workspace):
            return PolicyDecision(
                action="block",
                reason=f"Write target escapes workspace: {value}",
                risk_level="critical",
            )
        return PolicyDecision(action="allow", reason="Write target is inside workspace")


__all__ = ["FileWriteGuardPolicy"]
