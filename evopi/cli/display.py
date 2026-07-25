"""Rich REPL display with one owned live-render region."""

from __future__ import annotations

import sys
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text


class ReplDisplay:
    """Render streaming Markdown and tool status inside one Rich Live region."""

    def __init__(self) -> None:
        self.console = Console(file=sys.stderr, highlight=False)
        self._text: str = ""
        self._tool_status: list[str] = []
        self._live: Live | None = None
        self._turn: int = 0
        self._status_text: str = ""

    def set_status(self, text: str) -> None:
        self._status_text = text

    def start_run(self) -> None:
        self._text = ""
        self._tool_status = []
        self._turn = 0
        self._start_live()

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
        if data.get("kind") == "text" and data.get("role", "assistant") == "assistant":
            self._text += data.get("delta", "")
            self._refresh()

    def _on_turn(self, data: dict[str, Any]) -> None:
        self._turn = data.get("turn", self._turn + 1)
        self._refresh()

    def _on_tool_start(self, data: dict[str, Any]) -> None:
        name = data.get("tool_name", "?")
        self._tool_status.append(f"…{name}")
        self._refresh()

    def _on_tool_end(self, data: dict[str, Any]) -> None:
        name = data.get("tool_name", "?")
        meta: dict = getattr(data.get("result"), "metadata", {}) if data.get("result") else {}
        is_error = getattr(data.get("result"), "is_error", False) if data.get("result") else False
        icon = "✗" if is_error else "✓"
        extra = ""
        if meta.get("timed_out"):
            extra = " TIMEOUT"
        elif meta.get("blocked"):
            extra = " BLOCKED"
        elif meta.get("exit_code") is not None and meta["exit_code"] != 0:
            extra = f" (exit {meta['exit_code']})"
        for i, s in enumerate(self._tool_status):
            if s == f"…{name}":
                self._tool_status[i] = f"{icon} {name}{extra}"
                break
        self._refresh()

    def _on_retry(self, data: dict[str, Any]) -> None:
        info = data.get("error_info")
        kind = getattr(info, "kind", "?") if info else "?"
        self._tool_status.append(f"↻{kind}")
        self._refresh()

    def _on_confirm_start(self, data: dict[str, Any]) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _on_confirm_end(self, data: dict[str, Any]) -> None:
        self._start_live()

    def _start_live(self) -> None:
        if self._live is not None:
            return
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=10,
            transient=False,
        )
        self._live.start()

    def _render(self) -> Group:
        items: list[Any] = []
        status_parts = [self._status_text] if self._status_text else []
        if self._turn:
            status_parts.append(f"Turn {self._turn}")
        if self._tool_status:
            status_parts.append("  ".join(self._tool_status))
        if status_parts:
            items.append(Text(" | ".join(status_parts), style="bold"))
        if self._text:
            items.append(
                Panel(
                    Markdown(self._text),
                    border_style="blue",
                    padding=(0, 1),
                )
            )
        return Group(*items)

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render(), refresh=True)
