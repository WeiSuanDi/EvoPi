from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from evopi.core.context import AgentContext
from evopi.core.tool import ToolCall, ToolResult
from evopi.policy.builtins import OutputTruncationPolicy, ShellSafetyPolicy
from evopi.policy.decisions import PolicyDecision
from evopi.policy.engine import PolicyEngine
from evopi.policy.registry import PolicyRegistry
from evopi.policy.registry import PolicyPack
from evopi.policy.types import PolicyContext


@dataclass
class RewritePolicy:
    name: str = "rewrite"
    version: str = "1"
    description: str = "rewrite"
    hooks: tuple = ("before_tool_call",)
    priority: int = 10
    enabled: bool = True
    source: str = "test"
    risk_level: str = "low"
    metadata: dict = field(default_factory=dict)

    def run(self, context: PolicyContext) -> PolicyDecision:
        return PolicyDecision(action="rewrite_args", rewritten_args={"value": "safe"})


@dataclass
class BlockPolicy(RewritePolicy):
    name: str = "block"
    priority: int = 1

    def run(self, context: PolicyContext) -> PolicyDecision:
        assert context.arguments == {"value": "safe"}
        return PolicyDecision(action="block", reason="blocked")


def test_policy_engine_applies_rewrites_but_block_wins() -> None:
    engine = PolicyEngine(PolicyRegistry([RewritePolicy(), BlockPolicy()]))
    evaluation = asyncio.run(
        engine.evaluate(
            PolicyContext(
                hook="before_tool_call",
                agent_context=AgentContext(),
                arguments={"value": "unsafe"},
            )
        )
    )

    assert evaluation.arguments == {"value": "safe"}
    assert evaluation.final.action == "block"


def test_builtin_shell_policy_blocks_destructive_command() -> None:
    engine = PolicyEngine(PolicyRegistry([ShellSafetyPolicy()]))
    evaluation = asyncio.run(
        engine.evaluate(
            PolicyContext(
                hook="before_tool_call",
                agent_context=AgentContext(),
                tool_call=ToolCall(id="1", name="shell_command", arguments={}),
                arguments={"command": "git reset --hard HEAD"},
            )
        )
    )

    assert evaluation.final.action == "block"


def test_output_truncation_replaces_result_without_marking_error() -> None:
    engine = PolicyEngine(PolicyRegistry([OutputTruncationPolicy(max_chars=5)]))
    evaluation = asyncio.run(
        engine.evaluate(
            PolicyContext(
                hook="after_tool_call",
                agent_context=AgentContext(),
                tool_result=ToolResult(content="0123456789"),
            )
        )
    )

    assert evaluation.final.action == "allow"
    assert evaluation.tool_result is not None
    assert evaluation.tool_result.content.startswith("01234")
    assert evaluation.tool_result.metadata["truncated"] is True


def test_registry_orders_disables_and_loads_policy_pack() -> None:
    low = RewritePolicy(name="low", priority=1)
    high = RewritePolicy(name="high", priority=100)
    registry = PolicyRegistry([low, high])

    assert [policy.name for policy in registry.for_hook("before_tool_call")] == [
        "high",
        "low",
    ]

    registry.set_enabled("high", False)
    assert [policy.name for policy in registry.for_hook("before_tool_call")] == ["low"]

    replacement = RewritePolicy(name="low", priority=200)
    registry.load_pack(PolicyPack("strict", [replacement], version="2"))
    assert registry.get("low") is replacement
