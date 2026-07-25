"""Command-line entry point for the EvoPi coding agent."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from prompt_toolkit import PromptSession

from evopi.ai.models import model_from_environment
from evopi.coding.harness import CodingHarness
from evopi.cli.confirmation import async_terminal_confirmation_handler
from evopi.cli.display import ReplDisplay
from evopi.cli.policy_review import policy_review_main
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
    parser.add_argument(
        "--session-root",
        type=Path,
        help="Override the persisted Session root directory",
    )
    return parser


def _build_harness(args: argparse.Namespace) -> CodingHarness:
    """Build a CodingHarness from CLI args."""
    from evopi.session.compact import CompactionSettings

    session_manager = _session_manager_from_args(args)
    # Model
    model = model_from_environment(
        getattr(args, "provider", None),
        timeout=getattr(args, "model_timeout", 120.0),
        model=getattr(args, "model", None),
        context_window=getattr(args, "context_window", 0),
    )

    # Harness
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
    )


async def _run_one_shot(args: argparse.Namespace) -> int:
    """Single prompt → response → exit."""
    prompt = args.prompt
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
        answer = await harness.prompt(prompt)
        if not answer.content.endswith("\n"):
            print()
        return 0
    finally:
        if harness is not None and not harness.is_running:
            harness.close()


async def _run_repl(args: argparse.Namespace) -> int:
    """Multi-turn REPL: prompt → response → prompt → ..."""
    harness: CodingHarness | None = None
    try:
        harness = _build_harness(args)

        display = ReplDisplay()
        harness.subscribe(display.handle_event)

        session = PromptSession[str]()
        print("EvoPi — type your message, Ctrl+C to abort, Ctrl+D to exit.")
        print("/branch /switch /fork /compact /leaves\n")

        while True:
            try:
                user_input = (await session.prompt_async("> ")).strip()
            except KeyboardInterrupt:
                print("\nEvoPi aborted.")
                return 130
            except EOFError:
                print("\nGoodbye.")
                return 0

            if not user_input:
                continue

            if user_input.startswith("/"):
                _handle_slash_command(harness, user_input)
                continue

            try:
                display.start_run()
                await harness.prompt(user_input)
                display.end_run()
            except (ValueError, RuntimeError) as exc:
                display.end_run()
                print(f"Error: {exc}", file=sys.stderr)
                continue
            except KeyboardInterrupt:
                display.end_run()
                print("  [aborted]")
                continue
    finally:
        if harness is not None and not harness.is_running:
            harness.close()


def _handle_slash_command(harness: CodingHarness, text: str) -> None:
    """Process a REPL slash-command against the active session."""
    parts = text.split()
    cmd = parts[0].lower()
    session = harness.session

    if cmd == "/leaves":
        leaves = session.leaves()
        active = session.leaf_id
        print(f"{len(leaves)} leaf(ves):")
        for lid in leaves:
            marker = " *" if lid == active else ""
            print(f"  {lid[:16]}...{marker}")

    elif cmd == "/switch":
        if len(parts) < 2:
            print("Usage: /switch <leaf_id>")
            return
        try:
            session.switch_leaf(parts[1])
            lid = session.leaf_id or "?"
            print(f"Switched to leaf {lid[:16]}...")
        except Exception as exc:
            print(f"Error: {exc}")

    elif cmd == "/branch":
        if session.leaf_id is None:
            print("No active leaf to branch from.")
            return
        name = parts[1] if len(parts) > 1 else ""
        try:
            entry = session.branch(from_entry_id=session.leaf_id, branch_name=name)
            pid = entry.parent_id or "?"
            print(f"Branched from {pid[:16]}...")
            print(f"New leaf: {entry.entry_id[:16]}...")
        except Exception as exc:
            print(f"Error: {exc}")

    elif cmd == "/fork":
        try:
            new_session = session.fork()
            print(f"Forked to new session: {new_session.session_id[:16]}...")
            print("(original session unchanged; restart with --session to use fork)")
        except Exception as exc:
            print(f"Error: {exc}")

    elif cmd == "/compact":
        if len(parts) < 2:
            print("Usage: /compact <summary of compacted messages>")
            return
        if session.leaf_id is None:
            print("No active leaf to compact.")
            return
        summary = text[len("/compact "):].strip()
        # Find last checkpoint as the compaction anchor
        path = session.get_active_path()
        anchor_id = path[0].entry_id if path else session.leaf_id
        for path_entry in reversed(path):
            if getattr(path_entry, "type", None) == "checkpoint":
                anchor_id = path_entry.entry_id
                break
        try:
            session.compact(up_to_entry_id=anchor_id, summary=summary)
            print(f"Compacted. Active leaf now at {session.leaf_id[:16]}...")
        except Exception as exc:
            print(f"Error: {exc}")

    else:
        print(f"Unknown command: {cmd}")
        print("Available: /leaves  /switch <id>  /branch [name]  /fork  /compact <summary>")


def _session_manager_from_args(args: argparse.Namespace) -> SessionManager:
    workspace = getattr(args, "workspace", Path.cwd())
    if not hasattr(args, "session_root"):
        return SessionManager.in_memory(workspace)
    root = args.session_root
    if args.no_session:
        return SessionManager.in_memory(workspace)
    if args.new_session:
        return SessionManager.create(workspace, root=root)
    if args.session is not None:
        return SessionManager.open(args.session, workspace=workspace, root=root)
    return SessionManager.continue_recent(workspace, root=root)


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)

    # Sub-commands
    if raw_args[:2] == ["policy", "review"]:
        return policy_review_main(raw_args[2:])
    if raw_args[:2] == ["session", "list"]:
        return session_list_main(raw_args[2:])

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
