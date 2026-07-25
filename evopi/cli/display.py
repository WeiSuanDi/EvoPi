"""Rich-based REPL display with streaming Markdown and tool panels."""

from __future__ import annotations

import sys
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text


class ReplDisplay:
    """Rich display for one REPL run.

    Usage::

        display = ReplDisplay()
        harness.subscribe(display.handle_event)
        display.start_run()
        await harness.prompt(text)
        display.end_run()
    """

    def __init__(self) -> None:
        self.console = Console(file=sys.stderr, highlight=False)
        self._text: str = ""
        self._tool_panels: list[Panel] = []
        self._live: Live | None = None
        self._turn: int = 0
        self._status_text: str = ""

    def set_status(self, text: str) -> None:
        self._status_text = text

    def start_run(self) -> None:
        self._text = ""
        self._tool_panels = []
        self._turn = 0
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=10,
            transient=False,
        )
        self._live.start()

    def end_run(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def handle_event(self, event: Any) -> None:
        h = {
            "message_update": self._on_text,
            "tool_execution_start": self._on_tool_start,
            "tool_execution_end": self._on_tool_end,
            "turn_start": self._on_turn,
            "model_retry_start": self._on_retry,
        }
        handler = h.get(event.type)
        if handler is not None:
            handler(event.data)

    def _on_text(self, data: dict[str, Any]) -> None:
        if data.get("kind") == "text":
            self._text += data.get("delta", "")
            self._refresh()

    def _on_turn(self, data: dict[str, Any]) -> None:
        self._turn = data.get("turn", self._turn + 1)
        self._refresh()

    def _on_tool_start(self, data: dict[str, Any]) -> None:
        name = data.get("tool_name", "?")
        self._tool_panels.append(
            Panel(Text(f"Running {name}...", style="bold cyan"), border_style="cyan", title="Tool")
        )
        self._refresh()

    def _on_tool_end(self, data: dict[str, Any]) -> None:
        result = data.get("result")
        name = data.get("tool_name", "?")
        meta: dict = getattr(result, "metadata", {}) if result else {}
        content = getattr(result, "content", "") if result else ""
        is_error = getattr(result, "is_error", False) if result else False

        lines: list[Text] = []
        if meta.get("exit_code") is not None:
            lines.append(Text(f"exit: {meta['exit_code']}", style="dim"))
        if meta.get("timed_out"):
            lines.append(Text("TIMEOUT", style="bold red"))
        if meta.get("blocked"):
            lines.append(Text("BLOCKED", style="bold red"))
        if content and not is_error:
            lines.append(Text(content[:500], style="dim"))

        icon = "✗" if is_error else "✓"
        if self._tool_panels:
            self._tool_panels[-1] = Panel(
                Group(*lines) if lines else Text(""),
                border_style="red" if is_error else "green",
                title=f"{icon} {name}",
            )
        self._refresh()

    def _on_retry(self, data: dict[str, Any]) -> None:
        info = data.get("error_info")
        kind = getattr(info, "kind", "?") if info else "?"
        self._tool_panels.append(
            Panel(
                Text(f"Retrying after {kind} (attempt {data.get('next_attempt', '?')})", style="yellow"),
                border_style="yellow",
                title="Retry",
            )
        )
        self._refresh()

    def _render(self) -> Group:
        items: list[Any] = []
        # Status line at top
        parts = [self._status_text] if self._status_text else []
        if self._turn:
            parts.append(f"Turn {self._turn}")
        if parts:
            items.append(Text(" | ".join(parts), style="bold"))
        # Chat content
        if self._text:
            items.append(Panel(Markdown(self._text), border_style="blue", title="EvoPi"))
        # Tools
        items.extend(self._tool_panels)
        return Group(*items)

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render(), refresh=True)
