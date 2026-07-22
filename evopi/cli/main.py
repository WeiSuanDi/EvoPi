"""Command-line entry point for the CodingHarness MVP."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from evopi.ai.models import model_from_environment
from evopi.coding.harness import CodingHarness
from evopi.core.events import CoreEvent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evopi", description="Run the EvoPi coding agent")
    parser.add_argument("prompt", nargs="?", help="Task for the agent")
    parser.add_argument("--provider", choices=["anthropic", "openai-compatible"])
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--trace", type=Path, default=Path(".evopi/trace.jsonl"))
    return parser


async def _run(args: argparse.Namespace) -> int:
    prompt = args.prompt or input("EvoPi> ").strip()
    model = model_from_environment(args.provider)
    harness = CodingHarness(
        model=model,
        workspace=args.workspace,
        trace_path=args.trace,
    )

    def display(event: CoreEvent) -> None:
        if event.type == "model_delta" and event.data.get("kind") == "text":
            print(event.data.get("delta", ""), end="", flush=True)

    harness.subscribe(display)
    answer = await harness.prompt(prompt)
    if not answer.content.endswith("\n"):
        print()
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(_run(args))
    except (ValueError, RuntimeError) as exc:
        print(f"EvoPi error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
