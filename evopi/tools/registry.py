"""Name-indexed collection of executable tools."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from evopi.core.tool import Tool


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool, *, replace: bool = False) -> None:
        if tool.name in self._tools and not replace:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> Tool:
        try:
            return self._tools.pop(name)
        except KeyError as exc:
            raise KeyError(f"Tool '{name}' is not registered") from exc

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def require(self, name: str) -> Tool:
        tool = self.get(name)
        if tool is None:
            raise KeyError(f"Tool '{name}' is not registered")
        return tool

    def definitions(self) -> list[dict]:
        return [tool.definition() for tool in self._tools.values()]

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)


__all__ = ["ToolRegistry"]
