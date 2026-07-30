from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from evopi.policy.decisions import PolicyDecision
from evopi.policy.builtins import (
    FileWriteGuardPolicy,
    OutputTruncationPolicy,
    ShellSafetyPolicy,
    TestAfterEditPolicy,
    ToolConfirmationPolicy,
)
from evopi.policy.registry import PolicyPack
from evopi.policy.types import HookName, Policy, PolicyContext, RiskLevel


@dataclass(slots=True)
class FinalTurnToolPolicy:
    """Fail closed if a model fabricates a ToolCall during Coding finalization."""

    is_final_turn: Callable[[], bool]
    name: str = "coding_final_turn_guard"
    version: str = "1.0.0"
    description: str = "Block ToolCalls during the final Coding model Turn."
    hooks: tuple[HookName, ...] = ("before_tool_call",)
    priority: int = 100
    enabled: bool = True
    source: str = "builtins"
    risk_level: RiskLevel = "high"
    metadata: dict = field(default_factory=dict)

    def run(self, context: PolicyContext) -> PolicyDecision:
        if context.tool_call is None or not self.is_final_turn():
            return PolicyDecision()
        return PolicyDecision(
            action="block",
            reason="Tool execution is disabled during the final turn",
            risk_level="high",
        )


def coding_policy_pack(
    workspace: str | Path,
    *,
    max_output_chars: int = 20_000,
    is_final_turn: Callable[[], bool] | None = None,
) -> PolicyPack:
    plugins_dir = Path.home() / ".evopi" / "plugins"
    local_plugins = Path(workspace) / ".evopi" / "plugins"
    policies: list[Policy] = [
        ShellSafetyPolicy(),
        ToolConfirmationPolicy(tool_names={"shell_command"}),
        FileWriteGuardPolicy(
            workspace=Path(workspace),
            extra_allowed_dirs=(plugins_dir, local_plugins),
        ),
        OutputTruncationPolicy(max_chars=max_output_chars),
        TestAfterEditPolicy(),
    ]
    if is_final_turn is not None:
        policies.append(FinalTurnToolPolicy(is_final_turn=is_final_turn))
    return PolicyPack(
        "coding",
        policies,
    )


__all__ = ["FinalTurnToolPolicy", "coding_policy_pack"]
