"""Bounded live Event Stream with monotonic sequence numbers.

Sequence numbers start at 1 and are monotonic per server process. Retained
history is bounded by ``capacity``; a cursor older than the retained window
raises ``EventCursorExpiredError`` and never silently skips. Subscribers
receive retained events followed by live events without a gap or duplicate
because registration snapshots the retained history and the live cursor under
one lock. Slow subscribers use bounded queues and are failed explicitly rather
than blocking event production. Closing wakes subscribers and rejects future
publish operations.

``publish`` is synchronous and is intended to be called from the event-loop
thread that owns the subscribers (the harness loop). When called from another
thread it is routed to each subscriber's loop via ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import threading
import uuid as uuid_module
from collections import deque
from collections.abc import AsyncIterator
from itertools import count
from typing import TypeAlias

from evopi.core.events import CoreEvent
from evopi.core.types import JsonObject

from .codec import to_event_data
from .errors import (
    EventCursorExpiredError,
    EventCursorInvalidError,
    EventPublishAfterCloseError,
    EventStreamClosedError,
    EventSubscriberDroppedError,
)
from .protocol import RpcEvent

DEFAULT_CAPACITY = 1000
DEFAULT_SUBSCRIBER_QUEUE_CAPACITY = 100

_SubscriberQueue: TypeAlias = asyncio.Queue[RpcEvent | None]


class _Subscriber:
    """One live subscription with its bounded queue and failure flag."""

    _ids = count(1)

    def __init__(self, *, loop: asyncio.AbstractEventLoop, queue_capacity: int) -> None:
        self.subscription_id = next(_Subscriber._ids)
        self.loop = loop
        self.queue: _SubscriberQueue = asyncio.Queue(maxsize=queue_capacity)
        self.failed = False


class EventStream:
    """Bounded, same-process event history with replay and live subscription."""

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_CAPACITY,
        subscriber_queue_capacity: int = DEFAULT_SUBSCRIBER_QUEUE_CAPACITY,
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        if (
            isinstance(subscriber_queue_capacity, bool)
            or not isinstance(subscriber_queue_capacity, int)
            or subscriber_queue_capacity <= 0
        ):
            raise ValueError("subscriber_queue_capacity must be a positive integer")
        self._events: deque[RpcEvent] = deque(maxlen=capacity)
        self._next_sequence = 1
        self._closed = False
        self._lock = threading.RLock()
        self._subscribers: dict[int, _Subscriber] = {}
        self._subscriber_queue_capacity = subscriber_queue_capacity

    def publish(self, event: CoreEvent) -> RpcEvent:
        """Publish a Core event and return its RpcEvent with the next sequence.

        The event data is strictly converted; an unsupported value raises
        ``RpcEventDataError`` and nothing is published.
        """
        data: JsonObject = event.data
        converted = {key: to_event_data(value) for key, value in data.items()}
        with self._lock:
            if self._closed:
                raise EventPublishAfterCloseError("event stream is closed")
            rpc_event = RpcEvent(
                event_id=str(uuid_module.uuid4()),
                sequence=self._next_sequence,
                type=event.type,
                data=converted,
                run_id=event.run_id,
                created_at=event.created_at,
            )
            self._next_sequence += 1
            self._events.append(rpc_event)
            for subscriber in list(self._subscribers.values()):
                self._put(subscriber, rpc_event)
        return rpc_event

    def replay(self, *, after_sequence: int) -> tuple[RpcEvent, ...]:
        """Return all retained events with sequence greater than the cursor."""
        self._check_cursor(after_sequence)
        with self._lock:
            if self._closed:
                raise EventStreamClosedError("event stream is closed")
            self._check_expired(after_sequence)
            return tuple(event for event in self._events if event.sequence > after_sequence)

    async def subscribe(self, *, after_sequence: int) -> AsyncIterator[RpcEvent]:
        """Subscribe: retained events after the cursor, then live events.

        Registration snapshots retained history and the live cursor under the
        same lock, so the handoff has no gap and no duplicate. Returns an
        async iterator; callers iterate ``await stream.subscribe(...)``.
        """
        self._check_cursor(after_sequence)
        with self._lock:
            if self._closed:
                raise EventStreamClosedError("event stream is closed")
            self._check_expired(after_sequence)
            replayed = tuple(event for event in self._events if event.sequence > after_sequence)
            subscriber = _Subscriber(
                loop=asyncio.get_running_loop(),
                queue_capacity=self._subscriber_queue_capacity,
            )
            self._subscribers[subscriber.subscription_id] = subscriber
        return self._iterate(subscriber, replayed)

    async def _iterate(
        self,
        subscriber: _Subscriber,
        replayed: tuple[RpcEvent, ...],
    ) -> AsyncIterator[RpcEvent]:
        try:
            for event in replayed:
                yield event
            while True:
                if subscriber.failed:
                    raise EventSubscriberDroppedError(
                        "subscriber dropped: bounded queue overflow"
                    )
                item = await subscriber.queue.get()
                if item is None:
                    return
                yield item
        finally:
            with self._lock:
                self._subscribers.pop(subscriber.subscription_id, None)

    def close(self) -> None:
        """Wake all subscribers, reject future publishes, and stay idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for subscriber in list(self._subscribers.values()):
                self._put(subscriber, None)
            self._subscribers.clear()

    def _check_cursor(self, after_sequence: int) -> None:
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int):
            raise EventCursorInvalidError("cursor must be an integer")
        if after_sequence < 0:
            raise EventCursorInvalidError("cursor must be non-negative")

    def _check_expired(self, after_sequence: int) -> None:
        if self._events and after_sequence < self._events[0].sequence - 1:
            raise EventCursorExpiredError("cursor is older than retained history")

    def _put(self, subscriber: _Subscriber, value: RpcEvent | None) -> None:
        loop = subscriber.loop
        if loop is not asyncio.get_running_loop():
            try:
                loop.call_soon_threadsafe(self._put_nowait, subscriber, value)
            except RuntimeError:
                pass  # the subscriber's loop is gone; the subscription is dead
            return
        self._put_nowait(subscriber, value)

    def _put_nowait(self, subscriber: _Subscriber, value: RpcEvent | None) -> None:
        try:
            subscriber.queue.put_nowait(value)
        except asyncio.QueueFull:
            subscriber.failed = True
            with self._lock:
                self._subscribers.pop(subscriber.subscription_id, None)


__all__ = ["DEFAULT_CAPACITY", "DEFAULT_SUBSCRIBER_QUEUE_CAPACITY", "EventStream"]
