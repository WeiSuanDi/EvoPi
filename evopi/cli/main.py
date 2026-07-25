"""Command-line entry point for the EvoPi coding agent."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from prompt_toolkit import PromptSession
from rich.console import Console
from rich.panel import Panel

from evopi.ai.models import model_from_environment
from evopi.coding.harness import CodingHarness
from evopi.cli.commands import handle_slash_command, set_last_prompt
from evopi.cli.confirmation import async_terminal_confirmation_handler
from evopi.cli.display import ReplDisplay
from evopi.cli.policy_review import policy_review_main
from evopi.cli.resume import pick_session
from evopi.cli.session import print_session_opened, session_list_main
from evopi.core.events import CoreEvent
from evopi.core.model_errors import ModelRetryConfig
from evopi.session import SessionManager


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evopi", description="Run the EvoPi coding agent")
    parser.add_argument("prompt", nargs="?", help="Task for the agent")
    parser.add_argument("--provider", choices=["anthropic", "openai-compatible"])
    parser.add_argument("--model", help="Override the model name from .env")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--trace", type=Path, default=Path(".evopi/trace.jsonl"))
    parser.add_argument("--no-retry", action="store_true")
    parser.add_argument("--max-retries", type=_non_negative_int, default=3)
    parser.add_argument(
        "--model-timeout", type=_positive_float, default=120.0,
        help="HTTP stream idle timeout in seconds",
    )
    parser.add_argument(
        "--deadline", type=_positive_float, metavar="SECONDS",
        help="Wall-clock deadline for the entire run",
    )
    parser.add_argument(
        "--tool-timeout", type=_positive_float, metavar="SECONDS",
        help="Default timeout for individual tool executions",
    )
    parser.add_argument(
        "--context-window", type=int, metavar="N",
        help="Model context window size for compaction decisions",
    )
    parser.add_argument(
        "--approvals-path", type=Path, metavar="PATH",
        help="Path to the approvals JSON file",
    )
    parser.add_argument(
        "--approval-mode", choices=["strict", "warn", "off"], default="warn",
        help="Activation Gate mode (default: warn)",
    )
    parser.add_argument(
        "--compaction", choices=["on", "off"], default="on",
        help="Enable or disable automatic context compaction (default: on)",
    )
    parser.add_argument(
        "--system-prompt", metavar="TEXT",
        help="Override the default system prompt",
    )
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument(
        "--new-session",
        action="store_true",
        help="Start a new persisted Session",
    )
    session_group.add_argument(
        "--session",
        metavar="ID|PATH",
        help="Open one persisted Session by UUID or path",
    )
    session_group.add_argument(
        "--no-session",
        action="store_true",
        help="Use an ephemeral in-memory Session",
    )
    session_group.add_argument(
        "--resume",
        action="store_true",
        help="Interactively pick a session to resume",
    )
    parser.add_argument(
        "--session-root",
        type=Path,
        help="Override the persisted Session root directory",
    )
    parser.add_argument(
        "--plugin", type=Path, action="append", metavar="PATH",
        help="Load a plugin from PATH (repeatable)",
    )
    return parser


def _build_harness(args: argparse.Namespace) -> CodingHarness:
    """Build a CodingHarness from CLI args."""
    from evopi.session.compact import CompactionSettings

    session_manager = _session_manager_from_args(args)
    model = model_from_environment(
        getattr(args, "provider", None),
        timeout=getattr(args, "model_timeout", 120.0),
        model=getattr(args, "model", None),
        context_window=getattr(args, "context_window", 0),
    )
    return CodingHarness(
        model=model,
        workspace=args.workspace,
        trace_path=args.trace,
        system_prompt=getattr(args, "system_prompt", "") or "",
        retry_config=ModelRetryConfig(
            enabled=not getattr(args, "no_retry", False),
            max_retries=getattr(args, "max_retries", 3),
        ),
        confirmation_handler=async_terminal_confirmation_handler,
        session_manager=session_manager,
        deadline=getattr(args, "deadline", None),
        tool_timeout=getattr(args, "tool_timeout", None),
        approvals_path=getattr(args, "approvals_path", None),
        approval_mode=getattr(args, "approval_mode", "warn"),
        compaction_settings=CompactionSettings(
            enabled=(getattr(args, "compaction", "on") == "on"),
        ),
        plugin_paths=getattr(args, "plugin", None),
    )


async def _run_one_shot(args: argparse.Namespace) -> int:
    """Single prompt → response → exit."""
    prompt_text = args.prompt
    harness: CodingHarness | None = None
    try:
        harness = _build_harness(args)

        def display(event: CoreEvent) -> None:
            if event.type == "session_start":
                print_session_opened(harness.session)  # type: ignore[arg-type]
            elif event.type == "message_update" and event.data.get("kind") == "text":
                print(event.data.get("delta", ""), end="", flush=True)
            elif event.type == "model_retry_start":
                info = event.data.get("error_info")
                kind = getattr(info, "kind", "unknown")
                print(
                    f"\nEvoPi retrying model after {kind} error "
                    f"(attempt {event.data.get('next_attempt')}, "
                    f"delay {event.data.get('delay')}s)...",
                    file=sys.stderr,
                    flush=True,
                )

        harness.subscribe(display)
        answer = await harness.prompt(prompt_text)
        if not answer.content.endswith("\n"):
            print()
        return 0
    finally:
        if harness is not None and not harness.is_running:
            harness.close()


async def _run_repl(args: argparse.Namespace) -> int:
    """Multi-turn REPL: prompt → response → prompt → ..."""
    console = Console(file=sys.stderr)
    harness: CodingHarness | None = None
    try:
        harness = _build_harness(args)

        # Welcome
        console.print(Panel(
            f"[bold]EvoPi[/] — Type your message, [bold]/help[/] for commands.\n"
            f"Model: [cyan]{harness.model.name}[/] | "
            f"Session: [dim]{harness.session.session_id[:12]}...[/] | "
            f"Workspace: [dim]{harness.session.workspace[:40]}[/]",
            border_style="blue",
        ))

        display = ReplDisplay()
        display.set_status(
            f"Model: {harness.model.name} | "
            f"Session: {harness.session.session_id[:12]}..."
        )
        harness.subscribe(display.handle_event)

        session = PromptSession[str]()
        while True:
            try:
                user_input = (await session.prompt_async("> ")).strip()
            except KeyboardInterrupt:
                console.print("\n[yellow]Aborted.[/]")
                return 130
            except EOFError:
                console.print("\n[dim]Goodbye.[/]")
                return 0

            if not user_input:
                continue

            if user_input.startswith("/"):
                if user_input == "/retry":
                    from evopi.cli.commands import _last_prompt
                    if _last_prompt:
                        user_input = _last_prompt
                        console.print(f"[dim]Retrying: {user_input[:80]}...[/]")
                    else:
                        console.print("[yellow]No previous prompt to retry.[/]")
                        continue
                else:
                    handle_slash_command(harness, user_input)
                    continue

            set_last_prompt(user_input)

            try:
                display.start_run()
                await harness.prompt(user_input)
                display.end_run()
            except (ValueError, RuntimeError) as exc:
                display.end_run()
                console.print(f"[red]Error: {exc}[/]")
                continue
            except KeyboardInterrupt:
                display.end_run()
                console.print("[yellow][aborted][/]")
                continue
    finally:
        if harness is not None and not harness.is_running:
            harness.close()


def _session_manager_from_args(args: argparse.Namespace) -> SessionManager:
    workspace = getattr(args, "workspace", Path.cwd())
    if not hasattr(args, "session_root"):
        return SessionManager.in_memory(workspace)
    root = args.session_root
    if getattr(args, "no_session", False):
        return SessionManager.in_memory(workspace)
    if getattr(args, "resume", False):
        return pick_session(workspace, root=root)
    if getattr(args, "new_session", False):
        return SessionManager.create(workspace, root=root)
    if getattr(args, "session", None) is not None:
        return SessionManager.open(args.session, workspace=workspace, root=root)
    return SessionManager.continue_recent(workspace, root=root)


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)

    if raw_args[:2] == ["policy", "review"]:
        return policy_review_main(raw_args[2:])
    if raw_args[:2] == ["session", "list"]:
        return session_list_main(raw_args[2:])
    if raw_args[:2] == ["plugin", "list"]:
        return _plugin_list_main(raw_args[2:])
    if raw_args[:2] == ["plugin", "install"]:
        return _plugin_install_main(raw_args[2:])
    if raw_args[:2] == ["plugin", "remove"]:
        return _plugin_remove_main(raw_args[2:])

    args = (
        build_parser().parse_args()
        if argv is None
        else build_parser().parse_args(raw_args)
    )

    try:
        if getattr(args, "prompt", None):
            return asyncio.run(_run_one_shot(args))
        return asyncio.run(_run_repl(args))
    except (ValueError, RuntimeError) as exc:
        print(f"EvoPi error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nEvoPi aborted.")
        return 130


def _plugin_list_main(argv: list[str]) -> int:
    """``evopi plugin list`` — print loaded plugins."""
    from evopi.plugins.loader import discover_plugins
    workspace = Path.cwd()
    plugins = discover_plugins(workspace)
    if not plugins:
        print("No plugins found.")
        print("  Global: ~/.evopi/plugins/")
        print(f"  Local:  {workspace / '.evopi' / 'plugins'}")
        return 0
    print(f"{len(plugins)} plugin(s):")
    for p in plugins:
        print(f"  {p}")
    return 0


def _plugin_install_main(argv: list[str]) -> int:
    """``evopi plugin install <path>`` — copy a plugin file into global plugins dir."""
    import shutil
    if len(argv) < 1:
        print("Usage: evopi plugin install <path>", file=sys.stderr)
        return 1
    src = Path(argv[0]).expanduser().resolve()
    if not src.exists():
        print(f"Error: {src} does not exist", file=sys.stderr)
        return 1
    dst_dir = Path.home() / ".evopi" / "plugins"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.copy2(src, dst)
    print(f"Installed: {dst}")
    return 0


def _plugin_remove_main(argv: list[str]) -> int:
    """``evopi plugin remove <name>`` — remove a plugin file from global plugins dir."""
    if len(argv) < 1:
        print("Usage: evopi plugin remove <name>", file=sys.stderr)
        return 1
    dst_dir = Path.home() / ".evopi" / "plugins"
    target = dst_dir / argv[0]
    if not target.exists():
        target = dst_dir / f"{argv[0]}.py"
    if not target.exists():
        print(f"Error: plugin '{argv[0]}' not found", file=sys.stderr)
        return 1
    target.unlink()
    print(f"Removed: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
