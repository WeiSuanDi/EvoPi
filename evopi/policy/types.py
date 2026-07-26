"""Policy contracts and hook context."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol, TypeAlias

from evopi.core.context import AgentContext
from evopi.core.messages import AssistantMessage
from evopi.core.model_errors import ModelErrorInfo
from evopi.core.tool import ToolCall, ToolResult
from evopi.core.types import JsonObject, Metadata

HookName: TypeAlias = Literal[
    "before_model_call",
    "after_model_call",
    "before_tool_call",
    "after_tool_call",
    "after_turn",
    "before_subagent_spawn",
    "after_subagent_run",
    "before_session_compact",
    "before_memory_write",
    "after_memory_write",
    "on_error",
]
RiskLevel: TypeAlias = Literal["low", "medium", "high", "critical"]


@dataclass(slots=True, kw_only=True)
class PolicyContext:
    hook: HookName
    agent_context: AgentContext
    assistant_message: AssistantMessage | None = None
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    arguments: JsonObject | None = None
    error: str | None = None
    error_info: ModelErrorInfo | None = None
    aborted: bool = False
    tool_plugin_source: str | None = None
    policy_plugin_source: str | None = None
    metadata: Metadata = field(default_factory=dict)


class Policy(Protocol):
    name: str
    version: str
    description: str
    hooks: tuple[HookName, ...]
    priority: int
    enabled: bool
    source: str
    risk_level: RiskLevel
    metadata: Metadata

    def run(self, context: PolicyContext) -> Any: ...


PolicyRunner: TypeAlias = Callable[[PolicyContext], Awaitable[Any] | Any]

__all__ = ["HookName", "Policy", "PolicyContext", "RiskLevel"]
