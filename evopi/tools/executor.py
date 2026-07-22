"""Standalone tool execution facade used outside the Agent loop."""

from __future__ import annotations

from evopi.core.tool import ToolCall, ToolResult
from evopi.tools.registry import ToolRegistry


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def execute(self, call: ToolCall) -> ToolResult:
        tool = self.registry.get(call.name)
        if tool is None:
            return ToolResult(content=f"Tool '{call.name}' not found", is_error=True)
        return await tool.execute(call.arguments)


__all__ = ["ToolExecutor"]
