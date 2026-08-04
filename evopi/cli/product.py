"""Product-level CLI routing helpers and stable automation output."""

from __future__ import annotations

import argparse
from typing import Any

from evopi import __version__
from evopi.core.messages import AssistantMessage
from evopi.core.model_errors import ModelErrorInfo


_MANAGEMENT_ACTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "config": (
        ("show", "Show effective configuration without exposing credentials"),
    ),
    "session": (
        ("list", "List persisted sessions"),
        ("gc", "Plan or apply checkpoint garbage collection"),
    ),
    "policy": (
        ("init", "Create an inactive Policy candidate"),
        ("review", "Review a Policy candidate"),
        ("discover", "Discover governance patterns in Trace evidence"),
        ("approve", "Approve immutable review evidence"),
        ("deny", "Deny immutable review evidence"),
        ("activate", "Select an approved Policy for future runtimes"),
        ("deactivate", "Remove an active Policy selection"),
        ("rollback", "Restore an earlier approved Policy"),
        ("list", "List Policy lifecycle state"),
        ("status", "Show Policy lifecycle state"),
    ),
    "plugin": (
        ("init", "Create an inactive Plugin candidate"),
        ("examples", "List packaged Plugin SDK examples"),
        ("review", "Review a Plugin candidate"),
        ("approve", "Approve a digest-bound Plugin artifact"),
        ("deny", "Deny a Plugin candidate"),
        ("remove", "Remove an installed Plugin selection"),
        ("list", "List Plugin lifecycle state"),
        ("reload", "Validate the next Plugin runtime snapshot"),
    ),
}


def build_product_parser() -> argparse.ArgumentParser:
    """Build the discoverable top-level product help parser."""

    parser = argparse.ArgumentParser(
        prog="evopi",
        description="Policy-governed, evolution-ready agent runtime",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Conversation:\n"
            "  evopi                         Start the interactive workbench\n"
            '  evopi "PROMPT"                Run one prompt (legacy compatible)\n'
            "  evopi chat [INITIAL_PROMPT]   Start the workbench, optionally with a prompt\n"
            "  evopi run [PROMPT] [--json]   Run once for scripts and automation\n\n"
            "Host integration:\n"
            "  evopi rpc                     Run the local JSONL host over stdio\n\n"
            "Management:\n"
            "  evopi session ...             Inspect and maintain sessions\n"
            "  evopi policy ...              Review, approve, and activate Policies\n"
            "  evopi plugin ...              Review, approve, and load Plugins\n"
            "  evopi config show             Inspect effective configuration\n"
            "  evopi doctor                  Run offline diagnostics\n\n"
            "Use 'evopi <command> --help' for command-specific help."
        ),
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", help="Show this help message and exit")
    parser.add_argument(
        "--version",
        action="version",
        version=f"EvoPi {__version__}",
        help="Show the EvoPi version and exit",
    )
    return parser


def format_management_help(command: str) -> str:
    """Return product help for one management command group."""

    actions = _MANAGEMENT_ACTIONS[command]
    width = max(len(name) for name, _ in actions)
    lines = [
        f"usage: evopi {command} ACTION [OPTIONS]",
        "",
        f"Governed {command} operations:",
        "",
        "actions:",
    ]
    lines.extend(f"  {name:<{width}}  {description}" for name, description in actions)
    lines.extend(
        [
            "",
            f"Use 'evopi {command} ACTION --help' for action-specific help.",
        ]
    )
    return "\n".join(lines)


def compose_run_prompt(stdin_text: str, argument: str | None) -> str:
    """Combine optional piped input and positional prompt deterministically."""

    piped = stdin_text.rstrip("\r\n")
    positional = (argument or "").strip()
    if piped and positional:
        return f"{piped}\n\n{positional}"
    return piped or positional


def safe_error_info(info: ModelErrorInfo | None) -> dict[str, Any] | None:
    """Serialize only the public, bounded portion of provider error details."""

    if info is None:
        return None
    return {
        "kind": info.kind,
        "message": info.message,
        "provider": info.provider,
        "retryable": info.retryable,
        "status_code": info.status_code,
        "code": info.code,
        "retry_after": info.retry_after,
        "request_id": info.request_id,
    }


def build_run_result(
    harness: Any,
    answer: AssistantMessage | None,
) -> dict[str, Any]:
    """Build the stable, privacy-minimal ``evopi run --json`` schema."""

    state = harness.agent.last_run
    return {
        "schema_version": 1,
        "session_id": harness.session.session_id,
        "run_id": state.run_id if state is not None else None,
        "end_reason": state.end_reason if state is not None else "error",
        "turns_used": state.turns_used if state is not None else 0,
        "max_turns": state.max_turns if state is not None else harness.agent.max_turns,
        "assistant": (
            {
                "id": answer.id,
                "content": answer.content,
                "stop_reason": answer.stop_reason,
            }
            if answer is not None
            else None
        ),
        "error_info": safe_error_info(
            state.error_info if state is not None else None
        ),
    }


def run_exit_code(end_reason: str) -> int:
    """Map a Core end reason to the public automation exit contract."""

    if end_reason == "aborted":
        return 130
    if end_reason in {"completed", "terminated"}:
        return 0
    return 1


__all__ = [
    "build_product_parser",
    "build_run_result",
    "compose_run_prompt",
    "format_management_help",
    "run_exit_code",
    "safe_error_info",
]
