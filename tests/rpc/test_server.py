"""Fake-Host tests for the generic asynchronous RPC server."""

from __future__ import annotations

import asyncio

import pytest

from evopi.core.types import JsonObject
from evopi.rpc import (
    RpcConnectionClosedError,
    RpcErrorInfo,
    RpcHostError,
    RpcRequest,
)
from evopi.rpc.server import RpcServer

_ID = "11111111-2222-4333-8444-555555555555"


class FakeHost:
    """Configurable fake implementing the full RpcHost method set."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, JsonObject]] = []
        self.delays: dict[str, float] = {}
        self.failures: dict[str, Exception] = {}
        self.results: dict[str, JsonObject] = {}
        self.run_active = False

    async def _invoke(self, method: str, params: JsonObject) -> JsonObject:
        self.calls.append((method, params))
        failure = self.failures.get(method)
        if failure is not None:
            raise failure
        if method == "run.start":
            if self.run_active:
                raise RpcHostError("run_already_active", "a run is already active")
            self.run_active = True
        delay = self.delays.get(method, 0.0)
        if delay:
            await asyncio.sleep(delay)
        return self.results.get(method, {"ok": True, "method": method})

    async def initialize(self, params: JsonObject) -> JsonObject:
        return await self._invoke("initialize", params)

    async def runtime_status(self, params: JsonObject) -> JsonObject:
        return await self._invoke("runtime.status", params)

    async def run_start(self, params: JsonObject) -> JsonObject:
        return await self._invoke("run.start", params)

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


_METHODS: list[tuple[str, JsonObject]] = [
    ("initialize", {}),
    ("runtime.status", {}),
    ("run.start", {"prompt": "hello"}),
    ("run.abort", {}),
    ("confirmation.list", {}),
    ("confirmation.respond", {"request_id": "req-1", "decision": "approve"}),
    (
        "confirmation.respond_batch",
        {
            "responses": [
                {"request_id": "req-1", "decision": "deny", "reason": "no"},
                {"request_id": "req-2", "decision": "approve", "metadata": {"note": "ok"}},
            ]
        },
    ),
    ("events.replay", {"after_sequence": 5}),
    ("shutdown", {}),
]


@pytest.mark.parametrize("method,params", _METHODS)
def test_every_method_dispatches_with_exact_params(method: str, params: JsonObject) -> None:
    async def scenario() -> None:
        host = FakeHost()
        server = RpcServer(host)
        response = await server.dispatch(RpcRequest(request_id=_ID, method=method, params=params))
        assert response.ok is True
        assert host.calls == [(method, params)]
        assert response.result == {"ok": True, "method": method}

    asyncio.run(scenario())


def test_unknown_method_returns_method_not_found() -> None:
    async def scenario() -> None:
        host = FakeHost()
        server = RpcServer(host)
        response = await server.dispatch(RpcRequest(request_id=_ID, method="no.such", params={}))
        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "method_not_found"
        assert host.calls == []

    asyncio.run(scenario())


def test_invalid_params_rejected_and_host_not_called() -> None:
    async def scenario() -> None:
        host = FakeHost()
        server = RpcServer(host)
        cases: list[tuple[str, JsonObject]] = [
            ("run.start", {"extra": 1}),  # unknown key
            ("run.start", {}),  # missing prompt
            ("run.start", {"prompt": ""}),  # empty prompt
            ("confirmation.respond", {"decision": "approve"}),  # missing required
            ("confirmation.respond", {"request_id": "r", "decision": "pending"}),  # old status term
            ("confirmation.respond", {"request_id": "", "decision": "approve"}),  # empty request id
            ("confirmation.respond", {"request_id": "r", "decision": "approve", "reason": 5}),  # non-string reason
            ("confirmation.respond", {"request_id": "r", "decision": "approve", "metadata": []}),  # wrong type
            ("confirmation.respond", {"request_id": "r", "status": "approved"}),  # status key unknown
            ("events.replay", {"after_sequence": True}),  # boolean as integer
            ("events.replay", {"after_sequence": "5"}),  # string as integer
            ("confirmation.respond_batch", {"responses": [{"decision": "approve"}]}),  # missing batch field
            ("confirmation.respond_batch", {"responses": [{"request_id": "", "decision": "approve"}]}),  # empty batch id
            ("confirmation.respond_batch", {"responses": [{"request_id": "r", "decision": "approve", "reason": 5}]}),  # bad batch reason
            ("confirmation.respond_batch", {"responses": [{"request_id": "r", "status": "approved"}]}),  # status key in batch
            ("confirmation.respond_batch", {"responses": "nope"}),  # wrong shape
        ]
        for method, params in cases:
            response = await server.dispatch(RpcRequest(request_id=_ID, method=method, params=params))
            assert response.ok is False
            assert response.error is not None
            assert response.error.code == "invalid_params"
        assert host.calls == []

    asyncio.run(scenario())


def test_duplicate_request_id_rejected_and_host_called_once() -> None:
    async def scenario() -> None:
        host = FakeHost()
        host.delays["run.start"] = 0.05
        server = RpcServer(host)
        request = RpcRequest(request_id="dup-1", method="run.start", params={"prompt": "hello"})
        first = asyncio.create_task(server.dispatch(request))
        await asyncio.sleep(0)  # let the first dispatch register as in-flight
        duplicate = await server.dispatch(request)
        await first
        assert duplicate.ok is False
        assert duplicate.error is not None
        assert duplicate.error.code == "duplicate_request"
        assert len(host.calls) == 1

    asyncio.run(scenario())


def test_concurrent_run_rejection_maps_to_run_already_active() -> None:
    async def scenario() -> None:
        host = FakeHost()
        server = RpcServer(host)
        first = await server.dispatch(RpcRequest(request_id=_ID, method="run.start", params={"prompt": "hello"}))
        assert first.ok is True
        second = await server.dispatch(RpcRequest(request_id="other", method="run.start", params={"prompt": "hello"}))
        assert second.ok is False
        assert second.error is not None
        assert second.error.code == "run_already_active"

    asyncio.run(scenario())


def test_host_exception_redacted_as_internal_error() -> None:
    async def scenario() -> None:
        host = FakeHost()
        host.failures["runtime.status"] = RuntimeError("secret-api-key leaked")
        server = RpcServer(host)
        response = await server.dispatch(
            RpcRequest(request_id=_ID, method="runtime.status", params={})
        )
        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "internal_error"
        assert response.error.message == "internal error"
        assert "secret" not in str(response.error)
        assert "Traceback" not in str(response.error)

    asyncio.run(scenario())


def test_host_error_code_message_and_details_are_echoed() -> None:
    async def scenario() -> None:
        host = FakeHost()
        host.failures["confirmation.respond"] = RpcHostError(
            "no_pending_request",
            "no matching pending request",
            details={"request_id": "req-9"},
        )
        server = RpcServer(host)
        response = await server.dispatch(
            RpcRequest(
                request_id=_ID,
                method="confirmation.respond",
                params={"request_id": "req-9", "decision": "approve"},
            )
        )
        assert response.ok is False
        assert response.error == RpcErrorInfo(
            code="no_pending_request",
            message="no matching pending request",
            details={"request_id": "req-9"},
        )

    asyncio.run(scenario())


def test_host_cancelled_error_code_is_preserved() -> None:
    async def scenario() -> None:
        host = FakeHost()
        host.failures["run.abort"] = RpcHostError("cancelled", "aborted by caller")
        server = RpcServer(host)
        response = await server.dispatch(RpcRequest(request_id=_ID, method="run.abort", params={}))
        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "cancelled"

    asyncio.run(scenario())


def test_non_json_safe_host_result_is_internal_error() -> None:
    async def scenario() -> None:
        host = FakeHost()
        host.results["runtime.status"] = {"bad": object()}
        server = RpcServer(host)
        response = await server.dispatch(
            RpcRequest(request_id=_ID, method="runtime.status", params={})
        )
        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "internal_error"

    asyncio.run(scenario())


def test_close_cancels_inflight_and_is_idempotent() -> None:
    async def scenario() -> None:
        host = FakeHost()
        host.delays["run.start"] = 5.0
        server = RpcServer(host)
        task = asyncio.create_task(server.dispatch(RpcRequest(request_id=_ID, method="run.start", params={"prompt": "hello"})))
        await asyncio.sleep(0)  # dispatch registered and running
        await server.close()
        await asyncio.sleep(0)
        assert task.cancelled()
        await server.close()  # exactly-once behavior

    asyncio.run(scenario())


def test_dispatch_after_close_is_rejected() -> None:
    async def scenario() -> None:
        server = RpcServer(FakeHost())
        await server.close()
        with pytest.raises(RpcConnectionClosedError):
            await server.dispatch(RpcRequest(request_id=_ID, method="run.start", params={"prompt": "hello"}))

    asyncio.run(scenario())


def test_duplicate_request_id_rejected_after_completion() -> None:
    """Reuse after completion is still a duplicate for the server lifetime."""

    async def scenario() -> None:
        host = FakeHost()
        server = RpcServer(host)
        request = RpcRequest(request_id="seq-1", method="run.start", params={"prompt": "hello"})
        first = await server.dispatch(request)
        assert first.ok is True
        second = await server.dispatch(request)  # sequential reuse
        assert second.ok is False
        assert second.error is not None
        assert second.error.code == "duplicate_request"
        assert len(host.calls) == 1

    asyncio.run(scenario())


def test_duplicate_request_id_rejected_after_failure() -> None:
    """Reuse after a failed dispatch is also rejected; the Host never runs twice."""

    async def scenario() -> None:
        host = FakeHost()
        host.failures["runtime.status"] = RuntimeError("boom")
        server = RpcServer(host)
        request = RpcRequest(request_id="fail-1", method="runtime.status", params={})
        first = await server.dispatch(request)
        assert first.ok is False
        assert first.error is not None
        assert first.error.code == "internal_error"
        second = await server.dispatch(request)
        assert second.ok is False
        assert second.error is not None
        assert second.error.code == "duplicate_request"
        assert len(host.calls) == 1

    asyncio.run(scenario())


def test_seen_request_ids_released_only_on_close() -> None:
    async def scenario() -> None:
        server = RpcServer(FakeHost())
        await server.dispatch(RpcRequest(request_id="x-1", method="initialize", params={}))
        assert "x-1" in server._seen_ids
        await server.close()
        assert server._seen_ids == set()

    asyncio.run(scenario())


def test_decision_terminology_matches_confirmation_v2() -> None:
    from evopi.rpc import CONFIRMATION_DECISIONS

    assert CONFIRMATION_DECISIONS == frozenset({"approve", "deny", "cancelled"})
    # The legacy status alias must not exist in the v1 public surface.
    import evopi.rpc as rpc

    assert not hasattr(rpc, "CONFIRMATION_STATUSES")
