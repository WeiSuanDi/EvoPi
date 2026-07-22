"""Core message protocol used by the EvoPi agent loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, TypeAlias
from uuid import uuid4

from evopi.core.tool import ToolCall


MessageRole: TypeAlias = Literal["system", "user", "assistant", "tool_result"]
StopReason: TypeAlias = Literal["stop", "length", "tool_use", "error", "aborted"]


def _new_message_id() -> str:
    return uuid4().hex


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True, kw_only=True)
class BaseMessage:
    """Fields shared by every complete message stored in the context."""

    content: str
    role: MessageRole
    id: str = field(default_factory=_new_message_id)
    created_at: datetime = field(default_factory=_utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, kw_only=True)
class SystemMessage(BaseMessage):
    """Global instructions that shape the agent's behavior."""

    role: Literal["system"] = field(default="system", init=False)


@dataclass(slots=True, kw_only=True)
class UserMessage(BaseMessage):
    """Text supplied by the user."""

    role: Literal["user"] = field(default="user", init=False)


@dataclass(slots=True, kw_only=True)
class AssistantMessage(BaseMessage):
    """A complete model response, optionally requesting tool calls."""

    role: Literal["assistant"] = field(default="assistant", init=False)
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: StopReason | None = None


@dataclass(slots=True, kw_only=True)
class ToolResultMessage(BaseMessage):
    """The result of one tool call, ready to be sent back to the model."""

    tool_call_id: str
    tool_name: str
    role: Literal["tool_result"] = field(default="tool_result", init=False)
    is_error: bool = False
    terminate: bool = False


Message: TypeAlias = SystemMessage | UserMessage | AssistantMessage | ToolResultMessage


__all__ = [
    "AssistantMessage",
    "BaseMessage",
    "Message",
    "MessageRole",
    "StopReason",
    "SystemMessage",
    "ToolResultMessage",
    "UserMessage",
]
