"""Structured Policy output and merged evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from evopi.core.tool import ToolResult
from evopi.core.types import JsonObject, Metadata

PolicyAction: TypeAlias = Literal[
    "allow",
    "block",
    "rewrite_args",
    "require_confirmation",
    "trigger_validation",
    "terminate",
]


@dataclass(slots=True, kw_only=True)
class PolicyDecision:
    action: PolicyAction = "allow"
    reason: str = ""
    risk_level: str = "low"
    rewritten_args: JsonObject | None = None
    replacement_result: ToolResult | None = None
    metadata: Metadata = field(default_factory=dict)
    policy_name: str | None = None


@dataclass(slots=True, kw_only=True)
class PolicyEvaluation:
    final: PolicyDecision
    decisions: list[PolicyDecision] = field(default_factory=list)
    arguments: JsonObject | None = None
    tool_result: ToolResult | None = None


__all__ = ["PolicyAction", "PolicyDecision", "PolicyEvaluation"]
