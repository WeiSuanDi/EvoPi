from __future__ import annotations

import asyncio

from evopi.core.context import AgentContext
from evopi.policy.builtins import ShellSafetyPolicy
from evopi.policy.types import PolicyContext
from evopi.validators import PolicySchemaValidator, dry_run_policy


def test_builtin_policy_passes_schema_and_dry_run() -> None:
    policy = ShellSafetyPolicy()
    schema_result = PolicySchemaValidator().validate(policy)
    dry_run_result = asyncio.run(
        dry_run_policy(
            policy,
            [PolicyContext(hook="before_tool_call", agent_context=AgentContext())],
        )
    )

    assert schema_result.passed is True
    assert dry_run_result.passed is True
