"""Known-good reference adapter for the interaction conformance kit (SFU-3).

This adapter implements the Steering/Follow-up semantics frozen in the
steering-follow-up-v1 milestone CONTEXT.md sections 2-4 with minimal,
self-contained machinery and explicit virtual phases: one atomic
admission/finalization gate, FIFO per-kind queues with mode-aware draining
(``one-at-a-time`` vs ``all``), safe-point steering delivery after the full
sibling Tool batch and ``turn_end``, follow-up delivery only at terminal
candidates, terminal clearing fail closed for Abort/deadline/error/turn-limit/
cancelled/closed, Turn-budget accounting that excludes provider retries, and
Session/Trace separation that never duplicates content.

It never imports production modules; Integration binds the approved production
components to the kit Protocols instead.  Time is advanced by the kit through
explicit phases and ``asyncio.sleep(0)`` yields, so every scenario is
deterministic and never depends on wall-clock timing, real models, or the
network.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from .conformance import (
    AdmissionResult,
    CommittedMessage,
    ConformanceFailure,
    EventRecord,
    EventType,
    InteractionError,
    InteractionKind,
    InteractionLimits,
    InteractionMetadata,
    InteractionOrigin,
    InteractionQueueMode,
    InteractionQueueSnapshot,
    InteractionReceipt,
    TerminalReason,
    to_json_safe,
)

FIXED_TS: datetime = datetime(2026, 1, 1, tzinfo=UTC)

_STEERING_MODES: tuple[InteractionQueueMode, ...] = ("one-at-a-time", "all")
_ORIGINS: tuple[InteractionOrigin, ...] = ("api", "rpc", "repl")
_TERMINAL_REASONS: tuple[TerminalReason, ...] = (
    "aborted",
    "deadline_exceeded",
    "error",
    "turn_limit",
    "cancelled",
    "closed",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class _PendingItem:
    input_id: str
    run_id: str
    kind: InteractionKind
    origin: InteractionOrigin
    content: str
    created_at: datetime
    position: int


def _invalid(message: str) -> AdmissionResult:
    return AdmissionResult(
        error=InteractionError(code="interaction_content_invalid", message=message)
    )


class ReferenceInteractionAdapter:
    """Deterministic virtual runtime implementing the frozen semantics.

    The virtual phase machine is: idle -> run_active (admission open) ->
    model_stream -> tool steps / turn_end safe point -> terminal candidate
    (drain or seal) -> ended.  Retries, terminal reasons, and multi-run
    support follow the CONTEXT.md contract exactly; the adapter never sleeps
    on wall-clock time.
    """

    def __init__(
        self,
        *,
        steering_mode: InteractionQueueMode = "one-at-a-time",
        follow_up_mode: InteractionQueueMode = "one-at-a-time",
        limits: InteractionLimits | None = None,
    ) -> None:
        if steering_mode not in _STEERING_MODES:
            raise ValueError(f"unknown steering_mode {steering_mode!r}")
        if follow_up_mode not in _STEERING_MODES:
            raise ValueError(f"unknown follow_up_mode {follow_up_mode!r}")
        self._steering_mode = steering_mode
        self._follow_up_mode = follow_up_mode
        self._limits = limits if limits is not None else InteractionLimits()
        self._run_id: str | None = None
        self._run_index = 0
        self._admission_open = False
        self._ended = True
        self._steering_queue: list[_PendingItem] = []
        self._follow_up_queue: list[_PendingItem] = []
        self._position = 0
        self._turns = 0
        self._attempts = 0
        self._sequence = 0
        self._message_counter = 0
        self._events: list[EventRecord] = []
        self._committed: list[CommittedMessage] = []
        self._trace: list[str] = []
        self._tools: list[str] = []
        self._tools_pending = 0
        self._turn_had_tools = False
        self._decision_event: asyncio.Event | None = None
        self._confirmation_approved = False
        self._terminate_requested = False
        self._idle_event = asyncio.Event()
        self._idle_event.set()

    # -- observation --------------------------------------------------------

    @property
    def interaction_limits(self) -> InteractionLimits:
        return self._limits

    def snapshot(self) -> InteractionQueueSnapshot:
        pending = tuple(
            InteractionReceipt(
                input_id=item.input_id,
                run_id=item.run_id,
                kind=item.kind,
                origin=item.origin,
                created_at=item.created_at,
                position=item.position,
            )
            for item in sorted(
                self._steering_queue + self._follow_up_queue, key=lambda i: i.position
            )
        )
        return InteractionQueueSnapshot(
            steering_mode=self._steering_mode,
            follow_up_mode=self._follow_up_mode,
            pending_steering_count=len(self._steering_queue),
            pending_follow_up_count=len(self._follow_up_queue),
            pending=pending,
        )

    def events(self) -> tuple[EventRecord, ...]:
        return tuple(self._events)

    def committed_messages(self) -> tuple[CommittedMessage, ...]:
        return tuple(self._committed)

    def trace_payloads(self) -> tuple[str, ...]:
        return tuple(self._trace)

    def tool_executions(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def turn_count(self) -> int:
        return self._turns

    def attempt_count(self) -> int:
        return self._attempts

    # -- admission ----------------------------------------------------------

    async def steer(self, content: str, *, origin: InteractionOrigin = "api") -> AdmissionResult:
        return self._admit("steer", content, origin)

    async def follow_up(
        self, content: str, *, origin: InteractionOrigin = "api"
    ) -> AdmissionResult:
        return self._admit("follow_up", content, origin)

    def _admit(self, kind: InteractionKind, content: Any, origin: Any) -> AdmissionResult:
        """Atomic admission gate: run check, seal check, then admission."""
        if self._run_id is None:
            return AdmissionResult(
                error=InteractionError(code="run_not_active", message="no active Run")
            )
        if not self._admission_open:
            return AdmissionResult(
                error=InteractionError(
                    code="interaction_closed", message="the admission gate is sealed"
                )
            )
        return self._admit_open(kind, content, origin)

    def _admit_open(self, kind: InteractionKind, content: Any, origin: Any) -> AdmissionResult:
        """Structural validation, capacity check, queueing, and emission."""
        if not isinstance(content, str):
            return _invalid("content must be a string")
        if not content.strip():
            return _invalid("content must contain non-whitespace text")
        try:
            size = len(content.encode("utf-8"))
        except UnicodeEncodeError:
            return _invalid("content is not encodable UTF-8 text")
        if size > self._limits.max_content_bytes:
            return AdmissionResult(
                error=InteractionError(
                    code="interaction_content_too_large",
                    message="content exceeds the byte limit",
                )
            )
        if origin not in _ORIGINS:
            return _invalid("origin must be exactly one of api, rpc, repl")
        if len(self._steering_queue) + len(self._follow_up_queue) >= self._limits.max_pending_items:
            return AdmissionResult(
                error=InteractionError(
                    code="interaction_queue_full", message="pending item limit reached"
                )
            )
        self._position += 1
        item = _PendingItem(
            input_id=f"in-{self._position}",
            run_id=cast(str, self._run_id),
            kind=kind,
            origin=cast(InteractionOrigin, origin),
            content=content,
            created_at=FIXED_TS,
            position=self._position,
        )
        queue = self._steering_queue if kind == "steer" else self._follow_up_queue
        queue.append(item)
        self._emit(
            "interaction_queued",
            {
                "input_id": item.input_id,
                "kind": item.kind,
                "origin": item.origin,
                "position": item.position,
                "mode": self._steering_mode if kind == "steer" else self._follow_up_mode,
                "pending_steering_count": len(self._steering_queue),
                "pending_follow_up_count": len(self._follow_up_queue),
            },
        )
        return AdmissionResult(
            receipt=InteractionReceipt(
                input_id=item.input_id,
                run_id=item.run_id,
                kind=item.kind,
                origin=item.origin,
                created_at=item.created_at,
                position=item.position,
            )
        )

    # -- virtual phase advancement -----------------------------------------

    async def start_run(self, prompt: str) -> str:
        """Open admission, commit the Prompt, drain the initial safe point,
        then open the first model Turn."""
        if self._run_id is not None and not self._ended:
            raise ConformanceFailure("start_run while a Run is still active")
        self._run_index += 1
        self._run_id = f"run-{self._run_index}"
        self._position = 0
        self._turns = 0
        self._attempts = 0
        self._steering_queue.clear()
        self._follow_up_queue.clear()
        self._tools_pending = 0
        self._turn_had_tools = False
        self._decision_event = None
        self._confirmation_approved = False
        self._terminate_requested = False
        self._admission_open = True
        self._ended = False
        self._idle_event.clear()
        await asyncio.sleep(0)
        self._message_counter += 1
        prompt_id = f"msg-{self._message_counter}"
        self._emit("message_start", {"message_id": prompt_id, "role": "user"})
        self._emit("message_end", {"message_id": prompt_id, "role": "user"})
        self._committed.append(
            CommittedMessage(message_id=prompt_id, role="user", content=prompt, interaction=None)
        )
        drained = await self._drain("steer")
        if not drained:
            self._emit("turn_start", {"turn_number": 1})
        self._turns = 1
        return self._run_id

    async def model_stream(self, *, tool_calls: int = 0, retry: bool = False) -> str:
        """Deliver one model response; ``retry=True`` is a retried Model Attempt.

        The stream window (one event-loop yield) is where scenarios queue
        steering mid-stream; the commit happens afterwards, so an admission
        during the window is genuinely queued before the message commits.
        """
        self._attempts += 1
        await asyncio.sleep(0)
        self._message_counter += 1
        message_id = f"msg-{self._message_counter}"
        self._emit("message_start", {"message_id": message_id, "role": "assistant"})
        self._emit("message_end", {"message_id": message_id, "role": "assistant"})
        self._committed.append(
            CommittedMessage(message_id=message_id, role="assistant", content="", interaction=None)
        )
        self._tools_pending = tool_calls
        self._turn_had_tools = tool_calls > 0
        return message_id

    async def tool_step(self, index: int, *, confirmation: bool = False) -> None:
        """Execute one sibling ToolCall of the current Assistant message.

        ``confirmation=True`` blocks until ``decide`` resolves the pending
        decision; a denied Tool never executes and records nothing.
        """
        if confirmation:
            event = asyncio.Event()
            self._decision_event = event
            await asyncio.sleep(0)
            await event.wait()
            self._decision_event = None
            if not self._confirmation_approved:
                self._tools_pending -= 1
                return
            self._tools.append(f"tool-{index}")
        else:
            await asyncio.sleep(0)
            self._tools.append(f"tool-{index}")
        self._tools_pending -= 1

    async def decide(self, *, approved: bool) -> None:
        """Resolve the currently pending Confirmation decision."""
        if self._decision_event is None:
            raise ConformanceFailure("decide without a pending confirmation")
        self._confirmation_approved = approved
        self._decision_event.set()

    async def turn_end(self, *, terminate: bool = False) -> str:
        """Complete after-turn processing and drain the steering safe point.

        Returns ``"continued"`` when the Run proceeds (steering drained or the
        turn executed sibling Tools) and ``"stop"`` when it reaches a terminal
        candidate.  ``terminate=True`` requests the graceful terminate batch.
        """
        if self._tools_pending != 0:
            raise ConformanceFailure("turn_end while sibling Tools are still pending")
        if terminate:
            self._terminate_requested = True
        self._emit("turn_end", {"turn_number": self._turns})
        drained = await self._drain("steer")
        if drained:
            return "continued"
        if self._turn_had_tools:
            self._emit("turn_start", {"turn_number": self._turns + 1})
            self._turns += 1
            return "continued"
        return "stop"

    async def terminal_candidate(self) -> str:
        """Atomic terminal gate: drain queued input or seal admission.

        Steering has priority over follow-up.  When nothing is queued the seal
        wins synchronously (no internal yield), so a concurrently queued
        admission deterministically observes ``interaction_closed``.
        """
        if self._tools_pending != 0:
            raise ConformanceFailure("terminal_candidate while sibling Tools are still pending")
        if self._steering_queue:
            await self._drain("steer")
            return "continued"
        if self._follow_up_queue:
            await self._drain("follow_up")
            return "continued"
        return self._seal()

    def _seal(self) -> str:
        outcome = "terminated" if self._terminate_requested else "completed"
        self._admission_open = False
        self._ended = True
        self._emit("agent_end", {"outcome": outcome})
        self._idle_event.set()
        return outcome

    async def terminate(self, reason: TerminalReason) -> None:
        """Clear every undelivered interaction fail closed with the reason."""
        if reason not in _TERMINAL_REASONS:
            raise ValueError(f"unknown terminal reason {reason!r}")
        pending = sorted(
            self._steering_queue + self._follow_up_queue, key=lambda item: item.position
        )
        self._emit(
            "interaction_cleared",
            {
                "reason": reason,
                "count": len(pending),
                "input_ids": [item.input_id for item in pending],
                "kinds": [item.kind for item in pending],
            },
        )
        self._steering_queue.clear()
        self._follow_up_queue.clear()
        self._admission_open = False
        self._ended = True
        self._emit("agent_end", {"outcome": reason})
        self._idle_event.set()

    async def wait_for_idle(self) -> None:
        """Resolve only after queue settlement and awaited ``agent_end``."""
        await self._idle_event.wait()

    # -- queue machinery ----------------------------------------------------

    async def _drain(self, kind: InteractionKind) -> int:
        """Drain one batch per the queue mode; return the number delivered."""
        if kind == "steer":
            return await self._drain_steering()
        return await self._drain_follow_up()

    async def _drain_steering(self) -> int:
        return await self._drain_kind(self._steering_mode, self._steering_queue)

    async def _drain_follow_up(self) -> int:
        return await self._drain_kind(self._follow_up_mode, self._follow_up_queue)

    async def _drain_kind(
        self, mode: InteractionQueueMode, queue: list[_PendingItem]
    ) -> int:
        if not queue:
            return 0
        if mode == "one-at-a-time":
            batch = [queue.pop(0)]
        else:
            batch = list(queue)  # one atomic FIFO snapshot
            queue.clear()
        for item in batch:
            await asyncio.sleep(0)
            self._deliver(item)
        self._emit("turn_start", {"turn_number": self._turns + 1})
        self._turns += 1
        return len(batch)

    def _deliver(self, item: _PendingItem) -> None:
        """Commit one delivered item as a normal UserMessage with metadata."""
        self._message_counter += 1
        message_id = f"msg-{self._message_counter}"
        self._emit(
            "interaction_delivered",
            {
                "input_id": item.input_id,
                "kind": item.kind,
                "origin": item.origin,
                "message_id": message_id,
                "mode": self._steering_mode if item.kind == "steer" else self._follow_up_mode,
                "remaining_steering_count": len(self._steering_queue),
                "remaining_follow_up_count": len(self._follow_up_queue),
            },
        )
        self._emit("message_start", {"message_id": message_id, "role": "user"})
        self._emit("message_end", {"message_id": message_id, "role": "user"})
        metadata = InteractionMetadata(
            schema_version=1,
            input_id=item.input_id,
            kind=item.kind,
            origin=item.origin,
            created_at=item.created_at,
        )
        self._committed.append(
            CommittedMessage(
                message_id=message_id, role="user", content=item.content, interaction=metadata
            )
        )
        self._trace.append(json.dumps(to_json_safe(dataclasses.asdict(metadata)), sort_keys=True))

    def _emit(self, type_: EventType, data: dict[str, Any]) -> None:
        self._sequence += 1
        self._events.append(
            EventRecord(
                sequence=self._sequence,
                type=type_,
                run_id=cast(str, self._run_id),
                data=data,
            )
        )
        self._trace.append(json.dumps(to_json_safe(data), sort_keys=True))
