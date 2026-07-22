from evopi.core.tool import Tool
from evopi.tools.registry import ToolRegistry


class ToolManager:
    def __init__(self) -> None:
        self.registry = ToolRegistry()

    def register(self, tool: Tool, *, replace: bool = False) -> None:
        self.registry.register(tool, replace=replace)

    def all(self) -> list[Tool]:
        return list(self.registry)


__all__ = ["ToolManager"]
