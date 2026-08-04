"""Independent host-interaction conformance kit (HIF-3).

This module defines the observable-behavior vocabulary for the Confirmation v2
and Event Stream / RPC v1 contracts frozen in the milestone CONTEXT.md sections
4 and 5: narrow adapter Protocols, result dataclasses, deterministic synthetic
fixture builders, and reusable async scenario functions.

The kit is production-independent: it never imports any production module, it
describes only observable behavior (no private fields, no implementation class
names), and Integration binds the approved production components to these
Protocols in a separate integration-owned test file.  Mutant adapters in
``mutants.py`` deliberately break one behavior each so the validity tests can
prove every scenario detects the intended defect.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias

JsonLike: TypeAlias = dict[str, Any] | list[Any] | str | int | float | bool | None

# ---------------------------------------------------------------------------
# Confirmation v2 observable vocabulary (CONTEXT section 4)
# ---------------------------------------------------------------------------

ConfirmationStatus: TypeAlias = Literal[
    "pending", "approved", "denied", "cancelled", "expired", "orphaned"
]
ConfirmationDecision: TypeAlias = Literal["approve", "deny", "cancelled"]


@dataclass(slots=True, frozen=True, kw_only=True)
class ConfirmationRequest:
    """Synthetic request shape the kit hands to an adapter."""

    id: str
    hook: str = "pre_tool_execution"
    reason: str = "synthetic confirmation fixture"
    risk_level: str = "high"
    policy_names: tuple[str, ...] = ()
    arguments: JsonLike | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float | None = None
    run_id: str | None = None
    session_id: str | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class ConfirmationResponse:
    """A human decision correlated to one confirmation request."""

    request_id: str
    decision: ConfirmationDecision
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True, kw_only=True)
class ConfirmationRecord:
    """Observable record of one confirmation lifecycle."""

    request_id: str
    status: ConfirmationStatus
    runtime_id: str
    revision: int
    response: ConfirmationResponse | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class RequestOutcome:
    """Terminal outcome of one ``request`` wait."""

    request_id: str
    status: ConfirmationStatus
    response: ConfirmationResponse | None
    executed: bool  # whether the guarded operation ran


@dataclass(slots=True, frozen=True, kw_only=True)
class RespondOutcome:
    request_id: str
    ok: bool
    status_after: ConfirmationStatus | None = None
    error: ConflictError | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class BatchOutcome:
    ok: bool
    applied: tuple[str, ...] = ()
    error: ConflictError | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class ReopenOutcome:
    orphaned: tuple[ConfirmationRecord, ...] = ()


@dataclass(slots=True, frozen=True, kw_only=True)
class ConflictError:
    """Structured, JSON-safe rejection for a confirmation operation."""

    code: str
    message: str
    details: JsonLike | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class ExecutedOperation:
    """Observable side effect of one executed guarded operation."""

    request_id: str
    decision: str


class AbortToken:
    """Client-side abort signal the scenario can trigger deterministically."""

    def __init__(self) -> None:
        self._triggered = False

    def trigger(self) -> None:
        self._triggered = True

    @property
    def triggered(self) -> bool:
        return self._triggered


class ConfirmationAdapter(Protocol):
    """Observable Confirmation v2 behavior an implementation must provide.

    Only behavior visible from the outside is described: no private fields, no
    implementation class names, no production imports.  Time is advanced by the
    kit so every scenario stays deterministic and never needs timing sleeps.
    """

    def advance_time(self, seconds: float) -> None: ...

    def pending(self) -> tuple[ConfirmationRecord, ...]: ...

    def execution_log(self) -> tuple[ExecutedOperation, ...]: ...

    async def request(
        self, request: ConfirmationRequest, *, abort: AbortToken | None = None
    ) -> RequestOutcome: ...

    async def respond(self, response: ConfirmationResponse) -> RespondOutcome: ...

    async def respond_batch(
        self, responses: tuple[ConfirmationResponse, ...]
    ) -> BatchOutcome: ...

    async def abort(self, request_id: str) -> RespondOutcome: ...

    async def close(self) -> None: ...

    async def reopen(self, *, runtime_id: str) -> ReopenOutcome: ...


# ---------------------------------------------------------------------------
# Event Stream / RPC v1 observable vocabulary (CONTEXT section 5)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True, kw_only=True)
class RpcRequest:
    request_id: str
    method: str
    params: dict[str, Any]
    schema_version: int = 1


@dataclass(slots=True, frozen=True, kw_only=True)
class RpcErrorInfo:
    code: str
    message: str
    details: JsonLike | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class RpcResponse:
    request_id: str
    ok: bool
    result: JsonLike | None = None
    error: RpcErrorInfo | None = None
    schema_version: int = 1


@dataclass(slots=True, frozen=True, kw_only=True)
class RpcEvent:
    event_id: str
    sequence: int
    type: str
    data: dict[str, Any]
    run_id: str | None
    created_at: datetime
    schema_version: int = 1


@dataclass(slots=True, frozen=True, kw_only=True)
class WireError:
    """Structured, JSON-safe rejection from the strict wire codec."""

    code: str
    message: str
    details: JsonLike | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class WireResult:
    ok: bool
    response: RpcResponse | None = None
    error: WireError | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class ReplayResult:
    ok: bool
    events: tuple[RpcEvent, ...] = ()
    error: WireError | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class DispatchedCall:
    """Observable record that a request id was dispatched to a handler."""

    request_id: str
    method: str
    params: dict[str, Any]


class ProtocolViolationError(Exception):
    """An adapter violated the RPC protocol (carries a structured WireError)."""

    def __init__(self, error: WireError) -> None:
        super().__init__(error.message)
        self.error = error


class RpcSubscriber(Protocol):
    async def next_event(self) -> RpcEvent | None: ...
    def failure(self) -> WireError | None: ...
    async def close(self) -> None: ...


class RpcAdapter(Protocol):
    """Observable Event Stream / RPC v1 behavior an implementation must provide."""

    retained_capacity: int

    async def publish(self, type_: str, data: dict[str, Any]) -> RpcEvent: ...
    def replay(self, *, after_sequence: int) -> ReplayResult: ...
    async def subscribe(
        self, *, after_sequence: int, max_queue: int = 64
    ) -> RpcSubscriber: ...
    async def call(self, request: RpcRequest) -> RpcResponse: ...
    async def send_wire(self, line: str) -> WireResult: ...
    def event_wire(self, event: RpcEvent) -> str | WireError: ...
    def parse_wire_event(self, line: str) -> RpcEvent | WireError: ...
    def dispatched(self) -> tuple[DispatchedCall, ...]: ...
    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Deterministic synthetic fixtures
# ---------------------------------------------------------------------------

FIXED_TS: datetime = datetime(2026, 1, 1, tzinfo=UTC)

KIT_REDACT_SECRET: str = "kit-redact-secret"
KIT_REDACT_COMMAND: str = "echo kit-redact-command"


def make_confirmation_request(
    *,
    request_id: str,
    timeout_seconds: float | None = None,
    secret: str = "kit-secret-value",
    command: str = "echo kit-fixture",
    **overrides: Any,
) -> ConfirmationRequest:
    """Build a synthetic, deterministic confirmation request.

    Every value is synthetic; no .env, real Trace, real Session, or user data
    is read.  The same arguments always produce the identical request.
    """
    base: dict[str, Any] = {
        "id": request_id,
        "hook": "pre_tool_execution",
        "reason": "synthetic confirmation fixture",
        "risk_level": "high",
        "policy_names": ("kit-test-policy",),
        "arguments": {
            "command": command,
            "path": "synthetic:/kit/path",
            "secret": secret,
        },
        "metadata": {"synthetic": True, "fixture": "deterministic"},
        "timeout_seconds": timeout_seconds,
        "run_id": "run-kit",
        "session_id": "session-kit",
    }
    base.update(overrides)
    return ConfirmationRequest(**base)


def make_response(
    *, request_id: str, decision: ConfirmationDecision = "approve", **overrides: Any
) -> ConfirmationResponse:
    base: dict[str, Any] = {"request_id": request_id, "decision": decision}
    base.update(overrides)
    return ConfirmationResponse(**base)


def make_event(
    *,
    event_id: str = "ev-1",
    sequence: int = 1,
    type_: str = "tool_execution_start",
    data: dict[str, Any] | None = None,
    run_id: str | None = "run-kit",
    created_at: datetime = FIXED_TS,
    schema_version: int = 1,
) -> RpcEvent:
    return RpcEvent(
        event_id=event_id,
        sequence=sequence,
        type=type_,
        data={"n": 1} if data is None else data,
        run_id=run_id,
        created_at=created_at,
        schema_version=schema_version,
    )


def make_rpc_request(
    *,
    request_id: str,
    method: str,
    params: dict[str, Any] | None = None,
    schema_version: int = 1,
) -> RpcRequest:
    return RpcRequest(
        request_id=request_id,
        method=method,
        params={} if params is None else params,
        schema_version=schema_version,
    )


def to_json_safe(value: Any) -> JsonLike:
    """Convert kit values to JSON-safe equivalents; reject everything else.

    Accepts JSON primitives, mappings, sequences, dataclasses, enums, Path, and
    datetime/date.  Any other value raises ValueError; there is never a ``repr``
    fallback.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value if isinstance(value.value, (str, int, float, bool)) else str(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): to_json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_json_safe(item) for item in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: to_json_safe(getattr(value, item.name))
            for item in dataclasses.fields(value)
        }
    raise ValueError(f"unsupported value of type {type(value).__name__}")


# ---------------------------------------------------------------------------
# Scenario helpers
# ---------------------------------------------------------------------------


class ConformanceFailure(AssertionError):
    """A conformance scenario observed a violation of the frozen contract."""


async def _await_bounded(awaitable: Awaitable[Any], *, what: str, seconds: float = 5.0) -> Any:
    """Await with a safety bound so a hung adapter fails the scenario fast."""
    try:
        return await asyncio.wait_for(awaitable, timeout=seconds)
    except asyncio.TimeoutError:
        raise ConformanceFailure(
            f"{what} hung; expected completion within {seconds:g}s"
        ) from None


# ---------------------------------------------------------------------------
# Confirmation scenarios
# ---------------------------------------------------------------------------


async def run_timeout_no_execution(adapter: ConfirmationAdapter) -> None:
    """Timeout expires the request as an automatic denial; nothing executes."""
    request = make_confirmation_request(request_id="timeout-1", timeout_seconds=5.0)
    task = asyncio.create_task(adapter.request(request))
    await asyncio.sleep(0)
    adapter.advance_time(5.0)
    outcome = await _await_bounded(task, what="timed-out request")
    if outcome.status != "expired":
        raise ConformanceFailure(
            f"timeout must yield status 'expired', got {outcome.status!r}"
        )
    if outcome.executed:
        raise ConformanceFailure("timed-out request must never execute")
    if outcome.response is None or outcome.response.decision != "deny":
        raise ConformanceFailure("timeout must return an automatic deny response")
    metadata = outcome.response.metadata
    if metadata.get("automatic") is not True or metadata.get("expired") is not True:
        raise ConformanceFailure(
            "timeout denial must carry automatic=true and expired=true metadata"
        )
    if adapter.execution_log():
        raise ConformanceFailure("timeout still executed the guarded operation")


async def run_stale_expired_rejection(adapter: ConfirmationAdapter) -> None:
    """A response arriving after expiry is rejected and never executes."""
    request = make_confirmation_request(request_id="stale-1", timeout_seconds=5.0)
    task = asyncio.create_task(adapter.request(request))
    await asyncio.sleep(0)
    adapter.advance_time(6.0)
    outcome = await _await_bounded(task, what="request that expired while waiting")
    if outcome.executed:
        raise ConformanceFailure("expired request must never execute")
    result = await adapter.respond(make_response(request_id="stale-1", decision="approve"))
    if result.ok:
        raise ConformanceFailure("expired request accepted a late response")
    if adapter.execution_log():
        raise ConformanceFailure("late response caused execution")


async def run_abort_precedence(adapter: ConfirmationAdapter) -> None:
    """Abort beats timeout; an already-committed response beats a later abort."""
    # A. an abort wins over an otherwise-pending wait
    request_a = make_confirmation_request(request_id="abort-a", timeout_seconds=30.0)
    token = AbortToken()
    task_a = asyncio.create_task(adapter.request(request_a, abort=token))
    await asyncio.sleep(0)
    token.trigger()
    outcome_a = await _await_bounded(task_a, what="aborted request")
    if outcome_a.status != "cancelled" or outcome_a.executed:
        raise ConformanceFailure("abort must yield cancelled with no execution")
    metadata_a = outcome_a.response.metadata if outcome_a.response else {}
    if metadata_a.get("automatic") is not True or metadata_a.get("aborted") is not True:
        raise ConformanceFailure(
            "abort must carry automatic=true and aborted=true metadata"
        )
    late = await adapter.respond(make_response(request_id="abort-a", decision="approve"))
    if late.ok:
        raise ConformanceFailure("response after abort had an effect")
    # B. an already-committed response beats a later abort
    request_b = make_confirmation_request(request_id="abort-b")
    task_b = asyncio.create_task(adapter.request(request_b))
    await asyncio.sleep(0)
    commit = await adapter.respond(make_response(request_id="abort-b", decision="approve"))
    if not commit.ok:
        raise ConformanceFailure("approve should commit the pending request")
    outcome_b = await _await_bounded(task_b, what="committed request")
    if outcome_b.status != "approved" or not outcome_b.executed:
        raise ConformanceFailure("committed approval must execute")
    later_abort = await adapter.abort("abort-b")
    if later_abort.ok:
        raise ConformanceFailure("abort after commit must not override the response")
    # C. abort beats timeout even when the deadline passes afterwards
    request_c = make_confirmation_request(request_id="abort-c", timeout_seconds=5.0)
    task_c = asyncio.create_task(adapter.request(request_c))
    await asyncio.sleep(0)
    early_abort = await adapter.abort("abort-c")
    if not early_abort.ok:
        raise ConformanceFailure("abort of a pending request should succeed")
    adapter.advance_time(60.0)
    outcome_c = await _await_bounded(task_c, what="abort-before-timeout request")
    if outcome_c.status != "cancelled":
        raise ConformanceFailure("timeout must not override an earlier abort")


async def run_duplicate_response(adapter: ConfirmationAdapter) -> None:
    """A duplicate response is rejected and causes no second effect."""
    request = make_confirmation_request(request_id="duplicate-1")
    task = asyncio.create_task(adapter.request(request))
    await asyncio.sleep(0)
    first = await adapter.respond(make_response(request_id="duplicate-1", decision="approve"))
    if not first.ok:
        raise ConformanceFailure("first response should be accepted")
    outcome = await _await_bounded(task, what="duplicate-scenario request")
    if outcome.status != "approved" or not outcome.executed:
        raise ConformanceFailure("approved request must execute once")
    second = await adapter.respond(make_response(request_id="duplicate-1", decision="approve"))
    if second.ok:
        raise ConformanceFailure("duplicate response was accepted")
    if second.error is None or second.error.code != "already_resolved":
        raise ConformanceFailure("duplicate response must be an already-resolved conflict")
    if len(adapter.execution_log()) != 1:
        raise ConformanceFailure("duplicate response caused a second effect")


async def run_atomic_batch(adapter: ConfirmationAdapter) -> None:
    """A batch applies all entries or none; an invalid entry rolls everything back."""
    request_a = make_confirmation_request(request_id="batch-a")
    request_b = make_confirmation_request(request_id="batch-b")
    task_a = asyncio.create_task(adapter.request(request_a))
    await asyncio.sleep(0)
    task_b = asyncio.create_task(adapter.request(request_b))
    await asyncio.sleep(0)
    invalid = (
        make_response(request_id="batch-a", decision="approve"),
        make_response(request_id="unknown-9", decision="approve"),
    )
    result = await adapter.respond_batch(invalid)
    if result.ok:
        raise ConformanceFailure("batch containing an unknown id must be rejected as a whole")
    if result.applied:
        raise ConformanceFailure(f"invalid batch was partially applied: {result.applied}")
    if adapter.execution_log():
        raise ConformanceFailure("invalid batch executed part of its entries")
    pending = {record.request_id: record.status for record in adapter.pending()}
    if pending.get("batch-a") != "pending" or pending.get("batch-b") != "pending":
        raise ConformanceFailure("invalid batch must leave every entry pending")
    valid = (
        make_response(request_id="batch-a", decision="approve"),
        make_response(request_id="batch-b", decision="deny"),
    )
    applied = await adapter.respond_batch(valid)
    if not applied.ok or tuple(applied.applied) != ("batch-a", "batch-b"):
        raise ConformanceFailure("valid batch must apply every entry atomically")
    outcome_a = await _await_bounded(task_a, what="batch request a")
    outcome_b = await _await_bounded(task_b, what="batch request b")
    if outcome_a.status != "approved" or not outcome_a.executed:
        raise ConformanceFailure("approved batch entry must execute")
    if outcome_b.status != "denied" or outcome_b.executed:
        raise ConformanceFailure("denied batch entry must not execute")
    if len(adapter.execution_log()) != 1:
        raise ConformanceFailure("batch applied the wrong number of operations")


async def run_orphan_no_replay(adapter: ConfirmationAdapter) -> None:
    """Requests pending at crash become orphaned on reopen and never replay."""
    request = make_confirmation_request(request_id="orphan-1")
    task = asyncio.create_task(adapter.request(request))
    await asyncio.sleep(0)
    await adapter.close()  # the owning process dies without responding
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    reopened = await adapter.reopen(runtime_id="runtime-b")
    orphaned = [record for record in reopened.orphaned if record.request_id == "orphan-1"]
    if not orphaned or orphaned[0].status != "orphaned":
        raise ConformanceFailure("pending requests of the crashed owner must reopen as orphaned")
    statuses = {record.request_id: record.status for record in adapter.pending()}
    if statuses.get("orphan-1") != "orphaned":
        raise ConformanceFailure("orphaned record must be visible with status 'orphaned'")
    if adapter.execution_log():
        raise ConformanceFailure("orphaned request was replayed or executed")
    late = await adapter.respond(make_response(request_id="orphan-1", decision="approve"))
    if late.ok:
        raise ConformanceFailure("orphaned request accepted a response")
    if late.error is None or late.error.code != "request_orphaned":
        raise ConformanceFailure("orphaned response must be a structured orphaned conflict")
    if adapter.execution_log():
        raise ConformanceFailure("orphaned request executed after reopen")


async def run_confirmation_redaction(adapter: ConfirmationAdapter) -> None:
    """Structured confirmation errors never leak request arguments or secrets."""
    request = make_confirmation_request(
        request_id="redact-1", secret=KIT_REDACT_SECRET, command=KIT_REDACT_COMMAND
    )
    task = asyncio.create_task(adapter.request(request))
    await asyncio.sleep(0)
    conflict = await adapter.respond(make_response(request_id="does-not-exist", decision="approve"))
    if conflict.ok:
        raise ConformanceFailure("unknown request id must be a conflict")
    text = conflict.error.message if conflict.error else ""
    if conflict.error is not None and conflict.error.details is not None:
        text += json.dumps(to_json_safe(conflict.error.details), sort_keys=True)
    for leaked in (KIT_REDACT_SECRET, KIT_REDACT_COMMAND):
        if leaked in text:
            raise ConformanceFailure("confirmation conflict error leaked request arguments")
    denied = await adapter.respond(make_response(request_id="redact-1", decision="deny"))
    if not denied.ok:
        raise ConformanceFailure("deny of a pending request should succeed")
    outcome = await _await_bounded(task, what="redaction-scenario request")
    metadata = outcome.response.metadata if outcome.response else {}
    meta_text = json.dumps(to_json_safe(metadata), sort_keys=True)
    for leaked in (KIT_REDACT_SECRET, KIT_REDACT_COMMAND):
        if leaked in meta_text:
            raise ConformanceFailure("confirmation outcome leaked request arguments")


# ---------------------------------------------------------------------------
# RPC / Event Stream scenarios
# ---------------------------------------------------------------------------


async def run_strict_json(adapter: RpcAdapter) -> None:
    """The wire codec rejects every malformed envelope with a structured error."""
    cases: list[tuple[str, str]] = [
        ('{"request_id": "broken"', "malformed_json"),
        (
            '{"request_id":"sj-1","method":"runtime.status","params":{},"schema_version":2}',
            "invalid_schema_version",
        ),
        (
            '{"request_id":"sj-1","method":"runtime.status","params":{},"schema_version":1,"extra":1}',
            "invalid_envelope_key",
        ),
        (
            '{"request_id":"sj-1","method":"runtime.status","params":5,"schema_version":1}',
            "invalid_params",
        ),
        (
            '{"request_id":"sj-1","method":"events.replay","params":{"after_sequence":true},'
            '"schema_version":1}',
            "invalid_params",
        ),
    ]
    for line, expected in cases:
        result = await adapter.send_wire(line)
        if result.ok:
            raise ConformanceFailure(f"malformed wire line was accepted: {line!r}")
        if result.error is None or result.error.code != expected:
            raise ConformanceFailure(
                f"expected wire error {expected!r}, got {result.error}"
            )
    good = await adapter.send_wire(
        '{"request_id":"sj-ok","method":"runtime.status","params":{},"schema_version":1}'
    )
    if not good.ok or good.response is None or not good.response.ok:
        raise ConformanceFailure("a well-formed request must be accepted")
    # event decode rejects malformed timestamps and non-object data
    bad_ts = adapter.parse_wire_event(
        '{"event_id":"e-1","sequence":1,"type":"t","data":{},"run_id":null,'
        '"created_at":"not-a-date","schema_version":1}'
    )
    if not isinstance(bad_ts, WireError) or bad_ts.code != "invalid_timestamp":
        raise ConformanceFailure("malformed event timestamp must be rejected")
    bad_data = adapter.parse_wire_event(
        '{"event_id":"e-1","sequence":1,"type":"t","data":[],"run_id":null,'
        '"created_at":"2026-01-01T00:00:00Z","schema_version":1}'
    )
    if not isinstance(bad_data, WireError) or bad_data.code != "invalid_data":
        raise ConformanceFailure("non-object event data must be rejected")
    # event encode rejects unsupported values; there is never a repr fallback
    unsupported = adapter.event_wire(make_event(event_id="ev-bad", sequence=1, data={"obj": object()}))
    if not isinstance(unsupported, WireError) or unsupported.code != "unsupported_value":
        raise ConformanceFailure("unsupported event data must be a protocol error")
    # a valid event encodes to one compact JSON object per line
    encoded = adapter.event_wire(make_event(event_id="ev-ok", sequence=1))
    if isinstance(encoded, WireError):
        raise ConformanceFailure("a valid event must encode to wire text")
    if "\n" in encoded:
        raise ConformanceFailure("wire output must be one JSON object per line")
    payload = json.loads(encoded)
    if not isinstance(payload, dict) or payload["event_id"] != "ev-ok":
        raise ConformanceFailure("event wire output must be a JSON object")
    # publish of unsupported data fails explicitly
    try:
        await adapter.publish("type", {"bad": object()})
    except ProtocolViolationError as violation:
        if violation.error.code != "unsupported_value":
            raise ConformanceFailure("publish error must carry unsupported_value")
    else:
        raise ConformanceFailure("publish of unsupported data must fail explicitly")
    # duplicate request ids are rejected at the wire level
    first = await adapter.send_wire(
        '{"request_id":"sj-dup","method":"runtime.status","params":{},"schema_version":1}'
    )
    if not first.ok:
        raise ConformanceFailure("first well-formed request must be accepted")
    second = await adapter.send_wire(
        '{"request_id":"sj-dup","method":"runtime.status","params":{},"schema_version":1}'
    )
    if second.ok or second.error is None or second.error.code != "duplicate_request_id":
        raise ConformanceFailure("duplicate request id must be rejected at the wire level")


async def run_event_ordering(adapter: RpcAdapter) -> None:
    """Sequences start at 1, are strictly increasing, and replay preserves order."""
    events: list[RpcEvent] = []
    for index in range(1, 6):
        event = await adapter.publish(f"type-{index}", {"n": index})
        events.append(event)
    sequences = [event.sequence for event in events]
    if sequences != [1, 2, 3, 4, 5]:
        raise ConformanceFailure(f"sequences must be monotonic from 1, got {sequences}")
    replay = adapter.replay(after_sequence=0)
    if not replay.ok:
        raise ConformanceFailure("a fresh replay must succeed")
    replayed = [event.sequence for event in replay.events]
    if replayed != [1, 2, 3, 4, 5]:
        raise ConformanceFailure(
            f"replay must preserve order without gaps or duplicates, got {replayed}"
        )


async def run_cursor_expiration(adapter: RpcAdapter) -> None:
    """A cursor older than retained history fails explicitly; never silently skips."""
    capacity = adapter.retained_capacity
    for index in range(1, capacity + 3):  # publish capacity+2, evicting the first two
        await adapter.publish(f"type-{index}", {"n": index})
    expired = adapter.replay(after_sequence=1)
    if expired.ok:
        raise ConformanceFailure("an expired cursor must not be silently skipped")
    if expired.error is None or expired.error.code != "event_cursor_expired":
        raise ConformanceFailure("an expired cursor must raise the cursor-expired error")
    try:
        await adapter.subscribe(after_sequence=1)
    except ProtocolViolationError as violation:
        if violation.error.code != "event_cursor_expired":
            raise ConformanceFailure("expired subscribe cursor must be cursor-expired")
    else:
        raise ConformanceFailure("an expired subscribe cursor must fail explicitly")
    tail = adapter.replay(after_sequence=capacity)
    if not tail.ok:
        raise ConformanceFailure("a valid cursor must still replay")
    tail_sequences = [event.sequence for event in tail.events]
    if tail_sequences != [capacity + 1, capacity + 2]:
        raise ConformanceFailure(
            f"replay after the retained edge returned {tail_sequences}"
        )


async def run_replay_live_handoff(adapter: RpcAdapter) -> None:
    """Retained replay hands off to live events without gaps or duplicates."""
    capacity = adapter.retained_capacity
    if capacity < 5:
        raise ConformanceFailure("retained capacity must be at least 5 for this scenario")
    for index in range(1, capacity + 1):
        await adapter.publish(f"type-{index}", {"n": index})
    subscriber = await adapter.subscribe(after_sequence=2, max_queue=64)
    try:
        for index in range(capacity + 1, capacity + 3):
            await adapter.publish(f"type-{index}", {"n": index})
        received: list[RpcEvent] = []
        for _ in range(capacity):
            event = await _await_bounded(subscriber.next_event(), what="subscriber event")
            if event is None:
                raise ConformanceFailure("subscriber was dropped before the handoff completed")
            received.append(event)
        sequences = [event.sequence for event in received]
        expected = list(range(3, capacity + 3))
        if sequences != expected:
            raise ConformanceFailure(
                f"replay/live handoff gap, duplicate, or missing boundary: "
                f"{sequences} != {expected}"
            )
        ids = [event.event_id for event in received]
        if len(set(ids)) != len(ids):
            raise ConformanceFailure("an event was delivered more than once")
    finally:
        await subscriber.close()


async def run_slow_subscriber_failure(adapter: RpcAdapter) -> None:
    """A slow subscriber fails explicitly instead of blocking the publisher."""
    subscriber = await adapter.subscribe(after_sequence=0, max_queue=1)
    try:
        await adapter.publish("type-1", {"n": 1})  # fills the bounded queue
        task = asyncio.create_task(adapter.publish("type-2", {"n": 2}))
        await asyncio.sleep(0)  # one event-loop yield, not a timing sleep
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise ConformanceFailure("publisher blocked behind a slow subscriber")
        first = await subscriber.next_event()
        if first is None or first.sequence != 1:
            raise ConformanceFailure(
                "slow subscriber must still receive queued events before failing"
            )
        drained = await subscriber.next_event()
        if drained is not None:
            raise ConformanceFailure("an overflowing subscriber did not fail explicitly")
        failure = subscriber.failure()
        if failure is None or failure.code != "subscriber_queue_overflow":
            raise ConformanceFailure(
                "a dropped subscriber must report a structured overflow error"
            )
    finally:
        await subscriber.close()


async def run_unknown_method(adapter: RpcAdapter) -> None:
    """Unknown methods return a stable method_not_found error, never a crash."""
    response = await adapter.call(
        make_rpc_request(request_id="um-1", method="no.such.method", params={})
    )
    if response.ok:
        raise ConformanceFailure("an unknown method must not succeed")
    if response.error is None or response.error.code != "method_not_found":
        raise ConformanceFailure(f"unknown method must be method_not_found, got {response.error}")
    if not response.error.message:
        raise ConformanceFailure("method_not_found must carry a safe message")


async def run_duplicate_request_id(adapter: RpcAdapter) -> None:
    """A repeated request id is rejected and the handler runs exactly once."""
    request = make_rpc_request(request_id="dup-id-1", method="runtime.status", params={})
    first = await adapter.call(request)
    if not first.ok:
        raise ConformanceFailure("the first request must succeed")
    second = await adapter.call(request)
    if second.ok:
        raise ConformanceFailure("a duplicate request id must be rejected")
    if second.error is None or second.error.code != "duplicate_request_id":
        raise ConformanceFailure(
            f"duplicate id must be duplicate_request_id, got {second.error}"
        )
    dispatches = [call for call in adapter.dispatched() if call.request_id == "dup-id-1"]
    if len(dispatches) != 1:
        raise ConformanceFailure(
            f"handler dispatched {len(dispatches)} times for one request id"
        )


async def run_concurrent_run_rejection(adapter: RpcAdapter) -> None:
    """V1 permits one active Run; a second start is rejected run_already_active."""
    first = await adapter.call(
        make_rpc_request(request_id="run-1", method="run.start", params={"run_id": "run-a"})
    )
    if not first.ok:
        raise ConformanceFailure("the first run.start must succeed")
    second = await adapter.call(
        make_rpc_request(request_id="run-2", method="run.start", params={"run_id": "run-b"})
    )
    if second.ok:
        raise ConformanceFailure("a second run.start must be rejected")
    if second.error is None or second.error.code != "run_already_active":
        raise ConformanceFailure(
            f"second run.start must be run_already_active, got {second.error}"
        )
    status = await adapter.call(
        make_rpc_request(request_id="run-3", method="runtime.status", params={})
    )
    if not status.ok or status.result is None or status.result.get("active_run_id") != "run-a":
        raise ConformanceFailure("runtime.status must report the active run")
    aborted = await adapter.call(
        make_rpc_request(request_id="run-4", method="run.abort", params={"run_id": "run-a"})
    )
    if not aborted.ok:
        raise ConformanceFailure("run.abort must succeed")
    third = await adapter.call(
        make_rpc_request(request_id="run-5", method="run.start", params={"run_id": "run-c"})
    )
    if not third.ok:
        raise ConformanceFailure("run.start after abort must succeed")


async def run_rpc_redaction(adapter: RpcAdapter) -> None:
    """Exceptions never leak tracebacks, arguments, or secrets into RPC errors."""
    response = await adapter.call(
        make_rpc_request(
            request_id="redact-1",
            method="explode",
            params={"token": KIT_REDACT_SECRET, "command": "rm -rf /tmp/kit"},
        )
    )
    if response.ok:
        raise ConformanceFailure("the exploding method must fail")
    if response.error is None or response.error.code != "internal_error":
        raise ConformanceFailure(
            f"an unexpected exception must map to internal_error, got {response.error}"
        )
    text = response.error.message
    if response.error.details is not None:
        text += json.dumps(to_json_safe(response.error.details), sort_keys=True)
    for leaked in (KIT_REDACT_SECRET, "rm -rf", "RuntimeError", "Traceback"):
        if leaked in text:
            raise ConformanceFailure("RPC error leaked exception or argument content")


# ---------------------------------------------------------------------------
# Scenario registries
# ---------------------------------------------------------------------------

ConfirmationScenarioFn: TypeAlias = Callable[[ConfirmationAdapter], Awaitable[None]]
RpcScenarioFn: TypeAlias = Callable[[RpcAdapter], Awaitable[None]]

CONFIRMATION_SCENARIOS: dict[str, ConfirmationScenarioFn] = {
    "timeout/no execution": run_timeout_no_execution,
    "stale/expired rejection": run_stale_expired_rejection,
    "abort precedence": run_abort_precedence,
    "duplicate response": run_duplicate_response,
    "atomic batch": run_atomic_batch,
    "orphan/no replay": run_orphan_no_replay,
    "confirmation redaction": run_confirmation_redaction,
}

RPC_SCENARIOS: dict[str, RpcScenarioFn] = {
    "strict JSON": run_strict_json,
    "event ordering": run_event_ordering,
    "cursor expiration": run_cursor_expiration,
    "replay/live handoff": run_replay_live_handoff,
    "slow subscriber failure": run_slow_subscriber_failure,
    "unknown method": run_unknown_method,
    "duplicate request ID": run_duplicate_request_id,
    "concurrent Run rejection": run_concurrent_run_rejection,
    "RPC redaction": run_rpc_redaction,
}
