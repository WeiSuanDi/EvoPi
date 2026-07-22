import asyncio

from evopi.core.context import AgentContext
from evopi.core.tool import ToolCall
from evopi.policy.builtins import ShellSafetyPolicy, ToolConfirmationPolicy
from evopi.policy.engine import PolicyEngine
from evopi.policy.registry import PolicyRegistry
from evopi.policy.types import Policy, PolicyContext


def _evaluate(command: str, *policies: Policy):
    engine = PolicyEngine(PolicyRegistry(policies))
    return asyncio.run(
        engine.evaluate(
            PolicyContext(
                hook="before_tool_call",
                agent_context=AgentContext(),
                tool_call=ToolCall(
                    id="call-1",
                    name="shell_command",
                    arguments={"command": command},
                ),
                arguments={"command": command},
            )
        )
    )


def test_selected_tool_requires_confirmation() -> None:
    evaluation = _evaluate(
        "python -m pytest",
        ToolConfirmationPolicy(tool_names={"shell_command"}),
    )

    assert evaluation.final.action == "require_confirmation"
    assert evaluation.final.risk_level == "medium"
    assert evaluation.final.metadata == {"tool_name": "shell_command"}


def test_unselected_tool_is_allowed() -> None:
    policy = ToolConfirmationPolicy(tool_names={"write_file"})
    evaluation = _evaluate("python -m pytest", policy)

    assert evaluation.final.action == "allow"


def test_dangerous_shell_block_wins_over_confirmation() -> None:
    evaluation = _evaluate(
        "git reset --hard HEAD",
        ToolConfirmationPolicy(tool_names={"shell_command"}),
        ShellSafetyPolicy(),
    )

    assert {decision.action for decision in evaluation.decisions} == {
        "block",
        "require_confirmation",
    }
    assert evaluation.final.action == "block"
    assert evaluation.final.policy_name == "shell_safety"
