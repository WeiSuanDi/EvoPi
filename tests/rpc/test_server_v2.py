"""State-machine and contract tests for the RPC v2 server."""

from __future__ import annotations

import asyncio
from typing import Any

from evopi.core.types import JsonObject
from evopi.rpc.protocol_v2 import RpcV2Request
from evopi.rpc.server_v2 import RpcV2Server


class _Host:
    def __init__(self) -> None:
        self.calls: list[tuple[str, JsonObject]] = []
        self.results: dict[str, JsonObject] = {
            "initialize": {
                "protocol": "evopi.rpc.v2",
                "schema_version": 2,
                "host_id": "11111111-2222-4333-8444-555555555555",
                "session_id": "session-1",
                "stream": {
                    "stream_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                    "cursor": 0,
                    "oldest_sequence": 0,
                    "latest_sequence": 0,
                    "capacity": 1000,
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
            },
            "runtime.status": {
                "active_run_id": None,
                "lifecycle": "idle",
                "session_id": "session-1",
                "pending_confirmation_count": 0,
                "last_end_reason": None,
                "last_run_error": None,
                "steering_mode": "one-at-a-time",
                "follow_up_mode": "one-at-a-time",
                "pending_steering_count": 0,
                "pending_follow_up_count": 0,
            },
            "run.start": {"run_id": "run-1", "start_sequence": 1},
            "run.steer": {
                "input_id": "input-1",
                "kind": "steer",
                "run_id": "run-1",
                "position": 1,
            },
            "run.follow_up": {
                "input_id": "input-2",
                "kind": "follow_up",
                "run_id": "run-1",
                "position": 1,
            },
            "run.abort": {"run_id": "run-1", "aborted": True},
            "confirmation.list": {"pending": []},
            "confirmation.respond": {
                "request_id": "confirmation-1",
                "status": "approved",
                "revision": 2,
            },
            "confirmation.respond_batch": {"applied": []},
            "events.replay": {
                "stream_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                "after_sequence": 0,
                "oldest_sequence": 0,
                "latest_sequence": 0,
                "events": [],
            },
            "shutdown": {"closed": True},
        }

    def __getattr__(self, name: str) -> Any:
        method = name.replace("_", ".")
        if method == "runtime.status":
            pass
        elif method == "run.start":
            pass
        elif method == "run.steer":
            pass
        elif method == "run.follow.up":
            method = "run.follow_up"
        elif method == "run.abort":
            pass
        elif method == "confirmation.list":
            pass
        elif method == "confirmation.respond":
            pass
        elif method == "confirmation.respond.batch":
            method = "confirmation.respond_batch"
        elif method == "events.replay":
            pass
        elif method not in {"initialize", "shutdown"}:
            raise AttributeError(name)

        async def invoke(params: JsonObject) -> JsonObject:
            self.calls.append((method, params))
            return self.results[method]

        return invoke


def _request(request_id: str, method: str, params: JsonObject) -> RpcV2Request:
    return RpcV2Request(request_id=request_id, method=method, params=params)


def test_v2_server_requires_initialize_before_other_methods() -> None:
    async def scenario() -> None:
        host = _Host()
        server = RpcV2Server(host)

        response = await server.dispatch(_request("one", "runtime.status", {}))

        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "not_initialized"
        assert host.calls == []

    asyncio.run(scenario())


def test_v2_server_initializes_once_then_accepts_requests() -> None:
    async def scenario() -> None:
        host = _Host()
        server = RpcV2Server(host)
        initialized = await server.dispatch(
            _request(
                "init-1",
                "initialize",
                {"client_name": "tests", "client_version": "1.0"},
            )
        )
        status = await server.dispatch(_request("status-1", "runtime.status", {}))
        repeated = await server.dispatch(
            _request(
                "init-2",
                "initialize",
                {"client_name": "tests", "client_version": "1.0"},
            )
        )

        assert initialized.ok is True
        assert status.ok is True
        assert repeated.ok is False
        assert repeated.error is not None
        assert repeated.error.code == "already_initialized"

    asyncio.run(scenario())


def test_v2_server_requires_run_and_confirmation_revision_bindings() -> None:
    async def scenario() -> None:
        host = _Host()
        server = RpcV2Server(host)
        await server.dispatch(
            _request(
                "init",
                "initialize",
                {"client_name": "tests", "client_version": "1.0"},
            )
        )

        missing_run = await server.dispatch(
            _request("steer", "run.steer", {"content": "continue"})
        )
        missing_revision = await server.dispatch(
            _request(
                "confirm",
                "confirmation.respond",
                {"request_id": "confirmation-1", "decision": "approve"},
            )
        )

        assert missing_run.ok is False
        assert missing_run.error is not None
        assert missing_run.error.code == "invalid_params"
        assert missing_revision.ok is False
        assert missing_revision.error is not None
        assert missing_revision.error.code == "invalid_params"

    asyncio.run(scenario())


def test_v2_server_rejects_host_result_shape_drift() -> None:
    async def scenario() -> None:
        host = _Host()
        host.results["runtime.status"] = {"unexpected": True}
        server = RpcV2Server(host)
        await server.dispatch(
            _request(
                "init",
                "initialize",
                {"client_name": "tests", "client_version": "1.0"},
            )
        )

        response = await server.dispatch(_request("status", "runtime.status", {}))

        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "internal_error"

    asyncio.run(scenario())


def test_v2_server_rejects_concurrent_duplicate_initialization() -> None:
    async def scenario() -> None:
        class SlowHost(_Host):
            def __init__(self) -> None:
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def initialize(self, params: JsonObject) -> JsonObject:
                self.started.set()
                await self.release.wait()
                return self.results["initialize"]

        host = SlowHost()
        server = RpcV2Server(host)
        first = asyncio.create_task(
            server.dispatch(
                _request(
                    "init-1",
                    "initialize",
                    {"client_name": "one", "client_version": "1"},
                )
            )
        )
        await host.started.wait()

        repeated = await server.dispatch(
            _request(
                "init-2",
                "initialize",
                {"client_name": "two", "client_version": "1"},
            )
        )
        host.release.set()

        assert repeated.ok is False
        assert repeated.error is not None
        assert repeated.error.code == "already_initialized"
        assert (await first).ok is True

    asyncio.run(scenario())
