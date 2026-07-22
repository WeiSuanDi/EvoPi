"""Conservative checks for obviously destructive shell commands."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from evopi.policy.decisions import PolicyDecision
from evopi.policy.types import HookName, PolicyContext, RiskLevel

_DANGEROUS_PATTERNS = (
    r"\brm\s+-[^\n]*r[^\n]*f\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-[^\n]*f",
    r"\bformat(?:\.com)?\s+[a-z]:",
    r"\bshutdown\b",
    r"\brmdir\s+/s\b",
    r"\bdel\s+/[sq]",
    r"\bremove-item\b[^\n]*\s-recurse\b",
)


@dataclass(slots=True)
class ShellSafetyPolicy:
    name: str = "shell_safety"
    version: str = "1.0.0"
    description: str = "Block clearly destructive shell commands."
    hooks: tuple[HookName, ...] = ("before_tool_call",)
    priority: int = 100
    enabled: bool = True
    source: str = "builtins"
    risk_level: RiskLevel = "critical"
    metadata: dict = field(default_factory=dict)

    def run(self, context: PolicyContext) -> PolicyDecision:
        if context.tool_call is None or context.tool_call.name != "shell_command":
            return PolicyDecision()
        command = str((context.arguments or {}).get("command", ""))
        for pattern in _DANGEROUS_PATTERNS:
            if re.search(pattern, command, flags=re.IGNORECASE):
                return PolicyDecision(
                    action="block",
                    reason=f"Command matched destructive pattern: {pattern}",
                    risk_level="critical",
                )
        return PolicyDecision(action="allow", reason="No destructive shell pattern matched")


__all__ = ["ShellSafetyPolicy"]
