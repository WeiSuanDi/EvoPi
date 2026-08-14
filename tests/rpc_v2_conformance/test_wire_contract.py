"""Implementation-independent checks for the frozen RPC v2 wire surface."""

from __future__ import annotations

import asyncio
from typing import Any

from evopi.core.types import JsonObject
from evopi.rpc import (
    RpcV2Request,
    RpcV2Server,
    decode_v2_request,
    encode_v2_request,
)
from evopi.rpc.methods_v2 import METHOD_HANDLERS_V2


EXPECTED_METHODS = {
    "initialize",
    "runtime.status",
    "run.start",
    "run.steer",
    "run.follow_up",
    "run.abort",
    "confirmation.list",
    "confirmation.respond",
    "confirmation.respond_batch",
    "events.replay",
    "shutdown",
}


class _ConformanceHost:
    def __getattr__(self, name: str) -> Any:
        async def invoke(params: JsonObject) -> JsonObject:
            if name == "initialize":
                return {
                    "protocol": "evopi.rpc.v2",
                    "schema_version": 2,
                    "host_id": "11111111-2222-4333-8444-555555555555",
                    "session_id": "session-1",
                    "stream": {
                        "stream_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                        "cursor": 0,
                        "oldest_sequence": 0,
                        "latest_sequence": 0,
                        "capacity": 100,
                    },
                    "active_tool_names": [],
                    "policy_names": [],
                    "capabilities": {
                        "event_replay": True,
                        "confirmation": True,
                        "text_steering": True,
                        "text_follow_up": True,
                    },
                    "steering_mode": "one-at-a-time",
                    "follow_up_mode": "one-at-a-time",
                }
            raise AssertionError(f"unexpected host call: {name}")

        return invoke


def test_canonical_request_and_fixed_method_surface() -> None:
    request = RpcV2Request(
        request_id="request-1",
        method="initialize",
        params={"client_name": "conformance", "client_version": "1"},
    )

    assert decode_v2_request(encode_v2_request(request)) == request
    assert set(METHOD_HANDLERS_V2) == EXPECTED_METHODS


def test_initialize_is_the_only_legal_first_method() -> None:
    async def scenario() -> None:
        server = RpcV2Server(_ConformanceHost())

        rejected = await server.dispatch(
            RpcV2Request(request_id="status-1", method="runtime.status", params={})
        )
        initialized = await server.dispatch(
            RpcV2Request(
                request_id="initialize-1",
                method="initialize",
                params={"client_name": "conformance", "client_version": "1"},
            )
        )

        assert rejected.ok is False
        assert rejected.error is not None and rejected.error.code == "not_initialized"
        assert initialized.ok is True

    asyncio.run(scenario())
