"""SubAgent result validation — what the parent can trust."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from evopi.core.messages import Message
from evopi.core.run import AgentEndReason


@dataclass(slots=True, kw_only=True)
class SubAgentResult:
    """The validated output of a completed sub-agent run.

    The parent agent receives this structured result, not raw messages.
    Policy can inspect it before the parent sees it.
    """

    content: str
    success: bool = True
    end_reason: AgentEndReason = "completed"
    messages: list[Message] = field(default_factory=list)
    tool_calls_made: int = 0
    turns_used: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


def validate_subagent_result(
    result: SubAgentResult,
    *,
    allowed_tools: set[str] | None = None,
    max_output_chars: int = 10_000,
) -> SubAgentResult:
    """Validate and sanitize a sub-agent result.

    - Truncates overlong content
    - Marks errors when end_reason is not completed
    """
    if result.end_reason != "completed":
        result = SubAgentResult(
            content=(
                f"Sub-agent ended with {result.end_reason}: {result.content}"
            ),
            success=False,
            end_reason=result.end_reason,
            messages=result.messages,
            tool_calls_made=result.tool_calls_made,
            turns_used=result.turns_used,
            metadata=dict(result.metadata),
        )

    if len(result.content) > max_output_chars:
        result = SubAgentResult(
            content=result.content[:max_output_chars]
            + f"\n... (truncated {len(result.content) - max_output_chars} chars)",
            success=result.success,
            end_reason=result.end_reason,
            messages=result.messages,
            tool_calls_made=result.tool_calls_made,
            turns_used=result.turns_used,
            metadata=dict(result.metadata),
        )

    return result


__all__ = ["SubAgentResult", "validate_subagent_result"]
