"""Provider-neutral model streaming events."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from evopi.core.messages import AssistantMessage, StopReason
from evopi.core.tool import ToolCall
from evopi.core.types import Metadata


@dataclass(slots=True, kw_only=True)
class TextDelta:
    type: Literal["text_delta"] = "text_delta"
    delta: str


@dataclass(slots=True, kw_only=True)
class ToolCallDelta:
    type: Literal["tool_call_delta"] = "tool_call_delta"
    index: int
    arguments_delta: str = ""
    tool_call_id: str | None = None
    tool_name: str | None = None


@dataclass(slots=True, kw_only=True)
class ModelComplete:
    type: Literal["complete"] = "complete"
    message: AssistantMessage


ModelStreamEvent: TypeAlias = TextDelta | ToolCallDelta | ModelComplete


class AssistantMessageBuilder:
    """Incrementally construct one complete provider-neutral assistant message."""

    def __init__(self) -> None:
        self._text_parts: list[str] = []
        self._tool_calls: dict[int, dict[str, str]] = {}

    def add_text(self, delta: str) -> None:
        self._text_parts.append(delta)

    def add_tool_call_delta(
        self,
        *,
        index: int,
        arguments_delta: str = "",
        tool_call_id: str | None = None,
        tool_name: str | None = None,
    ) -> None:
        state = self._tool_calls.setdefault(
            index, {"id": "", "name": "", "arguments": ""}
        )
        if tool_call_id:
            state["id"] = tool_call_id
        if tool_name:
            state["name"] += tool_name
        state["arguments"] += arguments_delta

    def build(
        self,
        *,
        stop_reason: StopReason,
        metadata: Metadata | None = None,
    ) -> AssistantMessage:
        calls = [
            ToolCall(
                id=state["id"] or f"call-{index}",
                name=state["name"],
                arguments=_parse_arguments(state["arguments"]),
            )
            for index, state in sorted(self._tool_calls.items())
        ]
        return AssistantMessage(
            content="".join(self._text_parts),
            tool_calls=calls,
            stop_reason=stop_reason,
            metadata=dict(metadata or {}),
        )


def _parse_arguments(value: str) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"_raw": value}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


__all__ = [
    "AssistantMessageBuilder",
    "ModelComplete",
    "ModelStreamEvent",
    "TextDelta",
    "ToolCallDelta",
]
