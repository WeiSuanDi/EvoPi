"""Run EvoPi through the typed local RPC v2 client.

Examples:
    python examples/rpc_v2_client.py "Summarize README.md"
    python examples/rpc_v2_client.py "Inspect the project" --steer "Focus on tests"
    python examples/rpc_v2_client.py "Work carefully" --abort-after 5
"""

from __future__ import annotations

import argparse
import asyncio

from evopi.rpc import (
    EvoPiRpcClient,
    RpcConfirmationAnswer,
    RpcConfirmationRecord,
    RpcMessageEvent,
)


async def confirm(record: RpcConfirmationRecord) -> RpcConfirmationAnswer | None:
    answer = await asyncio.to_thread(
        input,
        f"Approve {record.tool_name or record.hook} ({record.risk_level})? [y/N]: ",
    )
    decision = "approve" if answer.strip().lower() in {"y", "yes"} else "deny"
    return RpcConfirmationAnswer(
        request_id=record.request_id,
        expected_revision=record.revision,
        decision=decision,
        reason="answered by rpc_v2_client.py",
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--steer")
    parser.add_argument("--follow-up")
    parser.add_argument("--abort-after", type=float)
    args = parser.parse_args()

    client = await EvoPiRpcClient.spawn(confirmation_handler=confirm)
    try:
        run = await client.start_run(args.prompt)
        if args.steer:
            await run.steer(args.steer)
        if args.follow_up:
            await run.follow_up(args.follow_up)
        abort_task = None
        if args.abort_after is not None:
            async def abort_later() -> None:
                await asyncio.sleep(args.abort_after)
                await run.abort()

            abort_task = asyncio.create_task(abort_later())

        async for event in run.events():
            if isinstance(event, RpcMessageEvent) and event.event_type == "message_update":
                if event.data.get("kind") == "text":
                    print(event.data.get("delta", ""), end="", flush=True)
        result = await run.wait()
        if abort_task is not None and not abort_task.done():
            abort_task.cancel()
        print(f"\n[{result.end_reason}; turns={result.turns_used}/{result.max_turns}]")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
