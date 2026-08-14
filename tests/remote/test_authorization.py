from __future__ import annotations

import asyncio
from typing import Any

import pytest

from evopi.remote import (
    ControlLeaseManager,
    DeviceScope,
    RemoteAuthorizedRpcHost,
)
from evopi.rpc import RpcHostError


class _Host:
    def __getattr__(self, name: str) -> Any:
        async def call(params: dict[str, object]) -> dict[str, object]:
            return {"method": name, "params": params}

        return call


def test_observer_cannot_mutate_run_or_confirm() -> None:
    async def scenario() -> None:
        host = RemoteAuthorizedRpcHost(
            _Host(),
            device_id="device-1",
            connection_id="connection-1",
            scopes=(DeviceScope.OBSERVE,),
            leases=ControlLeaseManager(),
        )

        await host.runtime_status({})
        await host.events_replay({})
        with pytest.raises(RpcHostError, match="control"):
            await host.run_start({})
        with pytest.raises(RpcHostError, match="confirm"):
            await host.confirmation_respond({})

    asyncio.run(scenario())


def test_controller_requires_its_live_lease() -> None:
    async def scenario() -> None:
        leases = ControlLeaseManager()
        host = RemoteAuthorizedRpcHost(
            _Host(),
            device_id="device-1",
            connection_id="connection-1",
            scopes=(DeviceScope.OBSERVE, DeviceScope.CONTROL),
            leases=leases,
        )
        with pytest.raises(RpcHostError, match="lease"):
            await host.run_start({})

        leases.acquire(device_id="device-1", connection_id="connection-1")
        result = await host.run_start({"prompt": "hello"})
        assert result["method"] == "run_start"

    asyncio.run(scenario())


def test_confirm_scope_is_independent_and_shutdown_is_always_denied() -> None:
    async def scenario() -> None:
        host = RemoteAuthorizedRpcHost(
            _Host(),
            device_id="device-1",
            connection_id="connection-1",
            scopes=(DeviceScope.CONFIRM,),
            leases=ControlLeaseManager(),
        )
        await host.confirmation_list({})
        await host.confirmation_respond({})
        with pytest.raises(RpcHostError, match="disabled"):
            await host.shutdown({})

    asyncio.run(scenario())


def test_native_rpc_replay_cannot_bypass_remote_page_limits() -> None:
    class ReplayHost(_Host):
        async def events_replay(self, params: dict[str, object]) -> dict[str, object]:
            del params
            return {
                "stream_id": "stream-1",
                "after_sequence": 0,
                "oldest_sequence": 1,
                "latest_sequence": 105,
                "events": [{"sequence": sequence} for sequence in range(1, 106)],
            }

    async def scenario() -> None:
        host = RemoteAuthorizedRpcHost(
            ReplayHost(),
            device_id="device-1",
            connection_id="connection-1",
            scopes=(DeviceScope.OBSERVE,),
            leases=ControlLeaseManager(),
        )

        result = await host.events_replay({})
        assert len(result["events"]) == 100

    asyncio.run(scenario())
