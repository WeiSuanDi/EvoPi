"""Public BaseHarness binding for the local RPC v1 protocol.

The Host exposes the frozen interaction methods ``run.steer`` and
``run.follow_up``: it awaits the Lane 1 Harness interaction surface with
``origin="rpc"``, returns the exact receipt shape, and maps structured
interaction errors to the frozen safe codes. No content ever enters a
response, status, or error.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol, cast

from evopi.core.events import CoreEvent
from evopi.core.types import JsonObject
from evopi.harness import BaseHarness
from evopi.harness.confirmation import (
    ConfirmationBatchResponse,
    ConfirmationError,
    ConfirmationResponse,
)
from evopi.harness.confirmation_broker import ConfirmationBroker
from evopi.harness.confirmation_codec import encode_record

from .codec import to_event_data
from .errors import (
    EventCursorExpiredError,
    EventCursorInvalidError,
    EventStreamClosedError,
    RpcHostError,
)
from .event_stream import EventStream


class InteractionReceiptView(Protocol):
    """Frozen public receipt shape (CONTEXT.md section 3)."""

    input_id: str
    run_id: str
    kind: str
    origin: str
    position: int


class InteractionSnapshotView(Protocol):
    """Frozen public queue snapshot shape (CONTEXT.md section 3)."""

    steering_mode: str
    follow_up_mode: str
    pending_steering_count: int
    pending_follow_up_count: int


class InteractionHarness(Protocol):
    """Harness interaction surface introduced by Lane 1 (frozen contract).

    Lane 1 implements these methods on ``BaseHarness``. Until both Lanes are
    integrated, the Host binds this structural Protocol and Integration proves
    the real binding; the protocol keeps typos against the frozen surface out
    of this Lane.
    """

    async def steer(self, content: str, *, origin: str) -> InteractionReceiptView: ...
    async def follow_up(self, content: str, *, origin: str) -> InteractionReceiptView: ...

    @property
    def interaction_snapshot(self) -> InteractionSnapshotView: ...


# Structured interaction errors are mapped to the frozen safe RPC codes by
# their exact class names, which are part of the public contract
# (CONTEXT.md sections 3 and 6). Queue modes are construction-time settings
# with no live setter, so InteractionModeError and the abstract base never
# surface through RPC; they fall through to the generic redacted
# internal_error like any unexpected Harness failure.
_INTERACTION_ERRORS: dict[str, tuple[str, str]] = {
    "InteractionQueueClosedError": ("interaction_closed", "interaction queue is closed"),
    "InteractionQueueFullError": ("interaction_queue_full", "interaction queue is full"),
    "InteractionContentTooLargeError": (
        "interaction_content_too_large",
        "interaction content is too large",
    ),
    "InteractionContentError": (
        "interaction_content_invalid",
        "interaction content is invalid",
    ),
}


def _interaction_host_error(exc: Exception) -> RpcHostError | None:
    """Map a structured interaction error to its frozen safe RPC code."""
    mapped = _INTERACTION_ERRORS.get(type(exc).__name__)
    if mapped is None:
        return None
    code, message = mapped
    return RpcHostError(code=code, message=message, details={})


class HarnessRpcHost:
    """Expose one live Harness through the fixed, transport-neutral RPC methods.

    The Host starts Runs asynchronously so the same connection can resolve a
    Policy-created Confirmation or request Abort. It never creates a pending
    request itself and never invokes a Tool directly.
    """

    def __init__(
        self,
        harness: BaseHarness,
        broker: ConfirmationBroker,
        *,
        event_stream: EventStream | None = None,
    ) -> None:
        if harness.confirmation_broker is not broker:
            raise ValueError("HarnessRpcHost requires the Harness-bound ConfirmationBroker")
        self.harness = harness
        self.broker = broker
        self.events = event_stream or EventStream()
        self._run_task: asyncio.Task[Any] | None = None
        self._run_started: asyncio.Future[str] | None = None
        self._active_run_id: str | None = None
        self._last_run_error: str | None = None
        self._closed = False
        self._unsubscribe: Callable[[], None] = harness.subscribe(self._on_event)

    @property
    def closed(self) -> bool:
        return self._closed

    async def initialize(self, params: JsonObject) -> JsonObject:
        capabilities = self.harness.capabilities
        snapshot = self._snapshot()
        return {
            "protocol": "evopi.rpc.v1",
            "schema_version": 1,
            "session_id": self.harness.session.session_id,
            "active_tool_names": list(capabilities.active_tool_names),
            "policy_names": list(capabilities.policy_names),
            "capabilities": {"text_steering": True, "text_follow_up": True},
            "steering_mode": snapshot.steering_mode,
            "follow_up_mode": snapshot.follow_up_mode,
        }

    async def runtime_status(self, params: JsonObject) -> JsonObject:
        last_run = self.harness.last_run
        snapshot = self._snapshot()
        return {
            "active_run_id": self._active_run_id,
            "lifecycle": str(self.harness.state.status),
            "session_id": self.harness.session.session_id,
            "pending_confirmation_count": len(self.broker.list_pending()),
            "last_end_reason": last_run.end_reason if last_run is not None else None,
            "last_run_error": self._last_run_error,
            "steering_mode": snapshot.steering_mode,
            "follow_up_mode": snapshot.follow_up_mode,
            "pending_steering_count": snapshot.pending_steering_count,
            "pending_follow_up_count": snapshot.pending_follow_up_count,
        }

    async def run_steer(self, params: JsonObject) -> JsonObject:
        return await self._queue_interaction("steer", cast(str, params["content"]))

    async def run_follow_up(self, params: JsonObject) -> JsonObject:
        return await self._queue_interaction("follow_up", cast(str, params["content"]))

    async def _queue_interaction(self, kind: str, content: str) -> JsonObject:
        if self._closed:
            raise RpcHostError(
                code="host_closed",
                message="rpc host is closed",
                details={},
            )
        task = self._run_task
        if task is None or task.done():
            raise RpcHostError(
                code="run_not_active",
                message="interaction requires an active run",
                details={},
            )
        surface = cast(InteractionHarness, self.harness)
        try:
            if kind == "steer":
                receipt = await surface.steer(content, origin="rpc")
            else:
                receipt = await surface.follow_up(content, origin="rpc")
        except Exception as exc:
            mapped = _interaction_host_error(exc)
            if mapped is not None:
                raise mapped from None
            raise
        return {
            "input_id": receipt.input_id,
            "kind": receipt.kind,
            "run_id": receipt.run_id,
            "position": receipt.position,
        }

    def _snapshot(self) -> InteractionSnapshotView:
        return cast(InteractionHarness, self.harness).interaction_snapshot

    async def run_start(self, params: JsonObject) -> JsonObject:
        if self._closed:
            raise RpcHostError(
                code="host_closed",
                message="rpc host is closed",
                details={},
            )
        if self._run_task is not None and not self._run_task.done():
            raise RpcHostError(
                code="run_already_active",
                message="another run is already active",
                details={},
            )

        prompt = cast(str, params["prompt"])
        loop = asyncio.get_running_loop()
        started = loop.create_future()
        self._run_started = started
        self._last_run_error = None
        task = asyncio.create_task(self.harness.prompt(prompt))
        self._run_task = task
        task.add_done_callback(self._run_finished)

        done, _ = await asyncio.wait({task, started}, return_when=asyncio.FIRST_COMPLETED)
        if started in done:
            run_id = started.result()
            return {"run_id": run_id}

        # A startup failure happened before agent_start could establish a Run.
        try:
            task.result()
        except asyncio.CancelledError:
            message = "run startup was cancelled"
        except Exception:
            message = "run startup failed"
        else:
            message = "run ended before startup was acknowledged"
        raise RpcHostError(code="run_start_failed", message=message, details={})

    async def run_abort(self, params: JsonObject) -> JsonObject:
        task = self._run_task
        if task is None or task.done():
            return {"aborted": False}
        self.harness.abort()
        await asyncio.gather(task, return_exceptions=True)
        return {"aborted": True}

    async def confirmation_list(self, params: JsonObject) -> JsonObject:
        return {
            "pending": [encode_record(record) for record in self.broker.list_pending()]
        }

    async def confirmation_respond(self, params: JsonObject) -> JsonObject:
        response = self._response_from_params(params)
        try:
            record = await self.broker.submit(response)
        except ConfirmationError as exc:
            raise self._confirmation_error(exc) from None
        return {
            "request_id": record.request.id,
            "status": record.status,
            "revision": record.revision,
        }

    async def confirmation_respond_batch(self, params: JsonObject) -> JsonObject:
        raw_responses = cast(list[JsonObject], params["responses"])
        responses = tuple(self._response_from_params(item) for item in raw_responses)
        try:
            records = await self.broker.submit_batch(
                ConfirmationBatchResponse(responses=responses)
            )
        except ConfirmationError as exc:
            raise self._confirmation_error(exc) from None
        return {
            "applied": [
                {
                    "request_id": record.request.id,
                    "status": record.status,
                    "revision": record.revision,
                }
                for record in records
            ]
        }

    async def events_replay(self, params: JsonObject) -> JsonObject:
        try:
            events = self.events.replay(after_sequence=cast(int, params["after_sequence"]))
        except EventCursorExpiredError as exc:
            raise RpcHostError(
                code="event_cursor_expired",
                message="event cursor is older than retained history",
                details={},
            ) from exc
        except EventCursorInvalidError as exc:
            raise RpcHostError(
                code="event_cursor_invalid",
                message="event cursor is invalid",
                details={},
            ) from exc
        except EventStreamClosedError as exc:
            raise RpcHostError(
                code="event_stream_closed",
                message="event stream is closed",
                details={},
            ) from exc
        return {
            "events": [cast(JsonObject, to_event_data(event)) for event in events]
        }

    async def shutdown(self, params: JsonObject) -> JsonObject:
        await self.close()
        return {"closed": True}

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failure: Exception | None = None
        task = self._run_task
        if task is not None and not task.done():
            self.harness.abort()
            await asyncio.gather(task, return_exceptions=True)
        self._unsubscribe()
        try:
            self.harness.close()
        except Exception as exc:
            failure = exc
        finally:
            self.events.close()
        if failure is not None:
            raise failure

    def _on_event(self, event: CoreEvent) -> None:
        self.events.publish(event)
        if event.type == "agent_start" and event.run_id is not None:
            self._active_run_id = event.run_id
            started = self._run_started
            if started is not None and not started.done():
                started.set_result(event.run_id)

    def _run_finished(self, task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            self._last_run_error = "cancelled"
        else:
            error = task.exception()
            self._last_run_error = type(error).__name__ if error is not None else None
        self._active_run_id = None
        self._run_started = None
        if self._run_task is task:
            self._run_task = None

    @staticmethod
    def _response_from_params(params: JsonObject) -> ConfirmationResponse:
        metadata = params.get("metadata")
        return ConfirmationResponse(
            request_id=cast(str, params["request_id"]),
            decision=cast(Any, params["decision"]),
            reason=cast(str, params.get("reason", "")),
            metadata=cast(JsonObject, metadata if metadata is not None else {}),
        )

    @staticmethod
    def _confirmation_error(exc: ConfirmationError) -> RpcHostError:
        return RpcHostError(
            code=exc.code,
            message="confirmation operation rejected",
            details=dict(exc.details),
        )


__all__ = [
    "HarnessRpcHost",
    "InteractionHarness",
    "InteractionReceiptView",
    "InteractionSnapshotView",
]
