"""Conservative Policy chain execution."""

from __future__ import annotations

import inspect
from dataclasses import replace

from evopi.policy.decisions import PolicyDecision, PolicyEvaluation
from evopi.policy.registry import PolicyRegistry
from evopi.policy.types import PolicyContext

_PRECEDENCE = {
    "allow": 0,
    "trigger_validation": 1,
    "rewrite_args": 2,
    "terminate": 3,
    "require_confirmation": 4,
    "block": 5,
}


class PolicyEngine:
    def __init__(self, registry: PolicyRegistry) -> None:
        self.registry = registry

    async def evaluate(self, context: PolicyContext) -> PolicyEvaluation:
        decisions: list[PolicyDecision] = []
        arguments = dict(context.arguments) if context.arguments is not None else None
        tool_result = context.tool_result

        for policy in self.registry.for_hook(context.hook):
            current = replace(context, arguments=arguments, tool_result=tool_result)
            try:
                decision = policy.run(current)
                if inspect.isawaitable(decision):
                    decision = await decision
                if decision is None:
                    decision = PolicyDecision()
                if not isinstance(decision, PolicyDecision):
                    raise TypeError("Policy.run() must return PolicyDecision or None")
            except Exception as exc:
                decision = PolicyDecision(
                    action="block",
                    reason=f"Policy '{policy.name}' failed: {type(exc).__name__}: {exc}",
                    risk_level="high",
                )

            decision.policy_name = policy.name
            decisions.append(decision)
            if decision.rewritten_args is not None:
                arguments = dict(decision.rewritten_args)
            if decision.replacement_result is not None:
                tool_result = decision.replacement_result

        final = max(
            decisions,
            key=lambda item: _PRECEDENCE[item.action],
            default=PolicyDecision(action="allow", reason="No policy blocked this hook"),
        )
        return PolicyEvaluation(
            final=final,
            decisions=decisions,
            arguments=arguments,
            tool_result=tool_result,
        )


__all__ = ["PolicyEngine"]
