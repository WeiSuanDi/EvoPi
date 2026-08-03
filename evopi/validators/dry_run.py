"""Execute a Policy against isolated contexts without registration."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from evopi.core.types import JsonObject
from evopi.policy.decisions import PolicyAction
from evopi.policy.engine import PolicyEngine
from evopi.policy.registry import PolicyRegistry
from evopi.policy.types import Policy, PolicyContext
from evopi.validators.base import ValidationResult

_VALID_ACTIONS = frozenset(
    {"allow", "block", "rewrite_args", "require_confirmation", "trigger_validation", "terminate"}
)


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyDryRunCase:
    """A typed Dry Run case with expected Policy behavior.

    Legacy ``PolicyContext`` inputs keep the historical exception-only
    behavior.  Typed cases additionally compare the final action and, when
    supplied, the rewritten arguments.  Mismatches are stable Validation
    errors that contain the case ID but never raw arguments.
    """

    case_id: str
    context: PolicyContext
    expected_action: PolicyAction | None = None
    expected_rewritten_args: JsonObject | None = None

    def __post_init__(self) -> None:
        if not self.case_id or not self.case_id.strip():
            raise ValueError("case_id must be a non-empty string")
        if self.expected_action is not None and self.expected_action not in _VALID_ACTIONS:
            raise ValueError(
                f"expected_action must be a valid Policy action, got {self.expected_action!r}"
            )


DryRunInput: Iterable[PolicyContext | PolicyDryRunCase]


async def dry_run_policy(
    policy: Policy, cases: Iterable[PolicyContext | PolicyDryRunCase]
) -> ValidationResult:
    engine = PolicyEngine(PolicyRegistry([policy]))
    errors: list[str] = []
    count = 0
    for count, case in enumerate(cases, start=1):
        if isinstance(case, PolicyDryRunCase):
            context = case.context
        else:
            context = case
        evaluation = await engine.evaluate(context)
        if evaluation.final.reason.startswith(f"Policy '{policy.name}' failed:"):
            errors.append(f"Case {count}: {evaluation.final.reason}")
        elif isinstance(case, PolicyDryRunCase):
            errors.extend(_compare_typed_case(policy, case, evaluation.final))
    if count == 0:
        return ValidationResult(passed=False, errors=["Dry-run requires at least one case"])
    return ValidationResult(passed=not errors, errors=errors)


def _compare_typed_case(
    policy: Policy,
    case: PolicyDryRunCase,
    final: object,
) -> list[str]:
    """Compare a typed case against the final merged decision."""
    from evopi.policy.decisions import PolicyDecision

    if not isinstance(final, PolicyDecision):
        return [f"Case {case.case_id}: final decision is not a PolicyDecision"]
    errors: list[str] = []
    if case.expected_action is not None and final.action != case.expected_action:
        errors.append(
            f"Case {case.case_id}: expected action {case.expected_action!r}, "
            f"got {final.action!r}"
        )
    if (
        case.expected_rewritten_args is not None
        and final.rewritten_args != case.expected_rewritten_args
    ):
        errors.append(
            f"Case {case.case_id}: rewritten arguments do not match expectation"
        )
    return errors


__all__ = ["PolicyDryRunCase", "dry_run_policy"]
