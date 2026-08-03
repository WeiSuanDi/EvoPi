"""Tests for typed Dry Run cases with expected actions/args."""

from __future__ import annotations

import asyncio


from evopi.core.context import AgentContext
from evopi.core.messages import AssistantMessage
from evopi.core.tool import ToolCall
from evopi.policy.builtins import ShellSafetyPolicy
from evopi.policy.decisions import PolicyDecision
from evopi.policy.types import PolicyContext
from evopi.validators import dry_run_policy
from evopi.validators.dry_run import PolicyDryRunCase


def _context(tool_name: str = "shell_command", args: dict | None = None) -> PolicyContext:
    call = ToolCall(id="c1", name=tool_name, arguments=args or {})
    return PolicyContext(
        hook="before_tool_call",
        agent_context=AgentContext(messages=[], tools=[]),
        assistant_message=AssistantMessage(content="x"),
        tool_call=call,
        arguments=dict(call.arguments),
    )


# ---------------------------------------------------------------------------
# PolicyDryRunCase protocol
# ---------------------------------------------------------------------------

def test_typed_case_defaults() -> None:
    case = PolicyDryRunCase(case_id="c1", context=_context())
    assert case.case_id == "c1"
    assert case.expected_action is None
    assert case.expected_rewritten_args is None


def test_typed_case_with_expected_action() -> None:
    case = PolicyDryRunCase(case_id="c1", context=_context(), expected_action="allow")
    assert case.expected_action == "allow"


# ---------------------------------------------------------------------------
# Legacy compatibility — PolicyContext only (no expected action)
# ---------------------------------------------------------------------------

def test_legacy_context_only_keeps_exception_behavior() -> None:
    """Legacy PolicyContext inputs: only failures are reported, no action check."""
    policy = ShellSafetyPolicy()

    # A dangerous shell command should be blocked (error-free but final action block)
    dangerous = _context(args={"command": "rm -rf /"})
    result = asyncio.run(dry_run_policy(policy, [dangerous]))
    # Legacy mode does not fail on block — it only reports Policy failures
    assert result.passed is True


def test_legacy_context_policy_failure_is_error() -> None:
    class _BoomPolicy:
        name = "boom"
        version = "1.0"
        description = "raises"
        hooks = ("before_tool_call",)
        priority = 100
        enabled = True
        source = "test"
        risk_level = "high"
        metadata: dict = {}

        def run(self, ctx: PolicyContext) -> PolicyDecision:
            raise RuntimeError("boom")

    result = asyncio.run(
        dry_run_policy(_BoomPolicy(), [_context()])
    )
    assert result.passed is False
    assert any("boom" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Typed case expected action matching
# ---------------------------------------------------------------------------

def test_typed_case_expected_action_match() -> None:
    policy = ShellSafetyPolicy()
    case = PolicyDryRunCase(
        case_id="safe",
        context=_context(args={"command": "ls"}),
        expected_action="allow",
    )
    result = asyncio.run(dry_run_policy(policy, [case]))
    assert result.passed is True


def test_typed_case_expected_action_mismatch() -> None:
    policy = ShellSafetyPolicy()
    case = PolicyDryRunCase(
        case_id="mismatch",
        context=_context(args={"command": "ls"}),
        expected_action="block",  # shell_safety allows `ls`
    )
    result = asyncio.run(dry_run_policy(policy, [case]))
    assert result.passed is False
    assert any("mismatch" in e for e in result.errors)
    # Case ID present but raw arguments must NOT appear
    joined = "\n".join(result.errors)
    assert "ls" not in joined


def test_typed_case_expected_rewritten_args() -> None:
    class _RewritePolicy:
        name = "rewrite"
        version = "1.0"
        description = "rewrites"
        hooks = ("before_tool_call",)
        priority = 100
        enabled = True
        source = "test"
        risk_level = "low"
        metadata: dict = {}

        def run(self, ctx: PolicyContext) -> PolicyDecision:
            return PolicyDecision(
                action="rewrite_args",
                rewritten_args={"command": "echo hi", "safe": True},
            )

    case = PolicyDryRunCase(
        case_id="rw",
        context=_context(args={"command": "echo hi"}),
        expected_action="rewrite_args",
        expected_rewritten_args={"command": "echo hi", "safe": True},
    )
    result = asyncio.run(dry_run_policy(_RewritePolicy(), [case]))
    assert result.passed is True


def test_typed_case_expected_rewritten_args_mismatch() -> None:
    class _RewritePolicy:
        name = "rewrite2"
        version = "1.0"
        description = "rewrites"
        hooks = ("before_tool_call",)
        priority = 100
        enabled = True
        source = "test"
        risk_level = "low"
        metadata: dict = {}

        def run(self, ctx: PolicyContext) -> PolicyDecision:
            return PolicyDecision(
                action="rewrite_args",
                rewritten_args={"command": "echo hi", "safe": True},
            )

    case = PolicyDryRunCase(
        case_id="rw-mismatch",
        context=_context(args={"command": "echo hi"}),
        expected_action="rewrite_args",
        expected_rewritten_args={"command": "echo bye"},  # wrong
    )
    result = asyncio.run(dry_run_policy(_RewritePolicy(), [case]))
    assert result.passed is False
    assert any("rw-mismatch" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Type acceptance — mixed inputs
# ---------------------------------------------------------------------------

def test_dry_run_accepts_mixed_context_and_typed_cases() -> None:
    policy = ShellSafetyPolicy()
    cases = [
        _context(args={"command": "ls"}),
        PolicyDryRunCase(
            case_id="typed-safe",
            context=_context(args={"command": "pwd"}),
            expected_action="allow",
        ),
    ]
    result = asyncio.run(dry_run_policy(policy, cases))
    assert result.passed is True


# ---------------------------------------------------------------------------
# Export checks
# ---------------------------------------------------------------------------

def test_policy_dry_run_case_exported() -> None:
    from evopi.validators import PolicyDryRunCase as Exported

    assert Exported is PolicyDryRunCase
