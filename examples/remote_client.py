"""Connect to an approved EvoPi Remote Gateway device and stream one Run."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from evopi.remote import EvoPiRemoteClient, RemoteClientConfig, RemoteDeviceKeyStore
from evopi.rpc import RpcMessageEvent


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("device_root", type=Path)
    parser.add_argument("device_name")
    parser.add_argument("device_id")
    parser.add_argument("prompt")
    args = parser.parse_args()

    identity = RemoteDeviceKeyStore(args.device_root).load(args.device_name)
    client = await EvoPiRemoteClient.open(
        RemoteClientConfig(
            url=args.url,
            device_id=args.device_id,
            private_key=identity.private_key,
        )
    )
    try:
        await client.acquire_control()
        run = await client.start_run(args.prompt)
        async for event in run.events():
            if isinstance(event, RpcMessageEvent) and event.event_type == "message_update":
                if event.data.get("kind") == "text":
                    print(event.data.get("delta", ""), end="", flush=True)
        result = await run.wait()
        print(f"\n[{result.end_reason}; turns={result.turns_used}/{result.max_turns}]")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
