"""Known-good reference adapters for the interaction conformance kit (HIF-3).

These adapters implement the observable Confirmation v2 and Event Stream / RPC
v1 semantics frozen in the milestone CONTEXT.md sections 4 and 5 with minimal,
self-contained machinery.  The wire codec enforces the CONTEXT.md revision 2
invariants: duplicate JSON keys, non-finite numbers, empty identifiers, invalid
Event UUIDs, non-positive sequences, empty event types, non-UTC timestamps,
unknown versions, and non-object payloads are all rejected, and responses
encode the canonical five-key success/failure shape.

They never import production modules; Integration binds the approved production
components to the kit Protocols instead.  The kit drives time explicitly
(``advance_time``) so every scenario is deterministic and never depends on
real timing.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable

from .conformance import (
    AbortToken,
    BatchOutcome,
    ConflictError,
    ConfirmationRecord,
    ConfirmationRequest,
    ConfirmationResponse,
    DispatchedCall,
    ExecutedOperation,
    KIT_REDACT_SECRET,
    ProtocolViolationError,
    ReopenOutcome,
    ReplayResult,
    RequestOutcome,
    RespondOutcome,
    RpcErrorInfo,
    RpcEvent,
    RpcRequest,
    RpcResponse,
    RpcSubscriber,
    WireError,
    WireResult,
    to_json_safe,
)

KIT_EPOCH: datetime = datetime(2026, 1, 1, tzinfo=UTC)

_REQUEST_ENVELOPE_KEYS = frozenset({"request_id", "method", "params", "schema_version"})
_EVENT_ENVELOPE_KEYS = frozenset(
    {"event_id", "sequence", "type", "data", "run_id", "created_at", "schema_version"}
)
_RESPONSE_ENVELOPE_KEYS = frozenset({"request_id", "ok", "result", "error", "schema_version"})
_ERROR_OBJECT_KEYS = frozenset({"code", "message", "details"})
_RESPOND_ITEM_KEYS = frozenset({"request_id", "decision", "reason", "metadata"})
_V1_DECISIONS = ("approve", "deny", "cancelled")

# Exact v1 param contracts (CONTEXT.md revision 2): allowed keys, required keys.
_PARAM_CONTRACTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "initialize": (frozenset(), frozenset()),
    "runtime.status": (frozenset(), frozenset()),
    "run.start": (frozenset({"prompt"}), frozenset({"prompt"})),
    "run.abort": (frozenset(), frozenset()),
    "confirmation.list": (frozenset(), frozenset()),
    "confirmation.respond": (
        frozenset({"request_id", "decision", "reason", "metadata"}),
        frozenset({"request_id", "decision"}),
    ),
    "confirmation.respond_batch": (frozenset({"responses"}), frozenset({"responses"})),
    "events.replay": (frozenset({"after_sequence"}), frozenset({"after_sequence"})),
    "shutdown": (frozenset(), frozenset()),
}


class _MethodError(Exception):
    """Controlled method-level error with a stable code and safe message."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class _DuplicateKeyError(ValueError):
    """A JSON object repeated a key; the strict codec rejects it."""


class _NonFiniteError(ValueError):
    """A JSON constant like NaN/Infinity appeared; the strict codec rejects it."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise _DuplicateKeyError(f"duplicate JSON key {key!r}")
        obj[key] = value
    return obj


def _reject_constant(token: str) -> None:
    raise _NonFiniteError(f"non-finite number {token}")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _PendingRecord:
    __slots__ = ("request", "status", "runtime_id", "revision", "response", "deadline", "waiter")

    def __init__(
        self,
        request: ConfirmationRequest,
        runtime_id: str,
        deadline: datetime | None,
    ) -> None:
        self.request = request
        self.status = "pending"  # ConfirmationStatus
        self.runtime_id = runtime_id
        self.revision = 1
        self.response: ConfirmationResponse | None = None
        self.deadline = deadline
        self.waiter = asyncio.Event()


class ReferenceConfirmationAdapter:
    """Known-good Confirmation v2 adapter (deterministic, no real timing)."""

    def __init__(
        self, *, runtime_id: str = "runtime-kit", clock_start: datetime = KIT_EPOCH
    ) -> None:
        self._runtime_id = runtime_id
        self._clock = clock_start
        self._records: dict[str, _PendingRecord] = {}
        self._log: list[ExecutedOperation] = []
        self._closed = False

    # -- kit-driven clock --
    def advance_time(self, seconds: float) -> None:
        self._clock += timedelta(seconds=seconds)
        self._expire_due()

    def _deadline(self, request: ConfirmationRequest) -> datetime | None:
        if request.timeout_seconds is None:
            return None
        return self._clock + timedelta(seconds=request.timeout_seconds)

    def _expire_due(self) -> None:
        for record in list(self._records.values()):
            if (
                record.status == "pending"
                and record.deadline is not None
                and self._clock >= record.deadline
            ):
                self._resolve(record, "expired", self._automatic_deny(record.request.id))

    @staticmethod
    def _automatic_deny(request_id: str) -> ConfirmationResponse:
        return ConfirmationResponse(
            request_id=request_id,
            decision="deny",
            metadata={"automatic": True, "expired": True},
        )

    @staticmethod
    def _automatic_cancel(request_id: str) -> ConfirmationResponse:
        return ConfirmationResponse(
            request_id=request_id,
            decision="cancelled",
            metadata={"automatic": True, "aborted": True},
        )

    @staticmethod
    def _automatic_close(request_id: str) -> ConfirmationResponse:
        return ConfirmationResponse(
            request_id=request_id,
            decision="cancelled",
            metadata={"automatic": True, "closed": True},
        )

    def _resolve(
        self, record: _PendingRecord, status: str, response: ConfirmationResponse | None
    ) -> None:
        record.status = status
        record.response = response
        record.revision += 1
        record.waiter.set()

    # -- observable accessors --
    def pending(self) -> tuple[ConfirmationRecord, ...]:
        return tuple(
            ConfirmationRecord(
                request_id=record.request.id,
                status=record.status,
                runtime_id=record.runtime_id,
                revision=record.revision,
                response=record.response,
            )
            for record in self._records.values()
            if record.status == "pending"
        )

    def record(self, request_id: str) -> ConfirmationRecord | None:
        record = self._records.get(request_id)
        if record is None:
            return None
        return ConfirmationRecord(
            request_id=record.request.id,
            status=record.status,
            runtime_id=record.runtime_id,
            revision=record.revision,
            response=record.response,
        )

    def execution_log(self) -> tuple[ExecutedOperation, ...]:
        return tuple(self._log)

    # -- request lifecycle --
    async def request(
        self, request: ConfirmationRequest, *, abort: AbortToken | None = None
    ) -> RequestOutcome:
        if self._closed:
            # fail closed after graceful close: no record is created and no
            # waiter is left behind
            return RequestOutcome(
                request_id=request.id,
                status="cancelled",
                response=self._automatic_close(request.id),
                executed=False,
            )
        record = _PendingRecord(request, self._runtime_id, self._deadline(request))
        self._records[request.id] = record
        if abort is None:
            await record.waiter.wait()
        else:
            while not record.waiter.is_set() and not abort.triggered:
                await asyncio.sleep(0)
            if record.status == "pending":
                # the abort signal raced the waiter: resolve as cancelled
                self._resolve(record, "cancelled", self._automatic_cancel(request.id))
        return RequestOutcome(
            request_id=request.id,
            status=record.status,
            response=record.response,
            executed=(record.status == "approved"),
        )

    async def respond(self, response: ConfirmationResponse) -> RespondOutcome:
        record = self._records.get(response.request_id)
        if record is None:
            return RespondOutcome(
                request_id=response.request_id,
                ok=False,
                error=ConflictError(code="unknown_request_id", message="no pending request with that id"),
            )
        if record.status == "pending" and record.deadline is not None and self._clock >= record.deadline:
            self._expire_due()
        if record.status != "pending":
            return RespondOutcome(
                request_id=response.request_id,
                ok=False,
                error=ConflictError(
                    code=self._resolved_code(record), message=self._resolved_message(record)
                ),
            )
        if response.decision == "approve":
            self._log.append(ExecutedOperation(request_id=record.request.id, decision="approve"))
            self._resolve(record, "approved", response)
        elif response.decision == "deny":
            self._resolve(record, "denied", response)
        else:
            self._resolve(record, "cancelled", response)
        return RespondOutcome(request_id=response.request_id, ok=True, status_after=record.status)

    @staticmethod
    def _resolved_code(record: _PendingRecord) -> str:
        if record.status == "expired":
            return "request_expired"
        if record.status == "orphaned":
            return "request_orphaned"
        return "already_resolved"

    @staticmethod
    def _resolved_message(record: _PendingRecord) -> str:
        return f"request is already {record.status}"

    def _precheck(self, response: ConfirmationResponse) -> ConflictError | None:
        record = self._records.get(response.request_id)
        if record is None:
            return ConflictError(code="unknown_request_id", message="no pending request with that id")
        if record.status == "pending" and record.deadline is not None and self._clock >= record.deadline:
            self._expire_due()
        if record.status != "pending":
            return ConflictError(code=self._resolved_code(record), message=self._resolved_message(record))
        return None

    async def respond_batch(
        self, responses: tuple[ConfirmationResponse, ...]
    ) -> BatchOutcome:
        ids = [response.request_id for response in responses]
        if len(ids) != len(set(ids)):
            return BatchOutcome(
                ok=False, error=ConflictError(code="invalid_batch", message="duplicate request ids in batch")
            )
        # pre-validate every entry before applying anything: all or none
        for response in responses:
            error = self._precheck(response)
            if error is not None:
                return BatchOutcome(ok=False, applied=(), error=error)
        for response in responses:
            record = self._records[response.request_id]
            if response.decision == "approve":
                self._log.append(ExecutedOperation(request_id=response.request_id, decision="approve"))
                self._resolve(record, "approved", response)
            else:
                status = "denied" if response.decision == "deny" else "cancelled"
                self._resolve(record, status, response)
        return BatchOutcome(ok=True, applied=tuple(ids))

    async def abort(self, request_id: str) -> RespondOutcome:
        record = self._records.get(request_id)
        if record is None:
            return RespondOutcome(
                request_id=request_id,
                ok=False,
                error=ConflictError(code="unknown_request_id", message="no pending request with that id"),
            )
        if record.status != "pending":
            return RespondOutcome(
                request_id=request_id,
                ok=False,
                error=ConflictError(
                    code=self._resolved_code(record), message=self._resolved_message(record)
                ),
            )
        self._resolve(record, "cancelled", self._automatic_cancel(request_id))
        return RespondOutcome(request_id=request_id, ok=True, status_after="cancelled")

    # -- process boundary --
    async def close(self) -> None:
        # Graceful close cancels the live pending waits, wakes callers fail
        # closed, and leaves no pending record.  It is not a crash: the durable
        # facts are resolved, not orphaned, and a host reconnect keeps using the
        # same live adapter.
        self._closed = True
        for record in list(self._records.values()):
            if record.status == "pending":
                self._resolve(record, "cancelled", self._automatic_close(record.request.id))

    async def crash(self) -> None:
        # Test-only abrupt process loss boundary: the durable pending facts
        # survive untouched and the live waiters are abandoned (never resolved).
        # No cleanup runs, exactly like a process dying mid-wait.
        return None

    async def recover(self, *, runtime_id: str) -> ReopenOutcome:
        # Process recovery reopens the same durable store: every persisted
        # pending record transitions to orphaned, even when the caller reuses
        # the old runtime id.  No waiter or tool is reconstructed.
        orphaned: list[ConfirmationRecord] = []
        for record in list(self._records.values()):
            if record.status == "pending":
                record.status = "orphaned"
                record.revision += 1
                orphaned.append(
                    ConfirmationRecord(
                        request_id=record.request.id,
                        status="orphaned",
                        runtime_id=record.runtime_id,
                        revision=record.revision,
                        response=None,
                    )
                )
        self._runtime_id = runtime_id
        return ReopenOutcome(orphaned=tuple(orphaned))


class _ReferenceSubscriber:
    """Bounded-queue subscriber: overflows fail explicitly, never block."""

    def __init__(self, retained: list[RpcEvent], max_queue: int) -> None:
        self._queue: list[RpcEvent] = list(retained)
        self._max_queue = max_queue
        self._failure: WireError | None = None
        self._closed = False
        self._wake = asyncio.Event()

    def failure(self) -> WireError | None:
        return self._failure

    def mark_failed(self, error: WireError) -> None:
        if self._failure is None:
            self._failure = error
            self._wake.set()

    async def next_event(self) -> RpcEvent | None:
        while True:
            if self._queue:
                return self._queue.pop(0)
            if self._failure is not None or self._closed:
                return None
            self._wake.clear()
            await self._wake.wait()

    async def close(self) -> None:
        self._closed = True
        self._wake.set()

    def append_live(self, event: RpcEvent) -> bool:
        """Append a live event; return False when the bounded queue is full."""
        if self._closed or self._failure is not None:
            return False
        if len(self._queue) >= self._max_queue:
            self.mark_failed(
                WireError(code="subscriber_queue_overflow", message="subscriber too slow; dropped")
            )
            return False
        self._queue.append(event)
        self._wake.set()
        return True


class ReferenceRpcAdapter:
    """Known-good Event Stream / RPC v1 adapter (strict codec, bounded queues)."""

    def __init__(self, *, retained_capacity: int = 8) -> None:
        self.retained_capacity = retained_capacity
        self._sequence = 0
        self._retained: list[RpcEvent] = []
        self._subscribers: list[_ReferenceSubscriber] = []
        self._active_run: str | None = None
        self._run_counter = 0
        self._seen_request_ids: set[str] = set()
        self._dispatched: list[DispatchedCall] = []
        self._armed_failures: set[str] = set()
        self._closed = False
        self._methods: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
            "initialize": self._h_initialize,
            "runtime.status": self._h_runtime_status,
            "run.start": self._h_run_start,
            "run.abort": self._h_run_abort,
            "events.replay": self._h_events_replay,
            "confirmation.list": self._h_confirmation_list,
            "confirmation.respond": self._h_confirmation_respond,
            "confirmation.respond_batch": self._h_confirmation_respond_batch,
            "shutdown": self._h_shutdown,
        }

    # -- handlers --
    async def _h_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"protocol": "evopi.rpc.v1", "schema_version": 1}

    async def _h_runtime_status(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"active_run_id": self._active_run}

    async def _h_run_start(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._active_run is not None:
            raise _MethodError("run_already_active", "another run is already active")
        # the Host assigns the run id; the caller never chooses it
        self._run_counter += 1
        run_id = f"run-{self._run_counter}"
        self._active_run = run_id
        return {"run_id": run_id}

    async def _h_run_abort(self, params: dict[str, Any]) -> dict[str, Any]:
        self._active_run = None
        return {"aborted": True}

    async def _h_events_replay(self, params: dict[str, Any]) -> dict[str, Any]:
        result = self.replay(after_sequence=params["after_sequence"])
        if not result.ok:
            assert result.error is not None
            raise _MethodError(result.error.code, result.error.message)
        return {"events": [self._event_payload(event) for event in result.events]}

    async def _h_confirmation_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"pending": []}

    async def _h_confirmation_respond(self, params: dict[str, Any]) -> dict[str, Any]:
        # The reference server hosts no pending confirmations, so every
        # response is a structured conflict; correlation semantics live in the
        # Confirmation kit.
        raise _MethodError("no_matching_pending_request", "no pending request matches")

    async def _h_confirmation_respond_batch(self, params: dict[str, Any]) -> dict[str, Any]:
        raise _MethodError("no_matching_pending_request", "no pending request matches")

    async def _h_shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        await self.close()
        return {"closed": True}

    # -- test-only controls --
    def arm_failure(self, method: str) -> None:
        """Make one fixed v1 Host method raise unexpectedly on its next dispatch."""
        self._armed_failures.add(method)

    # -- wire payloads --
    def _event_payload(self, event: RpcEvent) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "sequence": event.sequence,
            "type": event.type,
            "data": to_json_safe(event.data),
            "run_id": event.run_id,
            "created_at": event.created_at.isoformat().replace("+00:00", "Z"),
            "schema_version": event.schema_version,
        }

    @staticmethod
    def _conversion_error(exc: ValueError) -> WireError:
        message = str(exc)
        if "non-finite" in message:
            return WireError(code="non_finite_number", message=message)
        return WireError(code="unsupported_value", message=message)

    def _strict_loads(self, line: str) -> dict[str, Any] | WireError:
        try:
            obj = json.loads(line, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except _DuplicateKeyError as exc:
            return WireError(code="duplicate_json_key", message=str(exc))
        except _NonFiniteError as exc:
            return WireError(code="non_finite_number", message=str(exc))
        except json.JSONDecodeError:
            return WireError(code="malformed_json", message="invalid JSON")
        if not isinstance(obj, dict):
            return WireError(code="invalid_envelope", message="envelope must be a JSON object")
        return obj

    def _validate_params(self, method: str, params: dict[str, Any]) -> WireError | None:
        contract = _PARAM_CONTRACTS.get(method)
        if contract is None:
            return None  # unknown method: params already object-checked
        allowed, required = contract
        unknown = set(params) - allowed
        if unknown:
            return WireError(code="invalid_params", message=f"unknown param key: {sorted(unknown)[0]}")
        missing = required - set(params)
        if missing:
            return WireError(code="invalid_params", message=f"missing required param: {sorted(missing)[0]}")
        if "prompt" in params and not isinstance(params["prompt"], str):
            return WireError(code="invalid_params", message="param 'prompt' must be a string")
        if "after_sequence" in params:
            value = params["after_sequence"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return WireError(code="invalid_params", message="param 'after_sequence' must be a non-negative integer")
        if "request_id" in params and (not isinstance(params["request_id"], str) or not params["request_id"]):
            return WireError(code="invalid_params", message="param 'request_id' must be a non-empty string")
        if "decision" in params and params["decision"] not in _V1_DECISIONS:
            return WireError(code="invalid_params", message="param 'decision' must be approve, deny, or cancelled")
        if "reason" in params and not isinstance(params["reason"], str):
            return WireError(code="invalid_params", message="param 'reason' must be a string")
        if "metadata" in params and (isinstance(params["metadata"], bool) or not isinstance(params["metadata"], dict)):
            return WireError(code="invalid_params", message="param 'metadata' must be an object")
        if "responses" in params:
            responses = params["responses"]
            if isinstance(responses, bool) or not isinstance(responses, list):
                return WireError(code="invalid_params", message="param 'responses' must be a list")
            for item in responses:
                error = self._validate_respond_item(item)
                if error is not None:
                    return error
        return None

    def _validate_respond_item(self, item: Any) -> WireError | None:
        if isinstance(item, bool) or not isinstance(item, dict):
            return WireError(code="invalid_params", message="each response must be an object")
        unknown = set(item) - _RESPOND_ITEM_KEYS
        if unknown:
            return WireError(code="invalid_params", message=f"unknown response key: {sorted(unknown)[0]}")
        if "request_id" not in item or "decision" not in item:
            return WireError(code="invalid_params", message="each response needs request_id and decision")
        if not isinstance(item["request_id"], str) or not item["request_id"]:
            return WireError(code="invalid_params", message="response request_id must be a non-empty string")
        if item["decision"] not in _V1_DECISIONS:
            return WireError(code="invalid_params", message="response decision must be approve, deny, or cancelled")
        if "reason" in item and not isinstance(item["reason"], str):
            return WireError(code="invalid_params", message="response reason must be a string")
        if "metadata" in item and (isinstance(item["metadata"], bool) or not isinstance(item["metadata"], dict)):
            return WireError(code="invalid_params", message="response metadata must be an object")
        return None

    def _decode_request(self, line: str) -> RpcRequest | WireError:
        loaded = self._strict_loads(line)
        if isinstance(loaded, WireError):
            return loaded
        unknown = set(loaded) - _REQUEST_ENVELOPE_KEYS
        if unknown:
            return WireError(code="invalid_envelope_key", message=f"unknown envelope key: {sorted(unknown)[0]}")
        missing = _REQUEST_ENVELOPE_KEYS - set(loaded)
        if missing:
            return WireError(code="invalid_envelope", message=f"missing envelope key: {sorted(missing)[0]}")
        request_id = loaded.get("request_id")
        method = loaded.get("method")
        if (
            not isinstance(request_id, str)
            or not request_id
            or not isinstance(method, str)
            or not method
        ):
            return WireError(code="invalid_envelope", message="request_id and method must be non-empty strings")
        schema_version = loaded.get("schema_version", 1)
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != 1
        ):
            return WireError(code="invalid_schema_version", message="unsupported schema version")
        params = loaded.get("params")
        if isinstance(params, bool) or not isinstance(params, dict):
            return WireError(code="invalid_params", message="params must be a JSON object")
        error = self._validate_params(method, params)
        if error is not None:
            return error
        return RpcRequest(
            request_id=request_id, method=method, params=params, schema_version=1
        )

    @staticmethod
    def _is_uuid(value: str) -> bool:
        try:
            return str(uuid.UUID(value)) == value
        except (ValueError, AttributeError, TypeError):
            return False

    @staticmethod
    def _parse_utc_timestamp(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            return None
        return parsed

    @staticmethod
    def _is_utc(value: datetime) -> bool:
        return value.tzinfo is not None and value.utcoffset() == timedelta(0)

    def _decode_event(self, line: str) -> RpcEvent | WireError:
        loaded = self._strict_loads(line)
        if isinstance(loaded, WireError):
            return loaded
        unknown = set(loaded) - _EVENT_ENVELOPE_KEYS
        if unknown:
            return WireError(code="invalid_envelope_key", message=f"unknown envelope key: {sorted(unknown)[0]}")
        missing = _EVENT_ENVELOPE_KEYS - set(loaded)
        if missing:
            return WireError(code="invalid_envelope", message=f"missing envelope key: {sorted(missing)[0]}")
        created_at = self._parse_utc_timestamp(loaded.get("created_at"))
        if created_at is None:
            return WireError(code="invalid_timestamp", message="created_at must be a UTC timestamp")
        data = loaded.get("data")
        if isinstance(data, bool) or not isinstance(data, dict):
            return WireError(code="invalid_data", message="data must be a JSON object")
        sequence = loaded.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            return WireError(code="invalid_event", message="sequence must be a positive integer")
        event_id = loaded.get("event_id")
        type_ = loaded.get("type")
        if not isinstance(event_id, str) or not self._is_uuid(event_id):
            return WireError(code="invalid_event", message="event_id must be a UUID string")
        if not isinstance(type_, str) or not type_:
            return WireError(code="invalid_event", message="type must be a non-empty string")
        run_id = loaded.get("run_id")
        if run_id is not None and not isinstance(run_id, str):
            return WireError(code="invalid_event", message="run_id must be a string or null")
        schema_version = loaded.get("schema_version", 1)
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != 1
        ):
            return WireError(code="invalid_schema_version", message="unsupported schema version")
        return RpcEvent(
            event_id=event_id,
            sequence=sequence,
            type=type_,
            data=data,
            run_id=run_id,
            created_at=created_at,
            schema_version=schema_version,
        )

    def _decode_response(self, line: str) -> RpcResponse | WireError:
        loaded = self._strict_loads(line)
        if isinstance(loaded, WireError):
            return loaded
        unknown = set(loaded) - _RESPONSE_ENVELOPE_KEYS
        if unknown:
            return WireError(code="invalid_envelope_key", message=f"unknown envelope key: {sorted(unknown)[0]}")
        missing = _RESPONSE_ENVELOPE_KEYS - set(loaded)
        if missing:
            return WireError(code="invalid_envelope", message=f"missing envelope key: {sorted(missing)[0]}")
        request_id = loaded.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return WireError(code="invalid_response", message="request_id must be a non-empty string")
        ok = loaded.get("ok")
        if not isinstance(ok, bool):
            return WireError(code="invalid_response", message="ok must be a boolean")
        schema_version = loaded.get("schema_version", 1)
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != 1
        ):
            return WireError(code="invalid_schema_version", message="unsupported schema version")
        result = loaded.get("result")
        error = loaded.get("error")
        if ok:
            if isinstance(result, bool) or not isinstance(result, dict):
                return WireError(code="invalid_response", message="a success must carry an object result")
            if error is not None:
                return WireError(code="invalid_response", message="a success must have error null")
            return RpcResponse(request_id=request_id, ok=True, result=result, error=None, schema_version=1)
        if result is not None:
            return WireError(code="invalid_response", message="a failure must have result null")
        if isinstance(error, bool) or not isinstance(error, dict):
            return WireError(code="invalid_response", message="a failure must carry one exact error object")
        unknown_error = set(error) - _ERROR_OBJECT_KEYS
        if unknown_error:
            return WireError(code="invalid_response", message=f"unknown error key: {sorted(unknown_error)[0]}")
        missing_error = _ERROR_OBJECT_KEYS - set(error)
        if missing_error:
            return WireError(code="invalid_response", message=f"missing error key: {sorted(missing_error)[0]}")
        code = error.get("code")
        message = error.get("message")
        if not isinstance(code, str) or not code or not isinstance(message, str) or not message:
            return WireError(code="invalid_response", message="error code and message must be non-empty strings")
        details = error.get("details")
        if isinstance(details, bool) or not isinstance(details, dict):
            return WireError(code="invalid_response", message="error details must be an object")
        return RpcResponse(
            request_id=request_id,
            ok=False,
            result=None,
            error=RpcErrorInfo(code=code, message=message, details=details),
            schema_version=1,
        )

    # -- event stream --
    async def publish(self, type_: str, data: dict[str, Any]) -> RpcEvent:
        if self._closed:
            raise ProtocolViolationError(WireError(code="closed", message="stream is closed"))
        if not isinstance(type_, str) or not type_:
            raise ProtocolViolationError(
                WireError(code="invalid_event", message="type must be a non-empty string")
            )
        try:
            safe_data = to_json_safe(data)
        except ValueError as exc:
            raise ProtocolViolationError(self._conversion_error(exc)) from None
        self._sequence += 1
        event = RpcEvent(
            event_id=str(uuid.uuid4()),
            sequence=self._sequence,
            type=type_,
            data=safe_data,
            run_id=None,
            created_at=_utc_now(),
            schema_version=1,
        )
        self._retained.append(event)
        while len(self._retained) > self.retained_capacity:
            self._retained.pop(0)
        for subscriber in list(self._subscribers):
            subscriber.append_live(event)  # bounded and non-blocking
        return event

    def replay(self, *, after_sequence: int) -> ReplayResult:
        first_retained = self._retained[0].sequence if self._retained else self._sequence + 1
        if after_sequence < 0 or after_sequence < first_retained - 1:
            return ReplayResult(
                ok=False, error=WireError(code="event_cursor_expired", message="cursor older than retained history")
            )
        return ReplayResult(
            ok=True,
            events=tuple(event for event in self._retained if event.sequence > after_sequence),
        )

    def _retained_for_subscriber(self, after_sequence: int) -> list[RpcEvent]:
        return [event for event in self._retained if event.sequence > after_sequence]

    async def subscribe(self, *, after_sequence: int, max_queue: int = 64) -> RpcSubscriber:
        first_retained = self._retained[0].sequence if self._retained else self._sequence + 1
        if after_sequence < 0 or after_sequence < first_retained - 1:
            raise ProtocolViolationError(
                WireError(code="event_cursor_expired", message="cursor older than retained history")
            )
        subscriber = _ReferenceSubscriber(self._retained_for_subscriber(after_sequence), max_queue)
        self._subscribers.append(subscriber)
        return subscriber

    # -- server dispatch --
    async def call(self, request: RpcRequest) -> RpcResponse:
        if isinstance(request.schema_version, bool) or request.schema_version != 1:
            return RpcResponse(
                request_id=request.request_id,
                ok=False,
                error=RpcErrorInfo(code="invalid_schema_version", message="unsupported schema version"),
            )
        if request.request_id in self._seen_request_ids:
            return RpcResponse(
                request_id=request.request_id,
                ok=False,
                error=RpcErrorInfo(code="duplicate_request", message="request id already used"),
            )
        self._seen_request_ids.add(request.request_id)
        return await self._dispatch(request.request_id, request.method, request.params)

    async def _dispatch(
        self, request_id: str, method: str, params: dict[str, Any]
    ) -> RpcResponse:
        handler = self._methods.get(method)
        if handler is None:
            return RpcResponse(
                request_id=request_id,
                ok=False,
                error=RpcErrorInfo(code="method_not_found", message=f"unknown method: {method}"),
            )
        self._dispatched.append(
            DispatchedCall(request_id=request_id, method=method, params=to_json_safe(params))
        )
        try:
            if method in self._armed_failures:
                raise RuntimeError(f"boom: token={KIT_REDACT_SECRET!r}")
            result = await handler(params)
        except _MethodError as error:
            return RpcResponse(
                request_id=request_id,
                ok=False,
                error=RpcErrorInfo(
                    code=error.code, message=error.message, details=error.details or {}
                ),
            )
        except Exception as exc:
            return RpcResponse(request_id=request_id, ok=False, error=self._format_unexpected(exc))
        return RpcResponse(request_id=request_id, ok=True, result=result)

    @staticmethod
    def _format_unexpected(exc: Exception) -> RpcErrorInfo:
        # Never leak tracebacks, prompts, tool arguments, or provider state.
        return RpcErrorInfo(code="internal_error", message="internal error")

    # -- wire surface --
    async def send_wire(self, line: str) -> WireResult:
        decoded = self._decode_request(line)
        if isinstance(decoded, WireError):
            return WireResult(ok=False, error=decoded)
        response = await self.call(decoded)
        if response.ok:
            return WireResult(ok=True, response=response)
        error = response.error
        wire_error = (
            WireError(code=error.code, message=error.message, details=error.details)
            if error is not None
            else WireError(code="internal_error", message="internal error")
        )
        return WireResult(ok=False, response=response, error=wire_error)

    def event_wire(self, event: RpcEvent) -> str | WireError:
        if not isinstance(event.event_id, str) or not self._is_uuid(event.event_id):
            return WireError(code="invalid_event", message="event_id must be a UUID string")
        if isinstance(event.sequence, bool) or not isinstance(event.sequence, int) or event.sequence < 1:
            return WireError(code="invalid_event", message="sequence must be a positive integer")
        if not isinstance(event.type, str) or not event.type:
            return WireError(code="invalid_event", message="type must be a non-empty string")
        if not self._is_utc(event.created_at):
            return WireError(code="invalid_timestamp", message="created_at must be a UTC timestamp")
        if isinstance(event.data, bool) or not isinstance(event.data, dict):
            return WireError(code="invalid_data", message="data must be a JSON object")
        try:
            payload = self._event_payload(event)
            return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        except ValueError as exc:
            return self._conversion_error(exc)

    def parse_wire_event(self, line: str) -> RpcEvent | WireError:
        return self._decode_event(line)

    def response_wire(self, response: RpcResponse) -> str | WireError:
        if (
            isinstance(response.schema_version, bool)
            or not isinstance(response.schema_version, int)
            or response.schema_version != 1
        ):
            return WireError(code="invalid_schema_version", message="unsupported schema version")
        if not isinstance(response.request_id, str) or not response.request_id:
            return WireError(code="invalid_response", message="request_id must be a non-empty string")
        if response.ok:
            if isinstance(response.result, bool) or not isinstance(response.result, dict):
                return WireError(code="invalid_response", message="a success must carry an object result")
            if response.error is not None:
                return WireError(code="invalid_response", message="a success must have error null")
            try:
                payload = {
                    "request_id": response.request_id,
                    "ok": True,
                    "result": to_json_safe(response.result),
                    "error": None,
                    "schema_version": 1,
                }
                return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            except ValueError as exc:
                return self._conversion_error(exc)
        if response.result is not None:
            return WireError(code="invalid_response", message="a failure must have result null")
        if response.error is None:
            return WireError(code="invalid_response", message="a failure must carry one exact error object")
        error = response.error
        if (
            not isinstance(error.code, str)
            or not error.code
            or not isinstance(error.message, str)
            or not error.message
        ):
            return WireError(code="invalid_response", message="error code and message must be non-empty strings")
        if isinstance(error.details, bool) or not isinstance(error.details, dict):
            return WireError(code="invalid_response", message="error details must be an object")
        try:
            payload = {
                "request_id": response.request_id,
                "ok": False,
                "result": None,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "details": to_json_safe(error.details),
                },
                "schema_version": 1,
            }
            return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        except ValueError as exc:
            return self._conversion_error(exc)

    def parse_wire_response(self, line: str) -> RpcResponse | WireError:
        return self._decode_response(line)

    def dispatched(self) -> tuple[DispatchedCall, ...]:
        return tuple(self._dispatched)

    async def close(self) -> None:
        self._closed = True
        for subscriber in list(self._subscribers):
            await subscriber.close()
