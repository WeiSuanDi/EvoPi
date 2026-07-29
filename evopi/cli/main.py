"""Command-line entry point for the EvoPi coding agent."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.input import Input
from prompt_toolkit.output import Output
from rich.console import Console
from rich.panel import Panel

from evopi.ai.models import model_from_environment
from evopi.coding.harness import CodingHarness
from evopi.cli.commands import handle_slash_command, set_last_prompt
from evopi.cli.confirmation import async_terminal_confirmation_handler
from evopi.cli.display import ReplDisplay
from evopi.cli.policy_review import policy_review_main
from evopi.cli.policy import policy_init_main, policy_lifecycle_main
from evopi.cli.plugin import plugin_main
from evopi.cli.resume import pick_session
from evopi.cli.session import (
    print_session_opened,
    session_gc_main,
    session_list_main,
)
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


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evopi", description="Run the EvoPi coding agent")
    parser.add_argument("prompt", nargs="?", help="Task for the agent")
    parser.add_argument(
        "--provider",
        choices=["anthropic", "openai-compatible", "openai-responses"],
    )
    parser.add_argument("--model", help="Override the model name from .env")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--trace", type=Path, default=Path(".evopi/trace.jsonl"))
    parser.add_argument("--no-retry", action="store_true")
    parser.add_argument("--max-retries", type=_non_negative_int, default=3)
    parser.add_argument(
        "--max-output-tokens",
        type=_positive_int,
        default=4096,
        metavar="N",
        help="Maximum output tokens for each model attempt (default: 4096)",
    )
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
    memory_group = parser.add_mutually_exclusive_group()
    memory_group.add_argument(
        "--memory", type=Path, metavar="PATH",
        help="Enable persistent memory (JSON file path, e.g. .evopi/memory.json)",
    )
    memory_group.add_argument(
        "--no-memory",
        action="store_true",
        help="Disable workspace Memory for this invocation",
    )
    parser.add_argument(
        "--skills-root", type=Path, metavar="PATH",
        help="Enable skill loading from PATH (e.g. ~/.evopi/skills)",
    )
    parser.add_argument(
        "--enable-subagent", action="store_true",
        help="Enable the spawn_subagent tool for delegated tasks",
    )
    return parser


def _build_harness(args: argparse.Namespace) -> CodingHarness:
    """Build a CodingHarness from CLI args, auto-detecting available modules."""
    from evopi.session.compact import CompactionSettings

    session_manager = _session_manager_from_args(args)
    model_options: dict[str, Any] = {
        "timeout": getattr(args, "model_timeout", 120.0),
        "model": getattr(args, "model", None),
        "context_window": getattr(args, "context_window", 0),
    }
    if hasattr(args, "max_output_tokens"):
        model_options["max_tokens"] = args.max_output_tokens
    model = model_from_environment(
        getattr(args, "provider", None),
        **model_options,
    )

    # Auto-detect Memory — always on, stored in workspace
    memory_path = getattr(args, "memory", None)
    if getattr(args, "no_memory", False):
        memory_path = None
    elif memory_path is None:
        default_memory = args.workspace / ".evopi" / "memory.json"
        memory_path = default_memory

    # Auto-detect Skills — on if skills directory exists
    skills_root, resource_warnings = _skills_root_from_args(args)

    # SubAgent — explicit opt-in only
    enable_subagent = getattr(args, "enable_subagent", False)

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
        plugin_paths=_plugin_paths_from_args(args),
        memory_path=memory_path,
        skills_root=skills_root,
        enable_subagent=enable_subagent,
        resource_warnings=resource_warnings,
    )


def _skills_root_from_args(
    args: argparse.Namespace,
) -> tuple[Path | None, tuple[str, ...]]:
    explicit = getattr(args, "skills_root", None)
    if explicit is not None:
        return explicit, ()
    from evopi.evolution import WorkspaceTrustStore
    from evopi.plugins import resolve_evopi_home

    workspace = Path(args.workspace).expanduser().resolve()
    project = workspace / ".evopi" / "skills"
    home = resolve_evopi_home()
    trust_path = home / "workspace-trust.json"
    trusted = (
        trust_path.exists()
        and WorkspaceTrustStore(trust_path).is_trusted(workspace)
    )
    if project.exists() and trusted:
        return project, ()
    warnings = (
        ("Project Skills were skipped because the workspace is not trusted",)
        if project.exists()
        else ()
    )
    global_root = home / "skills"
    return (global_root if global_root.exists() else None), warnings


def _create_repl_prompt_session(
    *,
    input: Input | None = None,
    output: Output | None = None,
) -> PromptSession[str]:
    """Create the REPL editor without retaining its submitted input line."""

    return PromptSession[str](
        erase_when_done=True,
        input=input,
        output=output,
    )


def _plugin_paths_from_args(args: argparse.Namespace) -> list[str | Path]:
    from evopi.plugins import approved_plugin_entrypoints

    approved = list(approved_plugin_entrypoints(args.workspace))
    explicit = list(getattr(args, "plugin", None) or [])
    paths: list[str | Path] = [*approved, *explicit]
    return list(dict.fromkeys(paths))


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

        # Welcome — auto-detect enabled modules
        extras = []
        if harness.capabilities.memory_enabled:
            extras.append("Memory")
        if harness.capabilities.skills_enabled:
            extras.append("Skills")
        if getattr(args, "enable_subagent", False):
            extras.append("SubAgent")
        extra_str = f" | [green]+{'/'.join(extras)}[/]" if extras else ""

        console.print(Panel(
            f"[bold]EvoPi[/] — Type your message, [bold]/help[/] for commands.\n"
            f"Model: [cyan]{harness.model.name}[/] | "
            f"Session: [dim]{harness.session.session_id[:12]}...[/] | "
            f"Workspace: [dim]{harness.session.workspace[:40]}[/]"
            f"{extra_str}",
            border_style="blue",
        ))

        display = ReplDisplay()
        display.set_status(
            f"Model: {harness.model.name} | "
            f"Session: {harness.session.session_id[:12]}..."
        )
        harness.subscribe(display.handle_event)

        session = _create_repl_prompt_session()
        from evopi.cli.plugin_ui import ReplPluginUI

        harness.attach_plugin_ui(
            ReplPluginUI(
                display=display,
                prompt=session.prompt_async,
                console=console,
            )
        )
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
                    await handle_slash_command(harness, user_input)
                    continue

            set_last_prompt(user_input)

            try:
                display.show_user_message(user_input)
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
    if raw_args[:2] == ["policy", "init"]:
        return policy_init_main(raw_args[2:])
    if len(raw_args) >= 2 and raw_args[0] == "policy":
        return policy_lifecycle_main(raw_args[1], raw_args[2:])
    if raw_args[:2] == ["session", "list"]:
        return session_list_main(raw_args[2:])
    if raw_args[:2] == ["session", "gc"]:
        return session_gc_main(raw_args[2:])
    if len(raw_args) >= 2 and raw_args[0] == "plugin":
        return plugin_main(raw_args[1], raw_args[2:])

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

if __name__ == "__main__":
    raise SystemExit(main())
