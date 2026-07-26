"""PromptToolkit/Rich adapter for the host-neutral PluginUI protocol."""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from rich.console import Console

from evopi.plugins import PluginUIUnavailableError


class ReplDisplayHost(Protocol):
    def pause(self) -> None: ...

    def resume(self) -> None: ...

    def set_plugin_status(self, key: str, text: str | None) -> None: ...


class ReplPluginUI:
    def __init__(
        self,
        *,
        display: ReplDisplayHost,
        prompt: Callable[[str], Awaitable[str]],
        console: Console | None = None,
    ) -> None:
        self._display = display
        self._prompt = prompt
        self._console = console or Console(file=sys.stderr)
        self._status_keys: set[str] = set()

    async def notify(self, message: str, *, level: str = "info") -> None:
        styles = {
            "info": "cyan",
            "success": "green",
            "warning": "yellow",
            "error": "red",
        }
        self._console.print(message, style=styles.get(level, "cyan"))

    async def confirm(self, title: str, message: str) -> bool:
        response = await self._modal_prompt(
            f"{title}: {message} [y/N] "
        )
        return response.strip().lower() in {"y", "yes"}

    async def select(self, title: str, options: Sequence[str]) -> str:
        if not options:
            raise PluginUIUnavailableError("Plugin selection has no options")
        self._display.pause()
        try:
            self._console.print(f"[bold]{title}[/]")
            for index, option in enumerate(options, start=1):
                self._console.print(f"  {index}. {option}")
            response = await self._prompt("Select: ")
        finally:
            self._display.resume()
        try:
            selected = int(response.strip())
        except ValueError as exc:
            raise PluginUIUnavailableError(
                "Plugin selection must be a numeric option"
            ) from exc
        if not 1 <= selected <= len(options):
            raise PluginUIUnavailableError("Plugin selection is out of range")
        return options[selected - 1]

    async def input(self, title: str, prompt: str = "") -> str:
        label = f"{title}: {prompt}".strip() + " "
        return await self._modal_prompt(label)

    async def set_status(self, key: str, text: str | None) -> None:
        if text is None:
            self._status_keys.discard(key)
        else:
            self._status_keys.add(key)
        self._display.set_plugin_status(key, text)

    def clear_plugin_statuses(self, plugin_name: str) -> None:
        prefix = f"{plugin_name}:"
        keys = [key for key in self._status_keys if key.startswith(prefix)]
        for key in keys:
            self._display.set_plugin_status(key, None)
            self._status_keys.discard(key)

    async def _modal_prompt(self, label: str) -> str:
        self._display.pause()
        try:
            return await self._prompt(label)
        finally:
            self._display.resume()


__all__ = ["ReplPluginUI"]
