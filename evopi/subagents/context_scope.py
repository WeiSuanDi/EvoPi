"""SubAgent context scope — what a child agent can see and do."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from evopi.core.messages import Message


@dataclass(slots=True, kw_only=True)
class SubAgentScope:
    """Defines the context and capability boundary of a sub-agent.

    The parent agent decides what the child can see (messages, tools) and
    how long it can run (max_turns).  This is the contract between parent
    and child — the child cannot escape its scope.
    """

    system_prompt: str = ""
    messages: list[Message] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    max_turns: int = 5
    metadata: dict[str, Any] = field(default_factory=dict)

    def restrict(self, *, max_turns: int | None = None) -> SubAgentScope:
        """Return a more restrictive copy (used by Policy)."""
        return SubAgentScope(
            system_prompt=self.system_prompt,
            messages=list(self.messages),
            tool_names=list(self.tool_names),
            max_turns=max_turns if max_turns is not None else self.max_turns,
            metadata=dict(self.metadata),
        )


__all__ = ["SubAgentScope"]
