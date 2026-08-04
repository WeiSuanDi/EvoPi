"""Independent interaction conformance kit (SFU-3).

This module defines the observable-behavior vocabulary for the Steering and
Follow-up interaction-queue contract frozen in the steering-follow-up-v1
milestone CONTEXT.md sections 2-4: narrow adapter Protocols, result
dataclasses, deterministic virtual-phase driving, and reusable async scenario
functions.  The kit is production-independent: it never imports any production
module, it describes only observable behavior (no private fields, no
implementation class names), and Integration binds the approved production
components to ``InteractionAdapter`` in a separate integration-owned test file.

The virtual runtime is advanced with explicit phases (``start_run``,
``model_stream``, ``tool_step``, ``decide``, ``turn_end``,
``terminal_candidate``, ``terminate``) and ``asyncio.sleep(0)`` event-loop
yields only -- there are no wall-clock sleeps, no real models, and no network
anywhere in the kit.  Every scenario is deterministic: task interleaving
follows event-loop scheduling order, so the same adapter always observes the
same event sequence.  The known-good reference adapter in ``reference.py`` is
the oracle that the scenario battery must pass, and the deliberately broken
mutants in ``mutants.py`` prove each scenario is sharp enough to detect the
defect named in the Task acceptance matrix.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias, cast

InteractionKind: TypeAlias = Literal["steer", "follow_up"]
InteractionQueueMode: TypeAlias = Literal["one-at-a-time", "all"]
InteractionOrigin: TypeAlias = Literal["api", "rpc", "repl"]
TerminalReason: TypeAlias = Literal[
    "aborted", "deadline_exceeded", "error", "turn_limit", "cancelled", "closed"
]
EventType: TypeAlias = Literal[
    "interaction_queued",
    "interaction_delivered",
    "interaction_cleared",
    "message_start",
    "message_end",
    "turn_start",
    "turn_end",
    "agent_end",
]

JsonLike: TypeAlias = dict[str, Any] | list[Any] | str | int | float | bool | None

# Stable error codes (CONTEXT.md section 6 safe error codes plus the run gate).
ERROR_RUN_NOT_ACTIVE: str = "run_not_active"
ERROR_QUEUE_FULL: str = "interaction_queue_full"
ERROR_CONTENT_INVALID: str = "interaction_content_invalid"
ERROR_CONTENT_TOO_LARGE: str = "interaction_content_too_large"
ERROR_CLOSED: str = "interaction_closed"


# ---------------------------------------------------------------------------
# Observable vocabulary (CONTEXT.md sections 3-4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class InteractionLimits:
    """Frozen capacity and size limits of one interaction queue."""

    max_pending_items: int = 100
    max_content_bytes: int = 65_536

    def __post_init__(self) -> None:
        for name, value in (
            ("max_pending_items", self.max_pending_items),
            ("max_content_bytes", self.max_content_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an int, got {type(value).__name__}")
            if value <= 0:
                raise ValueError(f"{name} must be positive, got {value!r}")


@dataclass(frozen=True, slots=True, kw_only=True)
class InteractionReceipt:
    """Admission acknowledgement for one accepted interaction."""

    input_id: str
    run_id: str
    kind: InteractionKind
    origin: InteractionOrigin
    created_at: datetime
    position: int


@dataclass(frozen=True, slots=True, kw_only=True)
class InteractionQueueSnapshot:
    """Immutable point-in-time view of the queue; never a live handle."""

    steering_mode: InteractionQueueMode
    follow_up_mode: InteractionQueueMode
    pending_steering_count: int
    pending_follow_up_count: int
    pending: tuple[InteractionReceipt, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class InteractionError:
    """Structured, JSON-safe rejection of one admission attempt."""

    code: str
    message: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AdmissionResult:
    """Outcome of one admission attempt: exactly one of receipt or error."""

    receipt: InteractionReceipt | None = None
    error: InteractionError | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class InteractionMetadata:
    """JSON-safe interaction block attached to a delivered UserMessage."""

    schema_version: int
    input_id: str
    kind: InteractionKind
    origin: InteractionOrigin
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class CommittedMessage:
    """One message that reached the Session projection (persisted facts)."""

    message_id: str
    role: str
    content: str
    interaction: InteractionMetadata | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class EventRecord:
    """One observable runtime event; data is JSON-safe and never carries content."""

    sequence: int
    type: EventType
    run_id: str
    data: dict[str, Any]


class ConformanceFailure(AssertionError):
    """A conformance scenario observed a violation of the frozen contract."""


class InteractionAdapter(Protocol):
    """Observable Steering/Follow-up behavior an implementation must provide.

    Only behavior visible from the outside is described: no private fields, no
    implementation class names, no production imports.  Scenarios drive the
    runtime through explicit virtual phases; every method completes when its
    phase has been fully processed, and ``asyncio.sleep(0)`` yields inside the
    adapter create the deterministic interleaving windows scenarios rely on.
    No method may depend on wall-clock time, real models, or the network.

    Admission: ``steer``/``follow_up`` return an ``AdmissionResult`` -- a
    receipt when the atomic admission/finalization gate admits the item, or a
    structured error.  Codes are ``run_not_active`` (no Run has ever started),
    ``interaction_closed`` (the terminal seal won), ``interaction_queue_full``,
    ``interaction_content_invalid``, and ``interaction_content_too_large``.
    A receipt commits the runtime: the item must later be delivered or appear
    exactly once in an ``interaction_cleared`` event before ``agent_end``.

    Driving: ``start_run`` opens admission and commits the initial Prompt
    message, draining any steering queued before the first model attempt after
    the Prompt and before ``turn_start(1)``.  ``model_stream`` delivers one
    model response (``retry=True`` marks a provider retry, which must consume
    a Model Attempt but not a Turn).  ``tool_step`` executes one sibling
    ToolCall of the current Assistant message through Policy/Confirmation/
    ``after_tool_call``; ``decide`` resolves the currently pending
    Confirmation.  ``turn_end`` finishes after-turn processing and drains
    pending steering at the safe point (returns ``"continued"`` when the run
    proceeds, ``"stop"`` otherwise).  ``terminal_candidate`` atomically drains
    queued input (steering first, then follow-up) or seals admission and ends
    the Run, returning ``"continued"``, ``"completed"``, or ``"terminated"``.
    ``terminate`` clears every undelivered item fail closed with the reason.

    Observation: ``events`` returns the ordered event log (no content anywhere
    in event data), ``committed_messages`` the Session projection (only
    delivered input is persisted, exactly as accepted), ``trace_payloads`` the
    serialized Trace entries (never duplicating content), ``tool_executions``
    the executed Tools, and ``turn_count``/``attempt_count`` the Turn and
    Model-Attempt accounting.  ``wait_for_idle`` resolves only after queue
    settlement and the awaited ``agent_end`` listeners finish.
    """

    @property
    def interaction_limits(self) -> InteractionLimits:
        """Capacity and byte-size limits in force for this queue."""
        ...

    async def steer(
        self, content: str, *, origin: InteractionOrigin = "api"
    ) -> AdmissionResult:
        """Admit one steering interaction, or reject it with a structured error."""
        ...

    async def follow_up(
        self, content: str, *, origin: InteractionOrigin = "api"
    ) -> AdmissionResult:
        """Admit one follow-up interaction, or reject it with a structured error."""
        ...

    def snapshot(self) -> InteractionQueueSnapshot:
        """Immutable point-in-time queue snapshot."""
        ...

    async def wait_for_idle(self) -> None:
        """Resolve only after queue settlement and awaited ``agent_end`` listeners."""
        ...

    async def start_run(self, prompt: str) -> str:
        """Start one Run and return its run id; admission opens synchronously."""
        ...

    async def model_stream(self, *, tool_calls: int = 0, retry: bool = False) -> str:
        """Deliver one model response; ``retry=True`` is a retried Model Attempt."""
        ...

    async def tool_step(self, index: int, *, confirmation: bool = False) -> None:
        """Execute one sibling ToolCall; blocks while its Confirmation is pending."""
        ...

    async def decide(self, *, approved: bool) -> None:
        """Resolve the currently pending Confirmation decision."""
        ...

    async def turn_end(self, *, terminate: bool = False) -> str:
        """Complete after-turn processing; drain the steering safe point."""
        ...

    async def terminal_candidate(self) -> str:
        """Atomically drain queued input or seal admission and end the Run."""
        ...

    async def terminate(self, reason: TerminalReason) -> None:
        """Clear every undelivered interaction fail closed with the reason."""
        ...

    def events(self) -> tuple[EventRecord, ...]:
        """Ordered event log; data is JSON-safe and never carries content."""
        ...

    def committed_messages(self) -> tuple[CommittedMessage, ...]:
        """Session projection: only delivered messages, content preserved exactly."""
        ...

    def trace_payloads(self) -> tuple[str, ...]:
        """Serialized Trace entries; must never duplicate interaction content."""
        ...

    def tool_executions(self) -> tuple[str, ...]:
        """Names of Tools that actually executed, in execution order."""
        ...

    def turn_count(self) -> int:
        """Ordinary Turns consumed by the current Run (retries excluded)."""
        ...

    def attempt_count(self) -> int:
        """Model Attempts made by the current Run (retries included)."""
        ...


# ---------------------------------------------------------------------------
# Deterministic synthetic fixtures
# ---------------------------------------------------------------------------

FIXED_TS: datetime = datetime(2026, 1, 1, tzinfo=UTC)

REDACT_DELIVERED_CONTENT: str = "SFU3-REDACT-DELIVERED-8f3a"
REDACT_QUEUED_CONTENT: str = "SFU3-REDACT-QUEUED-7b2c"


def to_json_safe(value: Any) -> JsonLike:
    """Convert kit values to JSON-safe equivalents; reject everything else.

    Accepts JSON primitives, any string-keyed Mapping, any non-byte Sequence,
    dataclasses, enums, Path, and datetime/date.  Non-string mapping keys,
    bytes, non-finite floats, and Enum values that are not JSON primitives are
    rejected with ValueError; there is never a ``repr`` fallback.
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float")
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        if isinstance(value.value, (str, int, bool)):
            return value.value
        if isinstance(value.value, float):
            if not math.isfinite(value.value):
                raise ValueError("non-finite float")
            return value.value
        raise ValueError(f"enum value of type {type(value.value).__name__} is not JSON-safe")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        converted: dict[str, JsonLike] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"non-string mapping key of type {type(key).__name__}")
            converted[key] = to_json_safe(item)
        return converted
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_json_safe(item) for item in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {item.name: to_json_safe(getattr(value, item.name)) for item in dataclasses.fields(value)}
    raise ValueError(f"unsupported value of type {type(value).__name__}")


# ---------------------------------------------------------------------------
# Scenario helpers
# ---------------------------------------------------------------------------


async def _await_bounded(awaitable: Awaitable[Any], *, what: str, seconds: float = 5.0) -> Any:
    """Await with a safety bound so a hung adapter fails the scenario fast."""
    try:
        return await asyncio.wait_for(awaitable, timeout=seconds)
    except asyncio.TimeoutError:
        raise ConformanceFailure(
            f"{what} hung; expected completion within {seconds:g}s"
        ) from None


def _require_receipt(result: AdmissionResult, what: str) -> InteractionReceipt:
    """Unwrap an admission result; a rejection or empty result is a failure."""
    if result.receipt is not None and result.error is not None:
        raise ConformanceFailure(f"{what}: admission returned both a receipt and an error")
    if result.error is not None:
        raise ConformanceFailure(f"{what}: unexpected rejection {result.error.code}: {result.error.message}")
    if result.receipt is None:
        raise ConformanceFailure(f"{what}: admission returned neither receipt nor error")
    return result.receipt


def _require_error(result: AdmissionResult, expected: str, what: str) -> InteractionError:
    """Unwrap an admission rejection; a receipt or other code is a failure."""
    if result.error is None:
        raise ConformanceFailure(f"{what}: admission was accepted when it must fail")
    if result.error.code != expected:
        raise ConformanceFailure(f"{what}: expected {expected!r}, got {result.error.code!r}")
    return result.error


def _events_of(adapter: InteractionAdapter, run_id: str) -> tuple[EventRecord, ...]:
    return tuple(e for e in adapter.events() if e.run_id == run_id)


def _delivered_events_for(
    adapter: InteractionAdapter, run_id: str, input_id: str
) -> tuple[EventRecord, ...]:
    return tuple(
        e
        for e in adapter.events()
        if e.run_id == run_id and e.type == "interaction_delivered" and e.data.get("input_id") == input_id
    )


def _queued_events_for(
    adapter: InteractionAdapter, run_id: str, input_id: str
) -> tuple[EventRecord, ...]:
    return tuple(
        e
        for e in adapter.events()
        if e.run_id == run_id and e.type == "interaction_queued" and e.data.get("input_id") == input_id
    )


def _delivery_sequence(
    adapter: InteractionAdapter, run_id: str, input_id: str
) -> tuple[EventRecord, EventRecord, EventRecord]:
    """Return (delivered, message_start, message_end) for one delivered input.

    Raises when the input was not delivered exactly once or its message events
    are missing, so every scenario that calls this also proves exactly-once
    delivery and the message-pairing invariant.
    """
    delivered = _delivered_events_for(adapter, run_id, input_id)
    if len(delivered) != 1:
        raise ConformanceFailure(
            f"expected exactly one interaction_delivered for {input_id}, got {len(delivered)}"
        )
    message_id = delivered[0].data.get("message_id")
    starts = [
        e
        for e in adapter.events()
        if e.run_id == run_id and e.type == "message_start" and e.data.get("message_id") == message_id
    ]
    ends = [
        e
        for e in adapter.events()
        if e.run_id == run_id and e.type == "message_end" and e.data.get("message_id") == message_id
    ]
    if len(starts) != 1 or len(ends) != 1:
        raise ConformanceFailure(f"message events for {input_id} must pair with its delivery")
    return delivered[0], starts[0], ends[0]


def _assert_strictly_before(
    events: Sequence[EventRecord], first: EventRecord, second: EventRecord, what: str
) -> None:
    if not first.sequence < second.sequence:
        raise ConformanceFailure(f"{what}: expected {first.type} before {second.type}")


async def _drive_to_completion(adapter: InteractionAdapter) -> str:
    """Drive the virtual runtime until the Run seals; return the outcome.

    Precondition: a model call for the current continuation is in flight.
    Each cycle delivers that response, completes after-turn processing, and
    either drains the next queued item at the terminal candidate or seals.
    """
    while True:
        await adapter.model_stream(tool_calls=0)
        outcome = await adapter.turn_end()
        if outcome == "stop":
            outcome = await adapter.terminal_candidate()
        if outcome != "continued":
            return outcome


# ---------------------------------------------------------------------------
# Scenario battery (CONTEXT.md sections 2-4)
# ---------------------------------------------------------------------------


async def run_initial_admission_safe_point(adapter: InteractionAdapter) -> None:
    """Steering queued before the first model attempt drains after the initial
    Prompt message and before ``turn_start(1)``."""
    task = asyncio.create_task(adapter.start_run("prompt-1"))
    await asyncio.sleep(0)
    receipt = _require_receipt(
        await adapter.steer("steer-early"), "steer before the first model attempt"
    )
    run_id = await _await_bounded(task, what="start_run")
    events = _events_of(adapter, run_id)
    queued = _queued_events_for(adapter, run_id, receipt.input_id)
    if len(queued) != 1:
        raise ConformanceFailure("the early steer must have exactly one queued event")
    delivered, start, end = _delivery_sequence(adapter, run_id, receipt.input_id)
    turn_starts = [e for e in events if e.type == "turn_start"]
    if len(turn_starts) != 1 or turn_starts[0].data.get("turn_number") != 1:
        raise ConformanceFailure("the first model Turn must be turn_start(1)")
    _assert_strictly_before(events, queued[0], delivered, "queued before delivered")
    _assert_strictly_before(events, delivered, start, "delivered before message_start")
    _assert_strictly_before(events, start, end, "message_start before message_end")
    _assert_strictly_before(events, end, turn_starts[0], "initial drain before turn_start(1)")
    if adapter.turn_count() != 1:
        raise ConformanceFailure("the initial drain must consume exactly one Turn")


async def run_steering_during_model_stream(adapter: InteractionAdapter) -> None:
    """Steering admitted while the Assistant message is still streaming is
    delivered only at the safe point, never mid-stream."""
    run_id = await adapter.start_run("prompt-1")
    stream = asyncio.create_task(adapter.model_stream(tool_calls=2))
    await asyncio.sleep(0)
    receipt = _require_receipt(
        await adapter.steer("steer-during-stream"), "steer during the model stream"
    )
    await _await_bounded(stream, what="model stream")
    await adapter.tool_step(0)
    await adapter.tool_step(1)
    outcome = await adapter.turn_end()
    if outcome != "continued":
        raise ConformanceFailure("drained steering must continue the Run")
    delivered, start, end = _delivery_sequence(adapter, run_id, receipt.input_id)
    queued = _queued_events_for(adapter, run_id, receipt.input_id)
    _assert_strictly_before(events := _events_of(adapter, run_id), queued[0], delivered, "queued before delivered")
    _assert_strictly_before(events, delivered, start, "delivered before message_start")
    _assert_strictly_before(events, start, end, "message_start before message_end")


async def run_steering_during_first_sibling_tool(adapter: InteractionAdapter) -> None:
    """Steering queued during the first sibling Tool waits until the complete
    sibling batch and ``turn_end`` finish before delivery."""
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=2)
    first = asyncio.create_task(adapter.tool_step(0))
    await asyncio.sleep(0)
    receipt = _require_receipt(
        await adapter.steer("steer-first-sibling"), "steer during the first sibling Tool"
    )
    await _await_bounded(first, what="first sibling Tool")
    await adapter.tool_step(1)
    if _delivered_events_for(adapter, run_id, receipt.input_id):
        raise ConformanceFailure("steering was delivered before the full sibling Tool batch finished")
    outcome = await adapter.turn_end()
    if outcome != "continued":
        raise ConformanceFailure("drained steering must continue the Run")
    delivered, start, end = _delivery_sequence(adapter, run_id, receipt.input_id)
    events = _events_of(adapter, run_id)
    queued = _queued_events_for(adapter, run_id, receipt.input_id)
    _assert_strictly_before(events, queued[0], delivered, "queued before delivered")
    _assert_strictly_before(events, delivered, start, "delivered before message_start")
    _assert_strictly_before(events, start, end, "message_start before message_end")


async def run_steering_during_last_sibling_tool(adapter: InteractionAdapter) -> None:
    """Steering queued while the last sibling Tool runs is delivered only at
    the safe point after ``turn_end``."""
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=2)
    await adapter.tool_step(0)
    last = asyncio.create_task(adapter.tool_step(1))
    await asyncio.sleep(0)
    receipt = _require_receipt(
        await adapter.steer("steer-last-sibling"), "steer during the last sibling Tool"
    )
    await _await_bounded(last, what="last sibling Tool")
    if _delivered_events_for(adapter, run_id, receipt.input_id):
        raise ConformanceFailure("steering was delivered before the safe point after turn_end")
    outcome = await adapter.turn_end()
    if outcome != "continued":
        raise ConformanceFailure("drained steering must continue the Run")
    delivered, start, end = _delivery_sequence(adapter, run_id, receipt.input_id)
    events = _events_of(adapter, run_id)
    queued = _queued_events_for(adapter, run_id, receipt.input_id)
    _assert_strictly_before(events, queued[0], delivered, "queued before delivered")
    _assert_strictly_before(events, delivered, start, "delivered before message_start")
    _assert_strictly_before(events, start, end, "message_start before message_end")


async def run_steering_during_confirmation_wait(adapter: InteractionAdapter) -> None:
    """Steering may queue while a sibling Tool waits on Confirmation, but it
    neither resolves nor cancels the pending decision and is delivered only
    after that ToolCall reaches its final result."""
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=2)
    first = asyncio.create_task(adapter.tool_step(0, confirmation=True))
    await asyncio.sleep(0)
    receipt = _require_receipt(
        await adapter.steer("steer-while-confirming"), "steer during the Confirmation wait"
    )
    if _delivered_events_for(adapter, run_id, receipt.input_id):
        raise ConformanceFailure("steering was delivered while the Confirmation is pending")
    await adapter.decide(approved=True)
    await _await_bounded(first, what="confirmed Tool")
    if "tool-0" not in adapter.tool_executions():
        raise ConformanceFailure("the approved Tool must execute exactly once")
    await adapter.tool_step(1)
    outcome = await adapter.turn_end()
    if outcome != "continued":
        raise ConformanceFailure("drained steering must continue the Run")
    delivered, start, end = _delivery_sequence(adapter, run_id, receipt.input_id)
    events = _events_of(adapter, run_id)
    queued = _queued_events_for(adapter, run_id, receipt.input_id)
    _assert_strictly_before(events, queued[0], delivered, "queued before delivered")
    _assert_strictly_before(events, delivered, start, "delivered before message_start")
    _assert_strictly_before(events, start, end, "message_start before message_end")


async def run_steering_after_turn_end_drain(adapter: InteractionAdapter) -> None:
    """Steering arriving after a safe-point snapshot has already been drained
    waits for the following safe point instead of joining the drained batch."""
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=0)
    outcome = await adapter.turn_end()
    if outcome != "stop":
        raise ConformanceFailure("a tool-less turn without pending steering must stop")
    receipt = _require_receipt(
        await adapter.steer("steer-after-drain"), "steer after the safe point was drained"
    )
    if _delivered_events_for(adapter, run_id, receipt.input_id):
        raise ConformanceFailure("a steer queued after the drain must wait for the next safe point")
    outcome = await adapter.terminal_candidate()
    if outcome != "continued":
        raise ConformanceFailure("queued steering at the terminal candidate must continue the Run")
    delivered, start, end = _delivery_sequence(adapter, run_id, receipt.input_id)
    events = _events_of(adapter, run_id)
    queued = _queued_events_for(adapter, run_id, receipt.input_id)
    _assert_strictly_before(events, queued[0], delivered, "queued before delivered")
    _assert_strictly_before(events, delivered, start, "delivered before message_start")
    _assert_strictly_before(events, start, end, "message_start before message_end")


async def run_steering_before_next_model_call(adapter: InteractionAdapter) -> None:
    """Steering queued after the sibling batch but before the safe point is
    delivered before the next model call starts."""
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=1)
    await adapter.tool_step(0)
    receipt = _require_receipt(
        await adapter.steer("steer-before-next-call"), "steer just before the next model call"
    )
    outcome = await adapter.turn_end()
    if outcome != "continued":
        raise ConformanceFailure("drained steering must continue the Run")
    events = _events_of(adapter, run_id)
    delivered, start, end = _delivery_sequence(adapter, run_id, receipt.input_id)
    next_turns = [e for e in events if e.type == "turn_start" and e.data.get("turn_number") == 2]
    if len(next_turns) != 1:
        raise ConformanceFailure("the drained steering must start turn 2")
    _assert_strictly_before(events, end, next_turns[0], "delivered message before the next model call")
    if adapter.turn_count() != 2:
        raise ConformanceFailure("the steering continuation must consume exactly one Turn")


async def run_follow_up_during_tool_continuation(adapter: InteractionAdapter) -> None:
    """Follow-up queued during an active Tool continuation is never drained at
    a turn safe point; it is delivered only at a terminal candidate."""
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=2)
    first = asyncio.create_task(adapter.tool_step(0))
    await asyncio.sleep(0)
    receipt = _require_receipt(
        await adapter.follow_up("follow-up-during-tools"), "follow-up during Tool continuation"
    )
    await _await_bounded(first, what="Tool")
    await adapter.tool_step(1)
    await adapter.turn_end()
    if _delivered_events_for(adapter, run_id, receipt.input_id):
        raise ConformanceFailure("follow-up was drained at a turn safe point, not a terminal candidate")
    await adapter.model_stream(tool_calls=0)
    await adapter.turn_end()
    outcome = await adapter.terminal_candidate()
    if outcome != "continued":
        raise ConformanceFailure("follow-up at the terminal candidate must continue the Run")
    delivered, start, end = _delivery_sequence(adapter, run_id, receipt.input_id)
    events = _events_of(adapter, run_id)
    queued = _queued_events_for(adapter, run_id, receipt.input_id)
    _assert_strictly_before(events, queued[0], delivered, "queued before delivered")
    _assert_strictly_before(events, delivered, start, "delivered before message_start")
    _assert_strictly_before(events, start, end, "message_start before message_end")


async def run_steering_priority_over_follow_up(adapter: InteractionAdapter) -> None:
    """Queued steering has priority over an earlier queued follow-up: steering
    is delivered first and the follow-up waits; both arrive exactly once."""
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=0)
    await adapter.turn_end()
    fu = _require_receipt(await adapter.follow_up("follow-up-queued-first"), "follow-up queued first")
    steer = _require_receipt(await adapter.steer("steer-queued-second"), "steer queued second")
    outcome = await adapter.terminal_candidate()
    if outcome != "continued":
        raise ConformanceFailure("queued input at the terminal candidate must continue the Run")
    if not _delivered_events_for(adapter, run_id, steer.input_id):
        raise ConformanceFailure("steering must be drained before the follow-up")
    if _delivered_events_for(adapter, run_id, fu.input_id):
        raise ConformanceFailure("follow-up must wait while steering is pending")
    final = await _drive_to_completion(adapter)
    if final != "completed":
        raise ConformanceFailure(f"expected completed, got {final}")
    _delivery_sequence(adapter, run_id, steer.input_id)
    _delivery_sequence(adapter, run_id, fu.input_id)
    events = _events_of(adapter, run_id)
    steer_delivered = _delivered_events_for(adapter, run_id, steer.input_id)[0]
    fu_delivered = _delivered_events_for(adapter, run_id, fu.input_id)[0]
    _assert_strictly_before(events, steer_delivered, fu_delivered, "steering before follow-up")


async def run_no_tool_completion_follow_up(adapter: InteractionAdapter) -> None:
    """Follow-up queued when the Run would otherwise finish as completed is
    delivered at the terminal candidate; the Run continues and completes only
    when nothing is queued."""
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=0)
    outcome = await adapter.turn_end()
    if outcome != "stop":
        raise ConformanceFailure("a tool-less turn without pending steering must stop")
    receipt = _require_receipt(
        await adapter.follow_up("follow-up-at-completion"), "follow-up at natural completion"
    )
    outcome = await adapter.terminal_candidate()
    if outcome != "continued":
        raise ConformanceFailure("queued follow-up must override the otherwise finished Run")
    _delivery_sequence(adapter, run_id, receipt.input_id)
    final = await _drive_to_completion(adapter)
    if final != "completed":
        raise ConformanceFailure(f"expected completed, got {final}")


async def run_graceful_terminate_override(adapter: InteractionAdapter) -> None:
    """Explicit queued human input overrides an otherwise graceful
    ``terminate=True`` batch: the Run continues instead of sealing."""
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=0)
    await adapter.turn_end(terminate=True)
    receipt = _require_receipt(
        await adapter.follow_up("follow-up-overrides-terminate"), "follow-up after graceful terminate"
    )
    outcome = await adapter.terminal_candidate()
    if outcome != "continued":
        raise ConformanceFailure("queued input must override the graceful terminate decision")
    _delivery_sequence(adapter, run_id, receipt.input_id)


async def run_graceful_terminate_seals(adapter: InteractionAdapter) -> None:
    """With no queued input, a graceful ``terminate=True`` batch seals the Run
    as terminated and nothing is delivered."""
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=0)
    await adapter.turn_end(terminate=True)
    outcome = await adapter.terminal_candidate()
    if outcome != "terminated":
        raise ConformanceFailure(f"expected terminated, got {outcome}")
    events = _events_of(adapter, run_id)
    agent_ends = [e for e in events if e.type == "agent_end"]
    if len(agent_ends) != 1 or agent_ends[0].data.get("outcome") != "terminated":
        raise ConformanceFailure("agent_end must report the terminated outcome")
    if any(e.type == "interaction_delivered" for e in events):
        raise ConformanceFailure("a sealed Run must deliver nothing")
    if [m for m in adapter.committed_messages() if m.interaction is not None]:
        raise ConformanceFailure("a sealed Run must persist no interaction message")
    await _await_bounded(adapter.wait_for_idle(), what="idle after sealed termination")


async def run_atomic_drain_snapshot(adapter: InteractionAdapter) -> None:
    """The drain batch is one atomic snapshot: items admitted while the drain
    is in progress never join the current batch and wait for the next terminal
    candidate.  One-at-a-time drains one item; ``all`` drains the snapshot
    before one model request (one Turn, not one per item)."""
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=0)
    await adapter.turn_end()
    first = _require_receipt(await adapter.follow_up("f1"), "first follow-up")
    second = _require_receipt(await adapter.follow_up("f2"), "second follow-up")
    candidate = asyncio.create_task(adapter.terminal_candidate())
    await asyncio.sleep(0)
    late = _require_receipt(await adapter.follow_up("f3"), "follow-up during the drain")
    outcome = await _await_bounded(candidate, what="terminal candidate drain")
    if outcome != "continued":
        raise ConformanceFailure("the drained follow-up batch must continue the Run")
    mode = adapter.snapshot().follow_up_mode
    expected_first = 1 if mode == "one-at-a-time" else 2
    first_batch = [
        e.data.get("input_id")
        for e in _events_of(adapter, run_id)
        if e.type == "interaction_delivered"
    ]
    if len(first_batch) != expected_first:
        raise ConformanceFailure(
            f"the first drain batch had {len(first_batch)} items, expected {expected_first} for {mode}"
        )
    if first.input_id not in first_batch:
        raise ConformanceFailure("FIFO: the first admitted follow-up must drain first")
    if second.input_id in first_batch and mode == "one-at-a-time":
        raise ConformanceFailure("one-at-a-time drained more than one item")
    if second.input_id not in first_batch and mode == "all":
        raise ConformanceFailure("all-mode must snapshot every item admitted before the drain")
    if late.input_id in first_batch:
        raise ConformanceFailure("an arrival during the drain joined the current batch (atomic snapshot violated)")
    if adapter.turn_count() != 2:
        raise ConformanceFailure(f"the drained batch must consume exactly one Turn, got {adapter.turn_count()}")
    final = await _drive_to_completion(adapter)
    if final != "completed":
        raise ConformanceFailure(f"expected completed, got {final}")
    for receipt in (first, second, late):
        _delivery_sequence(adapter, run_id, receipt.input_id)
    events = _events_of(adapter, run_id)
    order = {
        e.data.get("input_id"): e.sequence
        for e in events
        if e.type == "interaction_delivered"
    }
    if not (
        order[first.input_id] < order[second.input_id] < order[late.input_id]
    ):
        raise ConformanceFailure("deliveries must follow FIFO admission order across candidates")


async def run_fifo_ordering(adapter: InteractionAdapter) -> None:
    """Receipt positions are 1-based and strictly increasing, and delivery
    order across candidates is exactly the admission order."""
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=0)
    await adapter.turn_end()
    first = _require_receipt(await adapter.follow_up("f1"), "first follow-up")
    second = _require_receipt(await adapter.follow_up("f2"), "second follow-up")
    if first.position != 1 or second.position != 2:
        raise ConformanceFailure("positions must be 1-based and strictly increasing")
    mode = adapter.snapshot().follow_up_mode
    outcome = await adapter.terminal_candidate()
    if outcome != "continued":
        raise ConformanceFailure("the first follow-up batch must continue the Run")
    first_batch = [
        e.data.get("input_id")
        for e in _events_of(adapter, run_id)
        if e.type == "interaction_delivered"
    ]
    expected = [first.input_id] if mode == "one-at-a-time" else [first.input_id, second.input_id]
    if first_batch != expected:
        raise ConformanceFailure(f"the first batch must be {expected} in FIFO order, got {first_batch}")
    final = await _drive_to_completion(adapter)
    if final != "completed":
        raise ConformanceFailure(f"expected completed, got {final}")
    events = _events_of(adapter, run_id)
    order = {
        e.data.get("input_id"): e.sequence
        for e in events
        if e.type == "interaction_delivered"
    }
    if not (order[first.input_id] < order[second.input_id]):
        raise ConformanceFailure("deliveries must follow FIFO admission order")


async def run_enqueue_vs_terminal_seal(adapter: InteractionAdapter) -> None:
    """At the terminal candidate the gate is atomic: when nothing is queued the
    seal wins deterministically, and any later admission fails closed with
    ``interaction_closed``, creating no item and no queued event."""
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=0)
    await adapter.turn_end()
    outcome = await adapter.terminal_candidate()
    if outcome != "completed":
        raise ConformanceFailure(f"expected completed, got {outcome}")
    late = await adapter.steer("late-steer")
    _require_error(late, ERROR_CLOSED, "steer after the terminal seal")
    if _queued_events_for(adapter, run_id, "late-steer"):
        raise ConformanceFailure("a rejected admission must not create a queued event")
    events = _events_of(adapter, run_id)
    if any(e.type == "interaction_queued" for e in events):
        raise ConformanceFailure("a sealed Run must have no queued events after the seal")
    if adapter.turn_count() != 1:
        raise ConformanceFailure("the sealed Run must not start a new model call")
    await _await_bounded(adapter.wait_for_idle(), what="idle after the sealed Run")


async def run_terminal_priority(adapter: InteractionAdapter) -> None:
    """Abort, deadline, final-retry error, and Turn exhaustion clear every
    undelivered item fail closed: cleared evidence before ``agent_end``, no
    delivery, and no new model call."""
    for reason in ("aborted", "deadline_exceeded", "error", "turn_limit"):
        run_id = await adapter.start_run("prompt-1")
        await adapter.model_stream(tool_calls=0)
        await adapter.turn_end()
        steer = _require_receipt(await adapter.steer(f"steer-{reason}"), f"steer before {reason}")
        fu = _require_receipt(await adapter.follow_up(f"follow-up-{reason}"), f"follow-up before {reason}")
        await adapter.terminate(cast(TerminalReason, reason))
        events = _events_of(adapter, run_id)
        cleared = [e for e in events if e.type == "interaction_cleared"]
        if len(cleared) != 1:
            raise ConformanceFailure(f"exactly one interaction_cleared expected for {reason}")
        record = cleared[0]
        if record.data.get("reason") != reason:
            raise ConformanceFailure(f"cleared reason must be {reason}, got {record.data.get('reason')}")
        if record.data.get("count") != 2:
            raise ConformanceFailure(f"cleared count must be 2, got {record.data.get('count')}")
        ids = list(record.data.get("input_ids", ()))
        kinds = list(record.data.get("kinds", ()))
        if ids != [steer.input_id, fu.input_id] or kinds != ["steer", "follow_up"]:
            raise ConformanceFailure("cleared input IDs and kinds must be exact and in admission order")
        if len(ids) != len(set(ids)):
            raise ConformanceFailure("cleared input IDs must be unique")
        if any(e.type == "interaction_delivered" for e in events):
            raise ConformanceFailure(f"undelivered items must clear, not deliver, on {reason}")
        if adapter.turn_count() != 1 or adapter.attempt_count() != 1:
            raise ConformanceFailure(f"terminal {reason} must not start a new model call")
        agent_ends = [e for e in events if e.type == "agent_end"]
        if len(agent_ends) != 1 or agent_ends[0].data.get("outcome") != reason:
            raise ConformanceFailure("agent_end must report the terminal reason")
        _assert_strictly_before(events, record, agent_ends[0], "cleared before agent_end")
        await _await_bounded(adapter.wait_for_idle(), what="idle after the terminal clear")


async def run_retry_is_not_a_turn(adapter: InteractionAdapter) -> None:
    """A provider retry is a Model Attempt, not another Turn: the Turn budget
    is unchanged while the attempt count advances."""
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=0)
    await adapter.turn_end()
    receipt = _require_receipt(await adapter.steer("steer-retry"), "steer before the retry")
    outcome = await adapter.terminal_candidate()
    if outcome != "continued":
        raise ConformanceFailure("the drained steering must continue the Run")
    if adapter.turn_count() != 2:
        raise ConformanceFailure(f"expected 2 Turns before the retry, got {adapter.turn_count()}")
    attempts_before = adapter.attempt_count()
    await adapter.model_stream(tool_calls=0, retry=True)
    if adapter.turn_count() != 2:
        raise ConformanceFailure("a provider retry must not consume another Turn")
    if adapter.attempt_count() != attempts_before + 1:
        raise ConformanceFailure("a retry is exactly one more Model Attempt")
    outcome = await adapter.turn_end()
    if outcome != "stop":
        raise ConformanceFailure("the retried response turn must stop cleanly")
    final = await adapter.terminal_candidate()
    if final != "completed":
        raise ConformanceFailure(f"expected completed, got {final}")
    _delivery_sequence(adapter, run_id, receipt.input_id)


async def run_event_ordering(adapter: InteractionAdapter) -> None:
    """For one delivered item the safe-point window after ``turn_end`` is
    exactly ``interaction_delivered``, ``message_start``, ``message_end``
    followed by ``turn_start`` of the next model call."""
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=1)
    await adapter.tool_step(0)
    receipt = _require_receipt(await adapter.steer("steer-ordered"), "steer before the safe point")
    outcome = await adapter.turn_end()
    if outcome != "continued":
        raise ConformanceFailure("drained steering must continue the Run")
    events = _events_of(adapter, run_id)
    turn_ends = [e for e in events if e.type == "turn_end"]
    if len(turn_ends) != 1:
        raise ConformanceFailure("expected exactly one turn_end event")
    turn_starts = [e for e in events if e.type == "turn_start"]
    if len(turn_starts) != 2:
        raise ConformanceFailure("expected turn_start for the prompt and the steering continuation")
    next_turn = [e for e in turn_starts if e.data.get("turn_number") == 2]
    if len(next_turn) != 1:
        raise ConformanceFailure("the continuation must open turn 2")
    delivered, start, end = _delivery_sequence(adapter, run_id, receipt.input_id)
    window = [e for e in events if turn_ends[0].sequence < e.sequence < next_turn[0].sequence]
    if window != [delivered, start, end]:
        raise ConformanceFailure(
            f"the safe-point window must be exactly delivered/message_start/message_end, got "
            f"{[e.type for e in window]}"
        )


async def run_exactly_once_input_ids(adapter: InteractionAdapter) -> None:
    """Every accepted input ID appears exactly once as delivered or cleared,
    cleared events precede ``agent_end``, and the follow-up never drains at a
    steering safe point."""
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=2)
    await adapter.tool_step(0)
    await adapter.tool_step(1)
    await adapter.turn_end()
    steer1 = _require_receipt(await adapter.steer("s1"), "first steer")
    fu = _require_receipt(await adapter.follow_up("f1"), "follow-up")
    steer2 = _require_receipt(await adapter.steer("s2"), "second steer")
    outcome = await adapter.terminal_candidate()
    if outcome != "continued":
        raise ConformanceFailure("queued steering must continue the Run")
    mode = adapter.snapshot().steering_mode
    delivered_ids = [
        e.data.get("input_id")
        for e in _events_of(adapter, run_id)
        if e.type == "interaction_delivered"
    ]
    expected = [steer1.input_id] if mode == "one-at-a-time" else [steer1.input_id, steer2.input_id]
    if delivered_ids != expected:
        raise ConformanceFailure(f"the steering drain must follow the configured mode, got {delivered_ids}")
    if fu.input_id in delivered_ids:
        raise ConformanceFailure("follow-up must never drain at a steering safe point")
    await adapter.terminate("cancelled")
    events = _events_of(adapter, run_id)
    cleared_events = [e for e in events if e.type == "interaction_cleared"]
    if len(cleared_events) != 1 or cleared_events[0].data.get("reason") != "cancelled":
        raise ConformanceFailure("the terminal clear must be one cancelled event")
    cleared_ids = [cast(str, item) for item in cleared_events[0].data.get("input_ids", ())]
    final_delivered_ids = [
        cast(str, e.data["input_id"]) for e in events if e.type == "interaction_delivered"
    ]
    evidence = final_delivered_ids + cleared_ids
    all_ids = [steer1.input_id, fu.input_id, steer2.input_id]
    if sorted(evidence) != sorted(all_ids):
        raise ConformanceFailure("every accepted input ID must appear exactly once as delivered or cleared")
    if len(evidence) != len(set(evidence)):
        raise ConformanceFailure("an input ID appeared more than once")
    agent_ends = [e for e in events if e.type == "agent_end"]
    if len(agent_ends) != 1 or agent_ends[0].data.get("outcome") != "cancelled":
        raise ConformanceFailure("agent_end must report the cancelled outcome")
    _assert_strictly_before(events, cleared_events[0], agent_ends[0], "cleared before agent_end")
    if any(e.type == "interaction_delivered" and e.sequence > agent_ends[0].sequence for e in events):
        raise ConformanceFailure("a delivery after agent_end would strand an acknowledged input")
    await _await_bounded(adapter.wait_for_idle(), what="idle after the terminal clear")


async def run_session_projection(adapter: InteractionAdapter) -> None:
    """Only delivered input is persisted: the Session projection contains the
    Prompt and the delivered message with exact content and the interaction
    metadata block; queued-but-undelivered content never enters it."""
    await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=2)
    await adapter.tool_step(0)
    await adapter.tool_step(1)
    first = _require_receipt(await adapter.steer("first-delivered-content"), "first steer")
    _require_receipt(await adapter.steer("second-still-queued-content"), "second steer")
    outcome = await adapter.turn_end()
    if outcome != "continued":
        raise ConformanceFailure("drained steering must continue the Run")
    mode = adapter.snapshot().steering_mode
    expected_drained = 1 if mode == "one-at-a-time" else 2
    committed = adapter.committed_messages()
    interaction_msgs = [m for m in committed if m.interaction is not None]
    if len(interaction_msgs) != expected_drained:
        raise ConformanceFailure(
            f"exactly {expected_drained} delivered message(s) must be committed, got {len(interaction_msgs)}"
        )
    delivered_msg = next(
        m
        for m in interaction_msgs
        if m.interaction is not None and m.interaction.input_id == first.input_id
    )
    if delivered_msg.content != "first-delivered-content":
        raise ConformanceFailure("content must be preserved exactly after validation")
    if delivered_msg.role != "user":
        raise ConformanceFailure("a delivered interaction must be a UserMessage")
    metadata = delivered_msg.interaction
    if metadata is None:
        raise ConformanceFailure("the delivered message must carry interaction metadata")
    if metadata.schema_version != 1 or metadata.kind != "steer" or metadata.origin != "api":
        raise ConformanceFailure("the interaction metadata block must be exact")
    if metadata.created_at != first.created_at:
        raise ConformanceFailure("the metadata must carry the receipt's created_at")
    if any(m.content == "second-still-queued-content" for m in committed):
        raise ConformanceFailure("queued-but-undelivered content was persisted into the Session")
    prompt_msgs = [m for m in committed if m.interaction is None and m.role == "user"]
    if len(prompt_msgs) != 1 or prompt_msgs[0].content != "prompt-1":
        raise ConformanceFailure("the initial Prompt must be committed exactly once without metadata")


async def run_trace_redaction(adapter: InteractionAdapter) -> None:
    """Event data and Trace entries never duplicate interaction content --
    neither for delivered input nor for queued-then-cleared input."""
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=2)
    await adapter.tool_step(0)
    await adapter.tool_step(1)
    _require_receipt(await adapter.steer(REDACT_DELIVERED_CONTENT), "delivered redaction steer")
    outcome = await adapter.turn_end()
    if outcome != "continued":
        raise ConformanceFailure("drained steering must continue the Run")
    _require_receipt(await adapter.follow_up(REDACT_QUEUED_CONTENT), "queued redaction follow-up")
    await adapter.terminate("closed")
    serialized_events = "\n".join(
        json.dumps(to_json_safe(e.data), sort_keys=True) for e in _events_of(adapter, run_id)
    )
    serialized_trace = "\n".join(adapter.trace_payloads())
    for marker in (REDACT_DELIVERED_CONTENT, REDACT_QUEUED_CONTENT):
        if marker in serialized_events:
            raise ConformanceFailure("interaction content leaked into queue events")
        if marker in serialized_trace:
            raise ConformanceFailure("interaction content leaked into Trace")


async def run_utf8_size_limits(adapter: InteractionAdapter) -> None:
    """The UTF-8 byte limit is enforced at the boundary, and accepted
    multi-byte content is preserved exactly after validation."""
    limits = adapter.interaction_limits
    if limits.max_content_bytes < 4:
        raise ConformanceFailure("the kit requires max_content_bytes >= 4 for this scenario")
    exact = "é" * (limits.max_content_bytes // 2)
    over = exact + "é"
    if len(exact.encode("utf-8")) > limits.max_content_bytes:
        raise ConformanceFailure("kit bug: the boundary content must fit the byte limit")
    if len(over.encode("utf-8")) <= limits.max_content_bytes:
        raise ConformanceFailure("kit bug: the overflow content must exceed the byte limit")
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=0)
    await adapter.turn_end()
    receipt = _require_receipt(await adapter.steer(exact), "content at the byte boundary")
    _require_error(
        await adapter.steer(over), ERROR_CONTENT_TOO_LARGE, "content over the byte limit"
    )
    outcome = await adapter.terminal_candidate()
    if outcome != "continued":
        raise ConformanceFailure("the boundary content must continue the Run")
    delivered, _, _ = _delivery_sequence(adapter, run_id, receipt.input_id)
    message_id = delivered.data.get("message_id")
    committed = [m for m in adapter.committed_messages() if m.message_id == message_id]
    if len(committed) != 1 or committed[0].content != exact:
        raise ConformanceFailure("accepted multi-byte content must be preserved exactly")


async def run_queue_capacity(adapter: InteractionAdapter) -> None:
    """The pending limit counts both queues together; the next admission fails
    with ``interaction_queue_full`` and every accepted item still clears with
    evidence."""
    limits = adapter.interaction_limits
    if limits.max_pending_items < 2:
        raise ConformanceFailure("the kit requires max_pending_items >= 2 for this scenario")
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=0)
    await adapter.turn_end()
    receipts = [
        _require_receipt(await adapter.steer(f"fill-{i}"), f"filling item {i}")
        for i in range(limits.max_pending_items)
    ]
    if [r.position for r in receipts] != list(range(1, limits.max_pending_items + 1)):
        raise ConformanceFailure("positions must be 1-based and strictly increasing")
    _require_error(
        await adapter.follow_up("overflow-follow-up"),
        ERROR_QUEUE_FULL,
        "admission beyond the total pending limit",
    )
    snapshot = adapter.snapshot()
    if snapshot.pending_steering_count != limits.max_pending_items:
        raise ConformanceFailure("the snapshot must report the exact pending steering count")
    if snapshot.pending_follow_up_count != 0 or len(snapshot.pending) != limits.max_pending_items:
        raise ConformanceFailure("the snapshot must report the exact pending follow-up count")
    await adapter.terminate("cancelled")
    events = _events_of(adapter, run_id)
    cleared = [e for e in events if e.type == "interaction_cleared"]
    if len(cleared) != 1 or cleared[0].data.get("reason") != "cancelled":
        raise ConformanceFailure("the terminal clear must be one cancelled event")
    cleared_ids = list(cleared[0].data.get("input_ids", ()))
    delivered_ids = [
        e.data.get("input_id") for e in events if e.type == "interaction_delivered"
    ]
    evidence = delivered_ids + cleared_ids
    if len(evidence) != limits.max_pending_items or len(set(evidence)) != limits.max_pending_items:
        raise ConformanceFailure(
            "every accepted undelivered item must carry exactly-once delivery-or-clear evidence"
        )
    if set(evidence) != {r.input_id for r in receipts}:
        raise ConformanceFailure("evidence must cover exactly the accepted items")


async def run_invalid_content(adapter: InteractionAdapter) -> None:
    """Empty, whitespace-only, non-string, and non-encodable content is
    rejected as ``interaction_content_invalid``; valid content is accepted and
    preserved exactly, including surrounding whitespace."""
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=0)
    await adapter.turn_end()
    for bad in ("", "   ", "\n\t", 42, True, "\ud800"):
        _require_error(
            await adapter.steer(cast(Any, bad)), ERROR_CONTENT_INVALID, f"invalid content {bad!r}"
        )
    _require_error(
        await adapter.steer("valid-content", origin=cast(Any, "voice")),
        ERROR_CONTENT_INVALID,
        "invalid origin literal",
    )
    receipt = _require_receipt(
        await adapter.steer("  keep-this-exact  "), "valid content with surrounding whitespace"
    )
    outcome = await adapter.terminal_candidate()
    if outcome != "continued":
        raise ConformanceFailure("the valid content must continue the Run")
    delivered, _, _ = _delivery_sequence(adapter, run_id, receipt.input_id)
    message_id = delivered.data.get("message_id")
    committed = [m for m in adapter.committed_messages() if m.message_id == message_id]
    if len(committed) != 1 or committed[0].content != "  keep-this-exact  ":
        raise ConformanceFailure("content must not be trimmed before becoming a UserMessage")


async def run_immutable_snapshots(adapter: InteractionAdapter) -> None:
    """``snapshot`` returns an immutable point-in-time copy: the pending
    collection is a tuple, the dataclass is frozen, and an earlier snapshot
    never reflects later admissions."""
    await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=0)
    await adapter.turn_end()
    before = adapter.snapshot()
    receipt = _require_receipt(await adapter.steer("immutable-1"), "steer after the first snapshot")
    after = adapter.snapshot()
    if before.pending_steering_count != 0 or before.pending_follow_up_count != 0 or before.pending:
        raise ConformanceFailure("a snapshot is an immutable point-in-time copy, not a live view")
    if after.pending_steering_count != 1 or after.pending_follow_up_count != 0:
        raise ConformanceFailure("the later snapshot must reflect the new admission")
    if not isinstance(after.pending, tuple):
        raise ConformanceFailure("snapshot pending items must be a tuple")
    if len(after.pending) != 1 or after.pending[0] != receipt:
        raise ConformanceFailure("the snapshot must carry the pending receipt")
    try:
        setattr(after, "pending_steering_count", 99)
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise ConformanceFailure("a snapshot must be frozen")


async def run_admission_gate(adapter: InteractionAdapter) -> None:
    """Admission without an active Run is ``run_not_active`` and creates no
    events; admission opens when the Run starts and the snapshot always
    reports the exact configured modes."""
    for call in (adapter.steer("no-run-steer"), adapter.follow_up("no-run-follow-up")):
        result = await call
        _require_error(result, ERROR_RUN_NOT_ACTIVE, "admission without an active Run")
    if adapter.events():
        raise ConformanceFailure("a rejected admission must not create events")
    snapshot = adapter.snapshot()
    if snapshot.steering_mode not in ("one-at-a-time", "all"):
        raise ConformanceFailure("the snapshot must report an exact steering mode")
    if snapshot.follow_up_mode not in ("one-at-a-time", "all"):
        raise ConformanceFailure("the snapshot must report an exact follow-up mode")
    if snapshot.pending_steering_count != 0 or snapshot.pending_follow_up_count != 0:
        raise ConformanceFailure("the snapshot must start empty")
    run_id = await adapter.start_run("prompt-1")
    receipt = _require_receipt(await adapter.steer("ok-after-start"), "steer after the Run starts")
    if receipt.run_id != run_id:
        raise ConformanceFailure("the receipt must carry the active run id")


async def run_wait_for_idle_settlement(adapter: InteractionAdapter) -> None:
    """``wait_for_idle`` does not resolve while the queue is unsettled and
    resolves after settlement; the settled receipt carries exactly-once
    delivery-or-clear evidence."""
    run_id = await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=0)
    await adapter.turn_end()
    receipt = _require_receipt(await adapter.steer("settle-1"), "steer before settlement")
    idle_task = asyncio.create_task(adapter.wait_for_idle())
    await asyncio.sleep(0)
    if idle_task.done():
        raise ConformanceFailure("wait_for_idle must not resolve while the queue is unsettled")
    await adapter.terminate("closed")
    await _await_bounded(idle_task, what="wait_for_idle after settlement")
    events = _events_of(adapter, run_id)
    cleared = [e for e in events if e.type == "interaction_cleared"]
    if len(cleared) != 1 or cleared[0].data.get("reason") != "closed":
        raise ConformanceFailure("the terminal clear must be one closed event")
    agent_ends = [e for e in events if e.type == "agent_end"]
    if len(agent_ends) != 1 or agent_ends[0].data.get("outcome") != "closed":
        raise ConformanceFailure("agent_end must report the closed outcome")
    _assert_strictly_before(events, cleared[0], agent_ends[0], "cleared before agent_end")
    evidence = sum(
        1
        for e in events
        if (e.type == "interaction_delivered" and e.data.get("input_id") == receipt.input_id)
        or (e.type == "interaction_cleared" and receipt.input_id in e.data.get("input_ids", ()))
    )
    if evidence != 1:
        raise ConformanceFailure("the settled receipt must carry exactly-once delivery-or-clear evidence")


# ---------------------------------------------------------------------------
# Scenario registry and Integration entry point
# ---------------------------------------------------------------------------

InteractionScenarioFn: TypeAlias = Callable[[InteractionAdapter], Coroutine[Any, Any, None]]
AdapterFactory: TypeAlias = Callable[[], InteractionAdapter]

INTERACTION_SCENARIOS: dict[str, InteractionScenarioFn] = {
    "initial admission safe point": run_initial_admission_safe_point,
    "steering during model stream": run_steering_during_model_stream,
    "steering during first sibling tool": run_steering_during_first_sibling_tool,
    "steering during last sibling tool": run_steering_during_last_sibling_tool,
    "steering during confirmation wait": run_steering_during_confirmation_wait,
    "steering after turn_end drain": run_steering_after_turn_end_drain,
    "steering before next model call": run_steering_before_next_model_call,
    "follow-up during tool continuation": run_follow_up_during_tool_continuation,
    "steering priority over follow-up": run_steering_priority_over_follow_up,
    "no-tool completion follow-up": run_no_tool_completion_follow_up,
    "graceful terminate override": run_graceful_terminate_override,
    "graceful terminate seals": run_graceful_terminate_seals,
    "atomic drain snapshot": run_atomic_drain_snapshot,
    "FIFO ordering": run_fifo_ordering,
    "enqueue vs terminal seal": run_enqueue_vs_terminal_seal,
    "terminal priority": run_terminal_priority,
    "retry is not a turn": run_retry_is_not_a_turn,
    "event ordering": run_event_ordering,
    "exactly-once input ids": run_exactly_once_input_ids,
    "session projection": run_session_projection,
    "trace redaction": run_trace_redaction,
    "utf-8 size limits": run_utf8_size_limits,
    "queue capacity": run_queue_capacity,
    "invalid content": run_invalid_content,
    "immutable snapshots": run_immutable_snapshots,
    "admission gate": run_admission_gate,
    "wait for idle settlement": run_wait_for_idle_settlement,
}


def run_conformance(adapter_factory: AdapterFactory) -> dict[str, str]:
    """Run the full scenario battery against one adapter factory.

    Integration calls this entry point with a factory that builds a fresh
    production adapter (default construction settings) and asserts every
    scenario reports ``"ok"``.  The kit itself never imports production code
    and this function never modifies Lane files.

    Returns a mapping of scenario name to ``"ok"`` or a failure/error report.
    """
    results: dict[str, str] = {}
    for name, scenario in INTERACTION_SCENARIOS.items():
        adapter = adapter_factory()
        try:
            asyncio.run(scenario(adapter))
        except ConformanceFailure as exc:
            results[name] = f"FAIL: {exc}"
        except Exception as exc:  # defensive: a broken factory must not hide the battery
            results[name] = f"ERROR: {type(exc).__name__}: {exc}"
        else:
            results[name] = "ok"
    return results
