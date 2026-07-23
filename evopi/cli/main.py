"""Command-line entry point for the CodingHarness MVP."""

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
from evopi.cli.policy_review import policy_review_main
from evopi.core.events import CoreEvent
from evopi.core.model_errors import ModelRetryConfig


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
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--trace", type=Path, default=Path(".evopi/trace.jsonl"))
    parser.add_argument("--no-retry", action="store_true")
    parser.add_argument("--max-retries", type=_non_negative_int, default=3)
    parser.add_argument("--model-timeout", type=_positive_float, default=120.0)
    return parser


async def _run(args: argparse.Namespace) -> int:
    prompt = args.prompt
    if prompt is None:
        prompt = (await PromptSession[str]().prompt_async("EvoPi> ")).strip()
    if hasattr(args, "model_timeout"):
        model = model_from_environment(args.provider, timeout=args.model_timeout)
    else:  # Compatibility for callers constructing a pre-v1 Namespace.
        model = model_from_environment(args.provider)
    harness = CodingHarness(
        model=model,
        workspace=args.workspace,
        trace_path=args.trace,
        retry_config=ModelRetryConfig(
            enabled=not getattr(args, "no_retry", False),
            max_retries=getattr(args, "max_retries", 3),
        ),
        confirmation_handler=async_terminal_confirmation_handler,
    )

    def display(event: CoreEvent) -> None:
        if event.type == "message_update" and event.data.get("kind") == "text":
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


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args[:2] == ["policy", "review"]:
        return policy_review_main(raw_args[2:])
    args = (
        build_parser().parse_args()
        if argv is None
        else build_parser().parse_args(raw_args)
    )
    try:
        return asyncio.run(_run(args))
    except (ValueError, RuntimeError) as exc:
        print(f"EvoPi error: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nEvoPi aborted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
