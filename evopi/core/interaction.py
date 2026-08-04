"""Steering and follow-up interaction queue protocol.

Steering and follow-up are direct host inputs delivered inside one active
Run.  This module owns the frozen public types, strict validation, the FIFO
queues with independent modes, and the single atomic admission / terminal-seal
gate.  Delivery happens only at safe points driven by :class:`AgentLoop`;
this module never schedules model calls or cancels anything.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, TypeAlias, cast
from uuid import uuid4

from evopi.core.cancellation import AbortSignal
from evopi.core.events import CoreEvent, EventListener, notify
from evopi.core.messages import UserMessage

InteractionKind: TypeAlias = Literal["steer", "follow_up"]
InteractionQueueMode: TypeAlias = Literal["one-at-a-time", "all"]
InteractionOrigin: TypeAlias = Literal["api", "rpc", "repl"]

_INTERACTION_KINDS: tuple[str, ...] = ("steer", "follow_up")
_INTERACTION_QUEUE_MODES: tuple[str, ...] = ("one-at-a-time", "all")
_INTERACTION_ORIGINS: tuple[str, ...] = ("api", "rpc", "repl")
_INTERACTION_METADATA_SCHEMA_VERSION = 1


class InteractionError(RuntimeError):
    """Base error for the interaction queue protocol."""


class InteractionQueueClosedError(InteractionError):
    """No Run is accepting interactions (idle or already sealed)."""


class InteractionQueueFullError(InteractionError):
    """The combined pending-item capacity is exhausted."""


class InteractionContentError(InteractionError):
    """Content is not a string or contains no non-whitespace text."""


class InteractionContentTooLargeError(InteractionContentError):
    """Content exceeds the configured UTF-8 size limit."""


class InteractionModeError(InteractionError):
    """A kind, origin, or queue mode is not one of the exact literals."""


@dataclass(frozen=True, slots=True, kw_only=True)
class InteractionLimits:
    max_pending_items: int = 100
    max_content_bytes: int = 65_536


@dataclass(frozen=True, slots=True, kw_only=True)
class InteractionReceipt:
    """Immutable acknowledgement of one admitted interaction.

    Receipts never carry content; only the delivered ``UserMessage`` does.
    """

    input_id: str
    run_id: str
    kind: InteractionKind
    origin: InteractionOrigin
    created_at: datetime
    position: int


@dataclass(frozen=True, slots=True, kw_only=True)
class InteractionQueueSnapshot:
    """Immutable point-in-time view of the queue without raw content."""

    steering_mode: InteractionQueueMode
    follow_up_mode: InteractionQueueMode
    pending_steering_count: int
    pending_follow_up_count: int
    pending: tuple[InteractionReceipt, ...]


@dataclass(slots=True)
class _PendingInteraction:
    input_id: str
    kind: InteractionKind
    origin: InteractionOrigin
    created_at: datetime
    position: int
    content: str
    mode: InteractionQueueMode


def validate_content(content: object, *, max_bytes: int) -> str:
    """Validate interaction content strictly and return it unchanged.

    Content must be a string with non-whitespace text whose UTF-8 size fits
    ``max_bytes``.  The accepted string is preserved exactly; it is never
    trimmed before becoming a ``UserMessage``.
    """
    if not isinstance(content, str):
        raise InteractionContentError("Interaction content must be a string")
    if not content.strip():
        raise InteractionContentError(
            "Interaction content must contain non-whitespace text"
        )
    size = len(content.encode("utf-8"))
    if size > max_bytes:
        raise InteractionContentTooLargeError(
            f"Interaction content is {size} UTF-8 bytes; limit is {max_bytes}"
        )
    return content


def _validate_mode(value: object, name: str) -> InteractionQueueMode:
    if value not in _INTERACTION_QUEUE_MODES:
        raise InteractionModeError(
            f"{name} must be one of {_INTERACTION_QUEUE_MODES}"
        )
    return cast(InteractionQueueMode, value)


def _validate_kind(value: object) -> InteractionKind:
    if value not in _INTERACTION_KINDS:
        raise InteractionModeError(
            f"Interaction kind must be one of {_INTERACTION_KINDS}"
        )
    return cast(InteractionKind, value)


def _validate_origin(value: object) -> InteractionOrigin:
    if value not in _INTERACTION_ORIGINS:
        raise InteractionModeError(
            f"Interaction origin must be one of {_INTERACTION_ORIGINS}"
        )
    return cast(InteractionOrigin, value)


def _positive_int(value: object, name: str) -> int:
    # Booleans are ints in Python but never satisfy integer fields here.
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


class InteractionQueueController:
    """FIFO steering/follow-up queues with one atomic admission/seal gate.

    The controller is bound to one Run at a time:

    - ``open(run_id)`` opens the admission gate when the Run starts.
    - ``admit`` validates and queues an item, or raises
      :class:`InteractionQueueClosedError` when the gate is closed and
      :class:`InteractionQueueFullError` when the combined capacity is full.
    - ``drain_steering`` / ``drain_follow_up_at_terminal`` deliver at the
      safe points driven by the Agent loop.
    - ``seal`` closes the gate and clears undelivered items fail closed with
      one observable ``interaction_cleared`` event.

    Admission and sealing share one synchronous gate: an admit that wins is
    guaranteed to be delivered or cleared with an observable terminal event;
    an admit that loses fails with ``InteractionQueueClosedError`` and creates
    no item and no queued event.
    """

    def __init__(
        self,
        *,
        steering_mode: InteractionQueueMode = "one-at-a-time",
        follow_up_mode: InteractionQueueMode = "one-at-a-time",
        limits: InteractionLimits | None = None,
    ) -> None:
        self._steering_mode = _validate_mode(steering_mode, "steering_mode")
        self._follow_up_mode = _validate_mode(follow_up_mode, "follow_up_mode")
        resolved_limits = limits if limits is not None else InteractionLimits()
        self._max_pending_items = _positive_int(
            resolved_limits.max_pending_items, "max_pending_items"
        )
        self._max_content_bytes = _positive_int(
            resolved_limits.max_content_bytes, "max_content_bytes"
        )
        self._steering: deque[_PendingInteraction] = deque()
        self._follow_up: deque[_PendingInteraction] = deque()
        self._position = 0
        self._sealed = True
        self._run_id: str | None = None

    @property
    def sealed(self) -> bool:
        return self._sealed

    @property
    def steering_mode(self) -> InteractionQueueMode:
        return self._steering_mode

    @property
    def follow_up_mode(self) -> InteractionQueueMode:
        return self._follow_up_mode

    def open(self, run_id: str) -> None:
        """Open the admission gate for a new Run.

        In normal operation the previous Run sealed before ending; any residue
        left by a crashed previous Run is dropped defensively before the gate
        opens.
        """
        if not self._sealed:
            self._steering.clear()
            self._follow_up.clear()
        self._sealed = False
        self._run_id = run_id
        self._position = 0

    def snapshot(self) -> InteractionQueueSnapshot:
        """Return an immutable view of the queue without raw content."""
        return InteractionQueueSnapshot(
            steering_mode=self._steering_mode,
            follow_up_mode=self._follow_up_mode,
            pending_steering_count=len(self._steering),
            pending_follow_up_count=len(self._follow_up),
            pending=tuple(
                self._receipt(item)
                for item in (*self._steering, *self._follow_up)
            ),
        )

    async def admit(
        self,
        kind: InteractionKind,
        origin: InteractionOrigin,
        content: object,
        *,
        emit: EventListener | None,
        signal: AbortSignal | None = None,
    ) -> InteractionReceipt:
        """Validate and queue one interaction, returning its receipt.

        Admission is async so the acknowledgement may include the
        ``interaction_queued`` lifecycle emission.  If an event listener
        fails, the accepted item stays authoritative (it will be delivered or
        cleared with an observable event) and the observer failure surfaces
        without duplicating the item.
        """
        valid_kind = _validate_kind(kind)
        origin_literal = _validate_origin(origin)
        text = validate_content(content, max_bytes=self._max_content_bytes)
        if self._sealed or self._run_id is None:
            raise InteractionQueueClosedError(
                "No Run is accepting interactions"
            )
        if self._pending_count() >= self._max_pending_items:
            raise InteractionQueueFullError(
                f"Interaction queue is full ({self._max_pending_items} pending)"
            )
        self._position += 1
        item = _PendingInteraction(
            input_id=uuid4().hex,
            kind=valid_kind,
            origin=origin_literal,
            created_at=datetime.now(UTC),
            position=self._position,
            content=text,
            mode=(
                self._steering_mode
                if valid_kind == "steer"
                else self._follow_up_mode
            ),
        )
        queue = self._steering if valid_kind == "steer" else self._follow_up
        queue.append(item)
        receipt = self._receipt(item)
        run_id_for_event = self._run_id
        await notify(
            emit,
            CoreEvent(
                type="interaction_queued",
                run_id=run_id_for_event,
                data={
                    "input_id": item.input_id,
                    "kind": item.kind,
                    "origin": item.origin,
                    "run_id": run_id_for_event,
                    "position": item.position,
                    "mode": item.mode,
                    "pending_steering_count": len(self._steering),
                    "pending_follow_up_count": len(self._follow_up),
                },
            ),
            signal=signal,
        )
        return receipt

    async def drain_steering(
        self,
        *,
        emit: EventListener | None,
        run_id: str | None,
        signal: AbortSignal | None = None,
        append: Callable[[UserMessage], None],
    ) -> tuple[UserMessage, ...]:
        """Deliver pending steering at a safe point.

        ``one-at-a-time`` delivers one FIFO item; ``all`` delivers the current
        FIFO snapshot as separate ``UserMessage`` objects.  Returns the
        delivered messages (empty when nothing was delivered).
        """
        if self._sealed or signal is not None and signal.aborted:
            return ()
        if not self._steering:
            return ()
        count = (
            1
            if self._steering_mode == "one-at-a-time"
            else len(self._steering)
        )
        return await self._drain(
            self._steering,
            count,
            emit=emit,
            run_id=run_id,
            signal=signal,
            append=append,
        )

    async def drain_follow_up_at_terminal(
        self,
        *,
        emit: EventListener | None,
        run_id: str | None,
        reason: str,
        signal: AbortSignal | None = None,
        append: Callable[[UserMessage], None],
    ) -> tuple[UserMessage, ...]:
        """Run the atomic terminal-candidate gate.

        With steering pending this is not a terminal candidate and nothing is
        drained.  Otherwise follow-up is drained according to its mode; when
        nothing is pending the gate seals admission atomically so the Run may
        end.  Returns the delivered messages.
        """
        if self._sealed or signal is not None and signal.aborted:
            return ()
        if self._steering:
            return ()
        if not self._follow_up:
            self._sealed = True
            return ()
        count = (
            1
            if self._follow_up_mode == "one-at-a-time"
            else len(self._follow_up)
        )
        return await self._drain(
            self._follow_up,
            count,
            emit=emit,
            run_id=run_id,
            signal=signal,
            append=append,
        )

    async def seal(
        self,
        *,
        emit: EventListener | None,
        run_id: str | None,
        reason: str,
        signal: AbortSignal | None = None,
    ) -> None:
        """Close the admission gate and clear undelivered items fail closed.

        Idempotent: a queue sealed by the terminal gate emits nothing again.
        Every cleared input ID appears in the ``interaction_cleared`` event.
        """
        if self._sealed:
            return
        self._sealed = True
        items = [*self._steering, *self._follow_up]
        self._steering.clear()
        self._follow_up.clear()
        if not items:
            return
        await notify(
            emit,
            CoreEvent(
                type="interaction_cleared",
                run_id=run_id,
                data={
                    "reason": reason,
                    "count": len(items),
                    "input_ids": [item.input_id for item in items],
                    "kinds": [item.kind for item in items],
                },
            ),
            signal=signal,
        )

    async def _drain(
        self,
        queue: deque[_PendingInteraction],
        count: int,
        *,
        emit: EventListener | None,
        run_id: str | None,
        signal: AbortSignal | None,
        append: Callable[[UserMessage], None],
    ) -> tuple[UserMessage, ...]:
        taken: list[_PendingInteraction] = []
        for _ in range(count):
            if not queue:
                break
            taken.append(queue.popleft())
        delivered: list[UserMessage] = []
        try:
            for item in taken:
                message = await self._deliver(
                    item, emit=emit, run_id=run_id, signal=signal, append=append
                )
                delivered.append(message)
        except Exception:
            # Restore undelivered items to the front so the failure seal can
            # clear them with an observable event; no acked input is stranded.
            for item in reversed(taken[len(delivered) :]):
                queue.appendleft(item)
            raise
        return tuple(delivered)

    async def _deliver(
        self,
        item: _PendingInteraction,
        *,
        emit: EventListener | None,
        run_id: str | None,
        signal: AbortSignal | None,
        append: Callable[[UserMessage], None],
    ) -> UserMessage:
        message = UserMessage(
            content=item.content,
            metadata={
                "interaction": {
                    "schema_version": _INTERACTION_METADATA_SCHEMA_VERSION,
                    "input_id": item.input_id,
                    "kind": item.kind,
                    "origin": item.origin,
                    "created_at": item.created_at.isoformat(),
                }
            },
        )
        await notify(
            emit,
            CoreEvent(
                type="interaction_delivered",
                run_id=run_id,
                data={
                    "input_id": item.input_id,
                    "kind": item.kind,
                    "origin": item.origin,
                    "run_id": run_id,
                    "message_id": message.id,
                    "mode": item.mode,
                    "pending_steering_count": len(self._steering),
                    "pending_follow_up_count": len(self._follow_up),
                },
            ),
            signal=signal,
        )
        await notify(
            emit,
            CoreEvent(
                type="message_start",
                run_id=run_id,
                data={"message_id": message.id, "role": message.role},
            ),
            signal=signal,
        )
        append(message)
        await notify(
            emit,
            CoreEvent(
                type="message_end",
                run_id=run_id,
                data={"message": message},
            ),
            signal=signal,
        )
        return message

    def _receipt(self, item: _PendingInteraction) -> InteractionReceipt:
        return InteractionReceipt(
            input_id=item.input_id,
            run_id=self._run_id or "",
            kind=item.kind,
            origin=item.origin,
            created_at=item.created_at,
            position=item.position,
        )

    def _pending_count(self) -> int:
        return len(self._steering) + len(self._follow_up)


__all__ = [
    "InteractionContentError",
    "InteractionContentTooLargeError",
    "InteractionError",
    "InteractionKind",
    "InteractionLimits",
    "InteractionModeError",
    "InteractionOrigin",
    "InteractionQueueClosedError",
    "InteractionQueueController",
    "InteractionQueueFullError",
    "InteractionQueueMode",
    "InteractionQueueSnapshot",
    "InteractionReceipt",
    "validate_content",
]
