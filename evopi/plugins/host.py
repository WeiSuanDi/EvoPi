"""Host-neutral runtime services exposed to approved Plugins."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Awaitable, Literal, Protocol, TypeAlias, overload

from evopi.core.tool import Tool
from evopi.core.types import JsonObject

PluginToolScope: TypeAlias = Literal["run", "session"]
PluginCommandHandler: TypeAlias = Callable[..., Awaitable[Any] | Any]
PluginPromptProvider: TypeAlias = Callable[
    ["PluginPromptContext"], Awaitable[str | None] | str | None
]


class PluginRuntimeError(RuntimeError):
    """Base error raised by the approved Plugin runtime."""


class PluginContractError(PluginRuntimeError):
    """Raised when a Plugin uses the public API outside its contract."""


class PluginUIUnavailableError(PluginRuntimeError):
    """Raised when an interactive UI operation has no attached host."""


@dataclass(slots=True, frozen=True, kw_only=True)
class PluginToolDescriptor:
    name: str
    description: str
    effects: tuple[str, ...]
    plugin_source: str | None = None


class PluginUI(Protocol):
    async def notify(self, message: str, *, level: str = "info") -> None: ...

    async def confirm(self, title: str, message: str) -> bool: ...

    async def select(
        self, title: str, options: Sequence[str]
    ) -> str: ...

    async def input(self, title: str, prompt: str = "") -> str: ...

    async def set_status(self, key: str, text: str | None) -> None: ...


class NullPluginUI:
    """Fail-closed UI used by library and non-interactive Harnesses."""

    async def notify(self, message: str, *, level: str = "info") -> None:
        return None

    async def confirm(self, title: str, message: str) -> bool:
        return False

    async def select(self, title: str, options: Sequence[str]) -> str:
        raise PluginUIUnavailableError("Plugin selection requires an interactive UI")

    async def input(self, title: str, prompt: str = "") -> str:
        raise PluginUIUnavailableError("Plugin input requires an interactive UI")

    async def set_status(self, key: str, text: str | None) -> None:
        return None


class PluginStateStore:
    """Plugin-namespaced state facade; PR2 supplies Session persistence."""

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}
        self._get_values: Callable[[], JsonObject] | None = None
        self._set_value: Callable[[str, Any], None] | None = None
        self._delete_value: Callable[[str], None] | None = None

    def bind(
        self,
        *,
        get_values: Callable[[], JsonObject],
        set_value: Callable[[str, Any], None],
        delete_value: Callable[[str], None],
    ) -> None:
        self._get_values = get_values
        self._set_value = set_value
        self._delete_value = delete_value

    def get(self, key: str, default: Any = None) -> Any:
        return self.snapshot().get(key, default)

    def set(self, key: str, value: Any) -> None:
        if self._set_value is not None:
            self._set_value(key, value)
            return
        self._values[key] = value

    def delete(self, key: str) -> None:
        if self._delete_value is not None:
            self._delete_value(key)
            return
        self._values.pop(key, None)

    def snapshot(self) -> JsonObject:
        if self._get_values is not None:
            return dict(self._get_values())
        return dict(self._values)


class PluginTools:
    """Legacy-compatible Tool registration list plus bound runtime controller."""

    def __init__(self, plugin_name: str, plugin_version: str) -> None:
        self.plugin_name = plugin_name
        self.plugin_version = plugin_version
        self._registered: list[Tool] = []
        self._get_all: Callable[[], list[Tool]] | None = None
        self._get_active: Callable[[], list[Tool]] | None = None
        self._set_active: Callable[[str, tuple[str, ...], PluginToolScope], None] | None = None
        self._clear_active: Callable[[str], None] | None = None

    def register(self, tool: Tool, *, replace: bool = False) -> Tool:
        tool.metadata.update(
            plugin_source=self.plugin_name,
            plugin_version=self.plugin_version,
            plugin_replace=replace,
        )
        if "effects" not in tool.metadata:
            tool.metadata["effects"] = ["unknown"]
        self._registered.append(tool)
        return tool

    def bind(
        self,
        *,
        get_all: Callable[[], list[Tool]],
        get_active: Callable[[], list[Tool]],
        set_active: Callable[[str, tuple[str, ...], PluginToolScope], None],
        clear_active: Callable[[str], None],
    ) -> None:
        self._get_all = get_all
        self._get_active = get_active
        self._set_active = set_active
        self._clear_active = clear_active

    def all(self) -> tuple[PluginToolDescriptor, ...]:
        tools = self._require_bound(self._get_all, "query all Tools")()
        return tuple(_tool_descriptor(tool) for tool in tools)

    def active(self) -> tuple[PluginToolDescriptor, ...]:
        tools = self._require_bound(self._get_active, "query active Tools")()
        return tuple(_tool_descriptor(tool) for tool in tools)

    def set_active(
        self,
        names: Sequence[str],
        *,
        scope: PluginToolScope = "session",
    ) -> None:
        callback = self._require_bound(self._set_active, "change active Tools")
        callback(self.plugin_name, tuple(names), scope)

    def clear_active_override(self) -> None:
        callback = self._require_bound(self._clear_active, "clear active Tools")
        callback(self.plugin_name)

    @staticmethod
    def _require_bound(value: Any, operation: str) -> Any:
        if value is None:
            raise PluginContractError(
                f"Plugin runtime is not active; cannot {operation} during register()"
            )
        return value

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._registered)

    def __len__(self) -> int:
        return len(self._registered)

    @overload
    def __getitem__(self, index: int) -> Tool: ...

    @overload
    def __getitem__(self, index: slice) -> list[Tool]: ...

    def __getitem__(self, index: int | slice) -> Tool | list[Tool]:
        return self._registered[index]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, PluginTools):
            return self._registered == other._registered
        return self._registered == other


@dataclass(slots=True, frozen=True, kw_only=True)
class PluginRuntimeContext:
    plugin_name: str
    plugin_version: str
    workspace: str
    session_id: str
    tools: PluginTools
    state: PluginStateStore
    ui: PluginUI


@dataclass(slots=True, frozen=True, kw_only=True)
class PluginCommandContext:
    command_name: str
    raw: str
    runtime: PluginRuntimeContext


@dataclass(slots=True, frozen=True, kw_only=True)
class PluginPromptContext:
    plugin_name: str
    plugin_version: str
    workspace: str
    session_id: str
    active_tools: tuple[str, ...]
    state: JsonObject = field(default_factory=dict)


@dataclass(slots=True, frozen=True, kw_only=True)
class PluginCommandSpec:
    name: str
    handler: PluginCommandHandler
    description: str = ""
    usage: str = ""
    runtime_plugin_name: str = ""

    def __iter__(self) -> Iterator[Any]:
        yield self.name
        yield self.handler

    def __getitem__(self, index: int) -> Any:
        return (self.name, self.handler)[index]


@dataclass(slots=True, frozen=True, kw_only=True)
class PluginPromptFragment:
    name: str
    provider: PluginPromptProvider
    priority: int = 0


def _tool_descriptor(tool: Tool) -> PluginToolDescriptor:
    raw_effects = tool.metadata.get("effects", ["unknown"])
    effects = (
        tuple(str(item) for item in raw_effects)
        if isinstance(raw_effects, list)
        else ("unknown",)
    )
    source = tool.metadata.get("plugin_source")
    return PluginToolDescriptor(
        name=tool.name,
        description=tool.description,
        effects=effects,
        plugin_source=source if isinstance(source, str) else None,
    )


__all__ = [
    "NullPluginUI",
    "PluginCommandContext",
    "PluginCommandHandler",
    "PluginCommandSpec",
    "PluginContractError",
    "PluginPromptContext",
    "PluginPromptFragment",
    "PluginPromptProvider",
    "PluginRuntimeContext",
    "PluginRuntimeError",
    "PluginStateStore",
    "PluginToolDescriptor",
    "PluginToolScope",
    "PluginTools",
    "PluginUI",
    "PluginUIUnavailableError",
]
