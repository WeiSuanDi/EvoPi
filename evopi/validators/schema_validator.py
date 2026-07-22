"""Cheap structural checks for candidate Policy assets."""

from __future__ import annotations

from evopi.harness.hooks import HOOKS
from evopi.policy.types import Policy
from evopi.validators.base import ValidationResult


class PolicySchemaValidator:
    def validate(self, policy: Policy) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        for field_name in (
            "name",
            "version",
            "description",
            "hooks",
            "priority",
            "enabled",
            "source",
            "risk_level",
            "metadata",
        ):
            if not hasattr(policy, field_name):
                errors.append(f"Missing Policy field: {field_name}")

        name = getattr(policy, "name", "")
        if not isinstance(name, str) or not name.strip():
            errors.append("Policy name must be a non-empty string")
        hooks = getattr(policy, "hooks", ())
        if not hooks:
            errors.append("Policy must bind at least one hook")
        else:
            unknown = [hook for hook in hooks if hook not in HOOKS]
            if unknown:
                errors.append(f"Unknown hooks: {', '.join(unknown)}")
        if not callable(getattr(policy, "run", None)):
            errors.append("Policy must define run(context)")
        if getattr(policy, "source", "") == "generated":
            warnings.append("Generated Policy requires supervisor review before enablement")
        return ValidationResult(passed=not errors, errors=errors, warnings=warnings)


__all__ = ["PolicySchemaValidator"]
