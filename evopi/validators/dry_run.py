"""Execute a Policy against isolated contexts without registration."""

from __future__ import annotations

from collections.abc import Iterable

from evopi.policy.engine import PolicyEngine
from evopi.policy.registry import PolicyRegistry
from evopi.policy.types import Policy, PolicyContext
from evopi.validators.base import ValidationResult


async def dry_run_policy(
    policy: Policy, cases: Iterable[PolicyContext]
) -> ValidationResult:
    engine = PolicyEngine(PolicyRegistry([policy]))
    errors: list[str] = []
    count = 0
    for count, context in enumerate(cases, start=1):
        evaluation = await engine.evaluate(context)
        if evaluation.final.reason.startswith(f"Policy '{policy.name}' failed:"):
            errors.append(f"Case {count}: {evaluation.final.reason}")
    if count == 0:
        return ValidationResult(passed=False, errors=["Dry-run requires at least one case"])
    return ValidationResult(passed=not errors, errors=errors)


__all__ = ["dry_run_policy"]
