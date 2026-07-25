"""Rich-based REPL display with streaming Markdown and compact tool status."""

from __future__ import annotations

import sys
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text


class ReplDisplay:
    """Rich display for one REPL run with minimal tool-call feedback.

    Tool calls are shown as compact one-liners::

        ✓ write_file    ✗ shell_command (exit 1)    ⏱ timed_out

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
        self._tool_line: str = ""
        self._live: Live | None = None
        self._turn: int = 0
        self._status_text: str = ""

    def set_status(self, text: str) -> None:
        self._status_text = text

    def start_run(self) -> None:
        self._text = ""
        self._tool_line = ""
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
            "confirmation_request": self._on_confirm_start,
            "confirmation_response": self._on_confirm_end,
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
        self._tool_line = f"[dim]… {name}[/]"
        self._refresh()

    def _on_tool_end(self, data: dict[str, Any]) -> None:
        name = data.get("tool_name", "?")
        meta: dict = getattr(data.get("result"), "metadata", {}) if data.get("result") else {}
        is_error = getattr(data.get("result"), "is_error", False) if data.get("result") else False
        icon = "[red]✗[/]" if is_error else "[green]✓[/]"
        extra = ""
        if meta.get("timed_out"):
            extra = " [red]TIMEOUT[/]"
        elif meta.get("blocked"):
            extra = " [red]BLOCKED[/]"
        elif meta.get("exit_code") is not None and meta["exit_code"] != 0:
            extra = f" [yellow](exit {meta['exit_code']})[/]"
        self._tool_line = f"{icon} {name}{extra}"
        self._refresh()

    def _on_confirm_start(self, data: dict[str, Any]) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _on_confirm_end(self, data: dict[str, Any]) -> None:
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=10,
            transient=False,
        )
        self._live.start()

    def _on_retry(self, data: dict[str, Any]) -> None:
        info = data.get("error_info")
        kind = getattr(info, "kind", "?") if info else "?"
        self._tool_line = f"[yellow]↻ retrying after {kind}[/]"
        self._refresh()

    def _render(self) -> Group:
        items: list[Any] = []
        # Status line
        parts = [self._status_text] if self._status_text else []
        if self._turn:
            parts.append(f"Turn {self._turn}")
        if self._tool_line:
            parts.append(self._tool_line)
        if parts:
            items.append(Text(" | ".join(parts), style="bold"))
        # Chat content
        if self._text:
            items.append(Panel(Markdown(self._text), border_style="blue", title="EvoPi"))
        return Group(*items)

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render(), refresh=True)
