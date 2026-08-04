"""Server-level tests for the interaction RPC method set (SFU-2 Task 1).

The server validates the exact ``{"content": <non-empty string>}`` params and
tracks duplicate request IDs. Strict non-whitespace content validation and
size limits are Harness/core rules surfaced as ``interaction_content_invalid``
/ ``interaction_content_too_large``; the server schema only requires a string.
"""

from __future__ import annotations

import asyncio

import pytest

from evopi.core.types import JsonObject
from evopi.rpc import RpcHostError, RpcRequest
from evopi.rpc.server import RpcServer

_ID = "11111111-2222-4333-8444-555555555555"


class FakeInteractionHost:
    """RpcHost fake that includes the interaction method set."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, JsonObject]] = []
        self.failures: dict[str, Exception] = {}

    async def _invoke(self, method: str, params: JsonObject) -> JsonObject:
        self.calls.append((method, params))
        failure = self.failures.get(method)
        if failure is not None:
            raise failure
        return {"ok": True, "method": method}

    async def initialize(self, params: JsonObject) -> JsonObject:
        return await self._invoke("initialize", params)

    async def runtime_status(self, params: JsonObject) -> JsonObject:
        return await self._invoke("runtime.status", params)

    async def run_start(self, params: JsonObject) -> JsonObject:
        return await self._invoke("run.start", params)

    async def run_steer(self, params: JsonObject) -> JsonObject:
        return await self._invoke("run.steer", params)

    async def run_follow_up(self, params: JsonObject) -> JsonObject:
        return await self._invoke("run.follow_up", params)

    async def run_abort(self, params: JsonObject) -> JsonObject:
        return await self._invoke("run.abort", params)

    async def confirmation_list(self, params: JsonObject) -> JsonObject:
        return await self._invoke("confirmation.list", params)

    async def confirmation_respond(self, params: JsonObject) -> JsonObject:
        return await self._invoke("confirmation.respond", params)

    async def confirmation_respond_batch(self, params: JsonObject) -> JsonObject:
        return await self._invoke("confirmation.respond_batch", params)

    async def events_replay(self, params: JsonObject) -> JsonObject:
        return await self._invoke("events.replay", params)

    async def shutdown(self, params: JsonObject) -> JsonObject:
        return await self._invoke("shutdown", params)


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("run.steer", {"content": "continue"}),
        ("run.follow_up", {"content": "one more thing"}),
    ],
)
def test_interaction_methods_dispatch_with_exact_params(
    method: str,
    params: JsonObject,
) -> None:
    async def scenario() -> None:
        host = FakeInteractionHost()
        server = RpcServer(host)
        response = await server.dispatch(
            RpcRequest(request_id=_ID, method=method, params=params)
        )
        assert response.ok is True
        assert host.calls == [(method, params)]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "params",
    [
        {},  # missing content
        {"content": ""},  # empty content
        {"content": 5},  # non-string content
        {"content": True},  # boolean trick
        {"content": ["x"]},  # list content
        {"content": "x", "extra": 1},  # unknown key
    ],
)
def test_interaction_invalid_params_rejected_before_host(params: JsonObject) -> None:
    async def scenario() -> None:
        host = FakeInteractionHost()
        server = RpcServer(host)
        for method in ("run.steer", "run.follow_up"):
            response = await server.dispatch(
                RpcRequest(request_id=_ID, method=method, params=params)
            )
            assert response.ok is False
            assert response.error is not None
            assert response.error.code == "invalid_params"
        assert host.calls == []

    asyncio.run(scenario())


def test_whitespace_only_content_reaches_the_host() -> None:
    """Non-whitespace validation is a Harness rule, not a server schema rule."""

    async def scenario() -> None:
        host = FakeInteractionHost()
        server = RpcServer(host)
        response = await server.dispatch(
            RpcRequest(request_id=_ID, method="run.steer", params={"content": "   "})
        )
        assert response.ok is True
        assert host.calls == [("run.steer", {"content": "   "})]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("method", "code", "message"),
    [
        ("run.steer", "run_not_active", "no active run"),
        ("run.follow_up", "interaction_queue_full", "queue full"),
        ("run.steer", "interaction_closed", "queue closed"),
    ],
)
def test_interaction_host_errors_echo_frozen_codes(
    method: str,
    code: str,
    message: str,
) -> None:
    async def scenario() -> None:
        host = FakeInteractionHost()
        host.failures[method] = RpcHostError(code, message)
        server = RpcServer(host)
        response = await server.dispatch(
            RpcRequest(request_id=_ID, method=method, params={"content": "hello"})
        )
        assert response.ok is False
        assert response.error is not None
        assert response.error.code == code
        assert response.error.message == message

    asyncio.run(scenario())


def test_interaction_duplicate_request_id_rejected() -> None:
    async def scenario() -> None:
        host = FakeInteractionHost()
        server = RpcServer(host)
        request = RpcRequest(request_id="interact-1", method="run.steer", params={"content": "x"})
        first = await server.dispatch(request)
        assert first.ok is True
        duplicate = await server.dispatch(request)
        assert duplicate.ok is False
        assert duplicate.error is not None
        assert duplicate.error.code == "duplicate_request"
        assert len(host.calls) == 1

    asyncio.run(scenario())
