"""The complete provider-neutral context visible to one model call."""

from __future__ import annotations

from dataclasses import dataclass, field

from evopi.core.messages import Message, SystemMessage
from evopi.core.tool import Tool


@dataclass(slots=True, kw_only=True)
class AgentContext:
    messages: list[Message] = field(default_factory=list)
    tools: list[Tool] = field(default_factory=list)

    @property
    def system_messages(self) -> list[SystemMessage]:
        return [message for message in self.messages if isinstance(message, SystemMessage)]

    def append(self, message: Message) -> None:
        self.messages.append(message)

    def snapshot(self) -> "AgentContext":
        return AgentContext(messages=list(self.messages), tools=list(self.tools))


__all__ = ["AgentContext"]
