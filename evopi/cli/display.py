"""Rich-based REPL display helpers.

Wraps Rich's Console, Panel, Markdown, and Live to render streaming model
output, tool call results, and session status.
"""

from __future__ import annotations

import sys
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from evopi.core.events import CoreEvent

_BASE_STYLE = {
    "tool": "bold cyan",
    "tool_error": "bold red",
    "tool_success": "bold green",
    "retry": "yellow",
    "confirm": "bold yellow",
    "session": "dim",
    "status": "bold",
}


class ReplDisplay:
    """Manages Rich output for one REPL run.

    Usage::

        display = ReplDisplay()
        # subscribe to harness events → display.handle(event)
        await harness.prompt(text)       # events fire during this call
        display.end_run()
    """

    def __init__(self) -> None:
        self.console = Console(file=sys.stderr, highlight=False)
        self._assistant_text: str = ""
        self._tool_panels: list[Panel] = []
        self._live: Live | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_run(self) -> None:
        """Begin a new run — open the Live rendering context."""
        self._assistant_text = ""
        self._tool_panels = []
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=10,
            transient=False,
        )
        self._live.start()

    def end_run(self) -> None:
        """Close the Live context and print the final assistant message."""
        if self._live is not None:
            self._live.stop()
            self._live = None
        # Print final assistant text to stdout
        if self._assistant_text.strip():
            out = Console(file=sys.stdout, highlight=True)
            out.print(Markdown(self._assistant_text.strip()))
        # Print tool panels to stderr
        for panel in self._tool_panels:
            self.console.print(panel)
        self._assistant_text = ""
        self._tool_panels = []

    def handle_event(self, event: CoreEvent) -> None:
        """Route a harness CoreEvent to the appropriate renderer."""
        handlers = {
            "message_update": self._on_message_update,
            "tool_execution_start": self._on_tool_start,
            "tool_execution_end": self._on_tool_end,
            "model_retry_start": self._on_retry,
            "confirmation_request": self._on_confirm,
        }
        handler = handlers.get(event.type)
        if handler is not None:
            handler(event.data)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_message_update(self, data: dict[str, Any]) -> None:
        if data.get("kind") == "text":
            self._assistant_text += data.get("delta", "")
            self._refresh()

    def _on_tool_start(self, data: dict[str, Any]) -> None:
        name = data.get("tool_name", "?")
        p = Panel(
            Text(f"Running {name}...", style=_BASE_STYLE["tool"]),
            border_style="cyan",
            title="Tool",
            title_align="left",
        )
        self._tool_panels.append(p)
        self._refresh()

    def _on_tool_end(self, data: dict[str, Any]) -> None:
        result = data.get("result")
        is_error = getattr(result, "is_error", False) if result is not None else False
        name = data.get("tool_name", "?")
        meta: dict[str, Any] = getattr(result, "metadata", {}) if result is not None else {}
        content = getattr(result, "content", "") if result is not None else ""

        # Build detail lines
        lines: list[Text] = []
        exit_code = meta.get("exit_code")
        if exit_code is not None:
            lines.append(Text(f"exit: {exit_code}", style="dim"))
        if meta.get("timed_out"):
            lines.append(Text("TIMEOUT", style="bold red"))
        if meta.get("blocked"):
            lines.append(Text("BLOCKED", style="bold red"))
        if content and not is_error:
            lines.append(Text(content[:300], style="dim"))

        _BASE_STYLE["tool_error"] if is_error else _BASE_STYLE["tool_success"]
        icon = "✗" if is_error else "✓"
        title = f"{icon} {name}"

        if self._tool_panels:
            self._tool_panels[-1] = Panel(
                Group(*lines) if lines else Text(""),
                border_style="red" if is_error else "green",
                title=title,
                title_align="left",
            )
        self._refresh()

    def _on_retry(self, data: dict[str, Any]) -> None:
        info = data.get("error_info")
        kind = getattr(info, "kind", "unknown") if info is not None else "unknown"
        msg = (
            f"[{_BASE_STYLE['retry']}]Retrying after {kind} error "
            f"(attempt {data.get('next_attempt', '?')})...[/]"
        )
        self.console.print(Text.from_markup(msg))

    def _on_confirm(self, data: dict[str, Any]) -> None:
        request = data.get("request")
        tool = ""
        if request is not None and hasattr(request, "tool_call"):
            tc = request.tool_call
            if tc is not None and hasattr(tc, "name"):
                tool = tc.name
        self.console.print(
            Panel(
                Text(f"Awaiting confirmation for: {tool}", style=_BASE_STYLE["confirm"]),
                border_style="yellow",
                title="Confirm",
            )
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _render(self) -> Group:
        items: list[Any] = []
        if self._assistant_text:
            items.append(
                Panel(
                    Markdown(self._assistant_text),
                    border_style="blue",
                    title="EvoPi",
                    title_align="left",
                )
            )
        items.extend(self._tool_panels)
        return Group(*items)

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render(), refresh=True)
