from __future__ import annotations

import asyncio

import pytest

from evopi.remote import RemoteConnectionRegistry, RemoteRateLimitError, RemoteSendQueue


def test_connection_registry_enforces_global_ip_and_device_caps() -> None:
    registry = RemoteConnectionRegistry(max_global=2, max_per_ip=1, max_per_device=1)
    registry.open("c1", "203.0.113.1")
    with pytest.raises(RemoteRateLimitError, match="IP"):
        registry.open("c2", "203.0.113.1")
    registry.open("c2", "203.0.113.2")
    with pytest.raises(RemoteRateLimitError, match="global"):
        registry.open("c3", "203.0.113.3")

    registry.authenticate("c1", "device-1")
    with pytest.raises(RemoteRateLimitError, match="device"):
        registry.authenticate("c2", "device-1")
    registry.close("c1")
    registry.authenticate("c2", "device-1")


def test_send_queue_enforces_item_and_byte_bounds() -> None:
    queue = RemoteSendQueue(max_items=2, max_bytes=5)
    queue.put_nowait("ab")
    queue.put_nowait("cd")
    with pytest.raises(RemoteRateLimitError, match="queue"):
        queue.put_nowait("e")


def test_full_send_queue_still_terminates_after_close() -> None:
    async def scenario() -> None:
        queue = RemoteSendQueue(max_items=1, max_bytes=5)
        queue.put_nowait("full")

        queue.close()

        assert await queue.get() == "full"
        assert await asyncio.wait_for(queue.get(), timeout=0.1) is None

    asyncio.run(scenario())
