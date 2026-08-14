"""Immutable public DTOs for the asynchronous RPC v2 Python client."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, TypeAlias

from evopi.core.types import JsonObject


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcEventCursor:
    stream_id: str
    sequence: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcServerInfo:
    host_id: str
    session_id: str
    cursor: RpcEventCursor
    oldest_sequence: int
    latest_sequence: int
    capacity: int
    active_tool_names: tuple[str, ...]
    policy_names: tuple[str, ...]
    steering_mode: str
    follow_up_mode: str
    capabilities: JsonObject


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcRuntimeStatus:
    active_run_id: str | None
    lifecycle: str
    session_id: str
    pending_confirmation_count: int
    last_end_reason: str | None
    last_run_error: str | None
    steering_mode: str
    follow_up_mode: str
    pending_steering_count: int
    pending_follow_up_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcMessage:
    id: str
    role: str
    content: str
    created_at: datetime
    metadata: JsonObject
    stop_reason: str | None = None
    tool_calls: tuple[RpcToolCall, ...] = ()
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool | None = None
    terminate: bool | None = None
    data: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcToolCall:
    id: str
    name: str
    arguments: JsonObject
    argument_error: JsonObject | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcConfirmationRecord:
    request_id: str
    revision: int
    status: str
    run_id: str | None
    hook: str
    reason: str
    risk_level: str
    policy_names: tuple[str, ...]
    tool_name: str | None


RpcConfirmationDecision: TypeAlias = Literal["approve", "deny", "cancelled"]


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcConfirmationAnswer:
    request_id: str
    expected_revision: int
    decision: RpcConfirmationDecision
    reason: str = ""
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcConfirmationAck:
    request_id: str
    status: str
    revision: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcInteractionReceipt:
    input_id: str
    kind: Literal["steer", "follow_up"]
    run_id: str
    position: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcEventBase:
    event_id: str
    cursor: RpcEventCursor
    event_type: str
    run_id: str | None
    created_at: datetime
    data: JsonObject


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcRunEvent(RpcEventBase):
    """Typed Run lifecycle event (``agent_start`` or ``agent_end``)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcTurnEvent(RpcEventBase):
    """Typed Turn lifecycle event."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcMessageEvent(RpcEventBase):
    """Typed message streaming event."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcToolExecutionEvent(RpcEventBase):
    """Typed Tool execution lifecycle event."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcConfirmationEvent(RpcEventBase):
    """Typed durable Confirmation lifecycle event."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcInteractionEvent(RpcEventBase):
    """Typed Steering or Follow-up lifecycle event."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcErrorEvent(RpcEventBase):
    """Typed runtime error event."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcUnknownEvent(RpcEventBase):
    """Forward-compatible event preserving unknown JSON-safe data."""


RpcClientEvent: TypeAlias = (
    RpcRunEvent
    | RpcTurnEvent
    | RpcMessageEvent
    | RpcToolExecutionEvent
    | RpcConfirmationEvent
    | RpcInteractionEvent
    | RpcErrorEvent
    | RpcUnknownEvent
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcRunResult:
    run_id: str
    end_reason: str
    turns_used: int
    max_turns: int
    messages: tuple[RpcMessage, ...]
    final_assistant: RpcMessage | None
    error: str | None
    error_info: JsonObject | None
    cursor: RpcEventCursor


@dataclass(frozen=True, slots=True, kw_only=True)
class RpcSubprocessConfig:
    command: tuple[str, ...] = ("evopi", "rpc", "--no-session")
    cwd: Path | None = None
    env: dict[str, str] | None = None
    client_name: str = "evopi-python"
    client_version: str = "1"
    handshake_timeout: float = 30.0
    shutdown_timeout: float = 5.0
    stderr_limit: int = 64 * 1024
    inbound_event_capacity: int = 1000

    def __post_init__(self) -> None:
        if not self.command or any(not isinstance(item, str) or not item for item in self.command):
            raise ValueError("command must contain non-empty strings")
        if self.handshake_timeout <= 0 or self.shutdown_timeout <= 0:
            raise ValueError("subprocess timeouts must be positive")
        if type(self.stderr_limit) is not int or self.stderr_limit < 0:
            raise ValueError("stderr_limit must be a non-negative integer")
        if type(self.inbound_event_capacity) is not int or self.inbound_event_capacity <= 0:
            raise ValueError("inbound_event_capacity must be a positive integer")


__all__ = [
    "RpcClientEvent",
    "RpcConfirmationAck",
    "RpcConfirmationAnswer",
    "RpcConfirmationDecision",
    "RpcConfirmationEvent",
    "RpcConfirmationRecord",
    "RpcErrorEvent",
    "RpcEventBase",
    "RpcEventCursor",
    "RpcInteractionEvent",
    "RpcInteractionReceipt",
    "RpcMessage",
    "RpcMessageEvent",
    "RpcRunEvent",
    "RpcRunResult",
    "RpcRuntimeStatus",
    "RpcServerInfo",
    "RpcSubprocessConfig",
    "RpcToolExecutionEvent",
    "RpcToolCall",
    "RpcTurnEvent",
    "RpcUnknownEvent",
]
