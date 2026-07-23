"""Transport-neutral contracts for human confirmation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Awaitable, Callable, Literal, TypeAlias
from uuid import uuid4

from evopi.core.tool import ToolCall
from evopi.core.types import JsonObject, Metadata
from evopi.policy.types import HookName, RiskLevel

ConfirmationDecision: TypeAlias = Literal["approve", "deny", "cancelled"]


def _new_confirmation_id() -> str:
    return uuid4().hex


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True, kw_only=True)
class ConfirmationRequest:
    """A policy-triggered operation that requires a human decision."""

    hook: HookName
    reason: str
    risk_level: RiskLevel = "low"
    policy_names: tuple[str, ...] = ()
    tool_call: ToolCall | None = None
    arguments: JsonObject | None = None
    id: str = field(default_factory=_new_confirmation_id)
    created_at: datetime = field(default_factory=_utc_now)
    metadata: Metadata = field(default_factory=dict)


@dataclass(slots=True, kw_only=True)
class ConfirmationResponse:
    """A human decision correlated to one confirmation request."""

    request_id: str
    decision: ConfirmationDecision
    reason: str = ""
    metadata: Metadata = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.decision == "approve"


ConfirmationHandler: TypeAlias = Callable[
    ...,
    Awaitable[ConfirmationResponse] | ConfirmationResponse,
]

__all__ = [
    "ConfirmationDecision",
    "ConfirmationHandler",
    "ConfirmationRequest",
    "ConfirmationResponse",
]
