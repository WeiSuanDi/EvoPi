"""RPC v2 evidence-binding adapter over the existing Harness RPC host."""

from __future__ import annotations

import json
from typing import cast
from uuid import uuid4

from evopi.core.types import JsonObject

from .codec_v2 import encode_v2_event
from .errors import EventCursorExpiredError, RpcHostError
from .harness_host import HarnessRpcHost
from .protocol import RpcEvent
from .protocol_v2 import RpcV2Event


class HarnessRpcV2Host:
    """Add v2 Run, Confirmation, and Event identity to ``HarnessRpcHost``.

    Execution remains owned by the wrapped v1 Host and its Harness. This
    adapter only validates evidence bindings and projects the v2 wire shapes.
    """

    def __init__(self, host: HarnessRpcHost, *, host_id: str | None = None) -> None:
        self._host = host
        self._host_id = host_id or str(uuid4())

    @property
    def host_id(self) -> str:
        return self._host_id

    @property
    def stream_id(self) -> str:
        return self._host.events.stream_id

    def project_event(self, event: RpcEvent) -> RpcV2Event:
        """Bind one retained legacy event to this Host's v2 stream identity."""

        return RpcV2Event(
            event_id=event.event_id,
            stream_id=self.stream_id,
            sequence=event.sequence,
            type=event.type,
            data=event.data,
            run_id=event.run_id,
            created_at=event.created_at,
        )

    async def initialize(self, params: JsonObject) -> JsonObject:
        del params
        legacy = await self._host.initialize({})
        window = self._host.events.snapshot(
            after_sequence=self._host.events.latest_sequence
        )
        return {
            "protocol": "evopi.rpc.v2",
            "schema_version": 2,
            "host_id": self._host_id,
            "session_id": legacy["session_id"],
            "stream": {
                "stream_id": window.stream_id,
                "cursor": window.latest_sequence,
                "oldest_sequence": window.oldest_sequence,
                "latest_sequence": window.latest_sequence,
                "capacity": window.capacity,
            },
            "active_tool_names": legacy["active_tool_names"],
            "policy_names": legacy["policy_names"],
            "capabilities": {
                "event_replay": True,
                "confirmation": True,
                "text_steering": legacy["capabilities"]["text_steering"],
                "text_follow_up": legacy["capabilities"]["text_follow_up"],
            },
            "steering_mode": legacy["steering_mode"],
            "follow_up_mode": legacy["follow_up_mode"],
        }

    async def runtime_status(self, params: JsonObject) -> JsonObject:
        return await self._host.runtime_status(params)

    async def run_start(self, params: JsonObject) -> JsonObject:
        result = await self._host.run_start({"prompt": params["prompt"]})
        run_id = cast(str, result["run_id"])
        started = self._host.last_started_run
        if started is None or started[0] != run_id:
            raise RpcHostError(
                code="run_start_failed",
                message="run start event was not correlated",
                details={"run_id": run_id},
            )
        return {"run_id": run_id, "start_sequence": started[1]}

    async def run_steer(self, params: JsonObject) -> JsonObject:
        await self._require_active_run(cast(str, params["run_id"]))
        return await self._host.run_steer({"content": params["content"]})

    async def run_follow_up(self, params: JsonObject) -> JsonObject:
        await self._require_active_run(cast(str, params["run_id"]))
        return await self._host.run_follow_up({"content": params["content"]})

    async def run_abort(self, params: JsonObject) -> JsonObject:
        run_id = cast(str, params["run_id"])
        await self._require_active_run(run_id)
        result = await self._host.run_abort({})
        return {"run_id": run_id, "aborted": result["aborted"]}

    async def _require_active_run(self, run_id: str) -> None:
        status = await self._host.runtime_status({})
        active_run_id = status["active_run_id"]
        if active_run_id != run_id:
            raise RpcHostError(
                code="run_mismatch",
                message="run does not match the active run",
                details={"active_run_id": active_run_id},
            )

    async def confirmation_list(self, params: JsonObject) -> JsonObject:
        return await self._host.confirmation_list(params)

    async def confirmation_respond(self, params: JsonObject) -> JsonObject:
        request_id = cast(str, params["request_id"])
        self._require_confirmation_revision(
            request_id,
            cast(int, params["expected_revision"]),
        )
        return await self._host.confirmation_respond(
            {
                "request_id": request_id,
                "decision": params["decision"],
                "reason": params["reason"],
                "metadata": params["metadata"],
            }
        )

    async def confirmation_respond_batch(self, params: JsonObject) -> JsonObject:
        responses = cast(list[JsonObject], params["responses"])
        for response in responses:
            self._require_confirmation_revision(
                cast(str, response["request_id"]),
                cast(int, response["expected_revision"]),
            )
        return await self._host.confirmation_respond_batch(
            {
                "responses": [
                    {
                        "request_id": response["request_id"],
                        "decision": response["decision"],
                        "reason": response["reason"],
                        "metadata": response["metadata"],
                    }
                    for response in responses
                ]
            }
        )

    def _require_confirmation_revision(
        self,
        request_id: str,
        expected_revision: int,
    ) -> None:
        record = next(
            (
                item
                for item in self._host.broker.list_pending()
                if item.request.id == request_id
            ),
            None,
        )
        if record is None:
            return
        if record.revision != expected_revision:
            raise RpcHostError(
                code="stale_revision",
                message="confirmation revision is stale",
                details={
                    "request_id": request_id,
                    "expected_revision": expected_revision,
                    "actual_revision": record.revision,
                },
            )

    async def events_replay(self, params: JsonObject) -> JsonObject:
        stream_id = cast(str, params["stream_id"])
        after_sequence = cast(int, params["after_sequence"])
        if stream_id != self.stream_id:
            raise RpcHostError(
                code="event_stream_mismatch",
                message="event cursor belongs to another host stream",
                details={},
            )
        latest = self._host.events.latest_sequence
        if after_sequence > latest:
            raise RpcHostError(
                code="event_cursor_invalid",
                message="event cursor is ahead of the stream",
                details={"latest_sequence": latest},
            )
        try:
            window = self._host.events.snapshot(after_sequence=after_sequence)
        except EventCursorExpiredError as exc:
            raise RpcHostError(
                code="event_cursor_expired",
                message="event cursor is older than retained history",
                details={},
            ) from exc
        events: list[JsonObject] = []
        for event in window.events:
            projected = self.project_event(event)
            events.append(cast(JsonObject, json.loads(encode_v2_event(projected))))
        return {
            "stream_id": stream_id,
            "after_sequence": after_sequence,
            "oldest_sequence": window.oldest_sequence,
            "latest_sequence": window.latest_sequence,
            "events": events,
        }

    async def shutdown(self, params: JsonObject) -> JsonObject:
        return await self._host.shutdown(params)

    async def close(self) -> None:
        await self._host.close()


__all__ = ["HarnessRpcV2Host"]
