"""Structured outcomes for one Core agent run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from evopi.core.messages import AssistantMessage
from evopi.core.model_errors import ModelErrorInfo

AgentEndReason: TypeAlias = Literal[
    "completed",
    "terminated",
    "aborted",
    "error",
    "turn_limit",
    "deadline_exceeded",
]


@dataclass(slots=True, frozen=True, kw_only=True)
class AgentLoopResult:
    message: AssistantMessage
    end_reason: AgentEndReason


@dataclass(slots=True, frozen=True, kw_only=True)
class AgentRunState:
    run_id: str
    end_reason: AgentEndReason
    error: str | None = None
    error_info: ModelErrorInfo | None = None


__all__ = ["AgentEndReason", "AgentLoopResult", "AgentRunState"]
