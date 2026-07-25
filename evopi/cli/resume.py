"""Session picker for --resume."""

from __future__ import annotations

import sys
from pathlib import Path

from prompt_toolkit import prompt
from prompt_toolkit.completion import WordCompleter
from rich.console import Console
from rich.table import Table

from evopi.session import SessionManager, SessionSummary


def pick_session(
    workspace: str | Path,
    root: str | Path | None = None,
) -> SessionManager:
    """Interactive session picker.

    Returns the selected SessionManager, or creates a new session if the
    user chooses that option.
    """
    console = Console(file=sys.stderr)
    summaries = SessionManager.list(workspace=workspace, root=root)

    if not summaries:
        console.print("[yellow]No existing sessions. Creating new...[/]")
        return SessionManager.create(workspace, root=root)

    # Render sessions table
    table = Table(title="Select a Session", border_style="blue")
    table.add_column("#", style="bold cyan", width=4)
    table.add_column("ID", style="dim")
    table.add_column("Turns")
    table.add_column("Status")
    table.add_column("Last run reason")
    for i, s in enumerate(summaries[:15], 1):
        stats = _session_stats(s)
        table.add_row(
            str(i),
            s.session_id[:16],
            str(s.message_count // 2),  # rough turn count
            stats["status"],
            stats["reason"],
        )
    console.print(table)

    # Prompt
    options = [str(i) for i in range(1, len(summaries[:15]) + 1)] + ["n", "q"]
    completer = WordCompleter(options)
    choice = prompt(
        "Enter # to resume, [bold]n[/] for new, [bold]q[/] to quit: ",
        completer=completer,
    ).strip().lower()

    if choice == "q":
        console.print("Goodbye.")
        sys.exit(0)
    if choice == "n":
        return SessionManager.create(workspace, root=root)
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(summaries[:15]):
            return SessionManager.open(
                summaries[idx].path, workspace=workspace, root=root
            )
    except ValueError:
        pass
    # Default: create new
    return SessionManager.create(workspace, root=root)


def _session_stats(s: SessionSummary) -> dict[str, str]:
    reason = s.last_run_reason or "new"
    if s.error:
        return {"status": "[red]broken[/]", "reason": reason}
    if reason == "interrupted":
        return {"status": "[yellow]interrupted[/]", "reason": reason}
    return {"status": "[green]ok[/]", "reason": reason}
