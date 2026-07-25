"""REPL slash commands with Rich-formatted output."""

from __future__ import annotations

import sys
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from evopi.coding.harness import CodingHarness

_console = Console(file=sys.stderr)


def handle_slash_command(harness: CodingHarness, text: str) -> None:
    """Dispatch a REPL slash-command to its handler."""
    parts = text.split()
    cmd = parts[0].lower()

    handlers: dict[str, Any] = {
        "/help": _cmd_help,
        "/clear": _cmd_clear,
        "/status": _cmd_status,
        "/retry": _cmd_retry,
        "/leaves": _cmd_leaves,
        "/switch": _cmd_switch,
        "/branch": _cmd_branch,
        "/fork": _cmd_fork,
        "/compact": _cmd_compact,
    }
    handler = handlers.get(cmd)
    if handler is not None:
        handler(harness, parts, text)
    else:
        _console.print(Panel(
            f"Unknown command: [bold]{cmd}[/]\n\n"
            "Type [bold]/help[/] to see available commands.",
            border_style="red",
            title="Error",
        ))


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

def _cmd_help(harness: CodingHarness, parts: list[str], raw: str) -> None:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan")
    grid.add_column()
    commands = [
        ("/help", "Show this help"),
        ("/status", "Session info: model, turns, tokens"),
        ("/clear", "Clear the screen"),
        ("/retry", "Re-run the last prompt"),
        ("/branch [name]", "Create a branch from current leaf"),
        ("/switch <id>", "Switch active leaf"),
        ("/fork", "Fork session into a new file"),
        ("/compact <summary>", "Compress conversation history"),
        ("/leaves", "List all branch leaves"),
    ]
    for key, desc in commands:
        grid.add_row(key, desc)

    _console.print(Panel(grid, border_style="blue", title="EvoPi Commands"))
    _console.print(Panel(
        "[bold]Ctrl+C[/] abort  [bold]Ctrl+D[/] quit  [bold]Ctrl+L[/] clear",
        border_style="dim",
    ))


# ---------------------------------------------------------------------------
# /clear
# ---------------------------------------------------------------------------

def _cmd_clear(harness: CodingHarness, parts: list[str], raw: str) -> None:
    import os
    os.system("cls" if os.name == "nt" else "clear")


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

def _cmd_status(harness: CodingHarness, parts: list[str], raw: str) -> None:
    session = harness.session
    path = session.get_active_path()
    msg_count = len(session.messages)
    leaves = session.leaves()

    table = Table(border_style="blue", title="Session Status")
    table.add_column("Property", style="bold")
    table.add_column("Value")
    table.add_row("Session ID", session.session_id[:16] + "...")
    table.add_row("Workspace", session.workspace)
    table.add_row("Persistent", str(session.is_persistent))
    table.add_row("Messages", str(msg_count))
    table.add_row("Entries", str(len(session.entries)))
    table.add_row("Leaves", str(len(leaves)))
    table.add_row("Active leaf", (session.leaf_id or "?")[:16] + "...")
    for entry in reversed(path):
        if hasattr(entry, "type") and entry.type == "run_end":
            table.add_row("Last run", getattr(entry, "reason", "?"))
            break
    table.add_row("Model", harness.model.name)
    table.add_row("Approval mode", harness.policies.approval_store.mode)
    table.add_row("Compaction", "on" if harness.compaction_settings.enabled else "off")

    _console.print(table)


# ---------------------------------------------------------------------------
# /retry
# ---------------------------------------------------------------------------

_last_prompt: str | None = None

def set_last_prompt(text: str) -> None:
    global _last_prompt
    _last_prompt = text

def _cmd_retry(harness: CodingHarness, parts: list[str], raw: str) -> None:
    if _last_prompt is None:
        _console.print("[yellow]No previous prompt to retry.[/]")
        return
    # This is handled specially in the REPL loop — it calls harness.prompt(_last_prompt)
    # We signal via a sentinel that the REPL loop checks.
    _console.print(f"[dim]Retrying: {_last_prompt[:80]}...[/]")


# ---------------------------------------------------------------------------
# Session tree commands
# ---------------------------------------------------------------------------

def _cmd_leaves(harness: CodingHarness, parts: list[str], raw: str) -> None:
    session = harness.session
    leaves = session.leaves()
    active = session.leaf_id
    out = [f"[bold]{len(leaves)} leaf(ves):[/]"]
    for lid in leaves:
        marker = " [bold green]*[/]" if lid == active else ""
        out.append(f"  {lid[:16]}...{marker}")
    _console.print(Panel("\n".join(out), border_style="blue"))


def _cmd_switch(harness: CodingHarness, parts: list[str], raw: str) -> None:
    if len(parts) < 2:
        _console.print("[yellow]Usage: /switch <leaf_id>[/]")
        return
    try:
        harness.session.switch_leaf(parts[1])
        _console.print(f"[green]Switched to {parts[1][:16]}...[/]")
    except Exception as exc:
        _console.print(f"[red]Error: {exc}[/]")


def _cmd_branch(harness: CodingHarness, parts: list[str], raw: str) -> None:
    session = harness.session
    if session.leaf_id is None:
        _console.print("[yellow]No active leaf to branch from.[/]")
        return
    name = parts[1] if len(parts) > 1 else ""
    try:
        entry = session.branch(from_entry_id=session.leaf_id, branch_name=name)
        pid = entry.parent_id or "?"
        _console.print(
            f"[green]Branched from {pid[:16]}... → {entry.entry_id[:16]}...[/]"
        )
    except Exception as exc:
        _console.print(f"[red]Error: {exc}[/]")


def _cmd_fork(harness: CodingHarness, parts: list[str], raw: str) -> None:
    try:
        new_session = harness.session.fork()
        _console.print(Panel(
            f"Forked: [bold]{new_session.session_id[:16]}...[/]\n"
            f"Restart with: [bold cyan]evopi --session {new_session.session_id[:16]}[/]",
            border_style="green",
            title="Fork",
        ))
    except Exception as exc:
        _console.print(f"[red]Error: {exc}[/]")


def _cmd_compact(harness: CodingHarness, parts: list[str], raw: str) -> None:
    if len(parts) < 2:
        _console.print("[yellow]Usage: /compact <summary text>[/]")
        return
    session = harness.session
    if session.leaf_id is None:
        _console.print("[yellow]No active leaf to compact.[/]")
        return
    summary = raw[len("/compact "):].strip()
    # Find last checkpoint as anchor
    path = session.get_active_path()
    anchor_id = path[0].entry_id if path else session.leaf_id
    for entry in reversed(path):
        if getattr(entry, "type", None) == "checkpoint":
            anchor_id = entry.entry_id
            break
    try:
        session.compact(up_to_entry_id=anchor_id, summary=summary)
        if session.leaf_id:
            _console.print(f"[green]Compacted. Leaf: {session.leaf_id[:16]}...[/]")
    except Exception as exc:
        _console.print(f"[red]Error: {exc}[/]")
