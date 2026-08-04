"""Deterministic tests for the bounded RPC Event Stream."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from evopi.core.events import CoreEvent
from evopi.rpc import (
    EventCursorExpiredError,
    EventCursorInvalidError,
    EventPublishAfterCloseError,
    EventStreamClosedError,
    EventSubscriberDroppedError,
    RpcEvent,
    RpcEventDataError,
)
from evopi.rpc.event_stream import EventStream


def _event(kind: str = "turn_start", i: int = 0) -> CoreEvent:
    return CoreEvent(type=kind, data={"i": i})


async def _drain_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for async condition")
        await asyncio.sleep(0.01)


async def _collect_sequences(
    stream: EventStream,
    *,
    after_sequence: int,
    into: list[int],
) -> None:
    async for event in await stream.subscribe(after_sequence=after_sequence):
        into.append(event.sequence)


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for condition")
        time.sleep(0.01)


class TestPublish:
    def test_sequences_start_at_one_and_are_monotonic(self) -> None:
        stream = EventStream(capacity=10)
        try:
            first = stream.publish(_event())
            second = stream.publish(_event())
            assert first.sequence == 1
            assert second.sequence == 2
            assert isinstance(first, RpcEvent)
            assert UUID(first.event_id)
        finally:
            stream.close()

    def test_publish_converts_core_event_fields(self) -> None:
        stream = EventStream(capacity=10)
        try:
            created = datetime(2026, 8, 4, tzinfo=UTC)
            event = stream.publish(
                CoreEvent(
                    type="tool_execution_start",
                    data={"path": Path("x"), "n": 1},
                    run_id="run-1",
                    created_at=created,
                )
            )
            assert event.type == "tool_execution_start"
            assert event.data == {"path": str(Path("x")), "n": 1}
            assert event.run_id == "run-1"
            assert event.created_at == created
        finally:
            stream.close()

    def test_unsupported_data_value_is_protocol_error_and_not_published(self) -> None:
        stream = EventStream(capacity=10)
        try:
            with pytest.raises(RpcEventDataError) as excinfo:
                stream.publish(CoreEvent(type="error", data={"bad": object()}))
            assert "0x" not in str(excinfo.value)
            assert stream.replay(after_sequence=0) == ()
        finally:
            stream.close()

    def test_capacity_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            EventStream(capacity=0)
        with pytest.raises(ValueError):
            EventStream(capacity=-1)
        with pytest.raises(ValueError):
            EventStream(subscriber_queue_capacity=0)


class TestReplay:
    def test_replay_after_rollover_returns_retained_window(self) -> None:
        stream = EventStream(capacity=3)
        try:
            for i in range(5):
                stream.publish(_event(i=i))
            assert [e.sequence for e in stream.replay(after_sequence=2)] == [3, 4, 5]
            assert [e.sequence for e in stream.replay(after_sequence=3)] == [4, 5]
            assert [e.sequence for e in stream.replay(after_sequence=5)] == []
        finally:
            stream.close()

    def test_cursor_older_than_retention_is_expired(self) -> None:
        stream = EventStream(capacity=3)
        try:
            for i in range(5):
                stream.publish(_event(i=i))
            with pytest.raises(EventCursorExpiredError):
                stream.replay(after_sequence=1)
        finally:
            stream.close()

    def test_invalid_cursor_rejected(self) -> None:
        stream = EventStream(capacity=3)
        try:
            stream.publish(_event())
            with pytest.raises(EventCursorInvalidError):
                stream.replay(after_sequence=-1)  # type: ignore[arg-type]
            with pytest.raises(EventCursorInvalidError):
                stream.replay(after_sequence=2.5)  # type: ignore[arg-type]
        finally:
            stream.close()

    def test_empty_stream_replays_nothing(self) -> None:
        stream = EventStream(capacity=3)
        try:
            assert stream.replay(after_sequence=0) == ()
        finally:
            stream.close()


class TestSubscribe:
    def test_replay_then_live_handoff_has_no_gap_or_duplicate(self) -> None:
        async def scenario() -> None:
            stream = EventStream(capacity=100)
            received: list[int] = []

            async def consume() -> None:
                async for event in await stream.subscribe(after_sequence=1):
                    received.append(event.sequence)

            try:
                for i in range(3):
                    stream.publish(_event(i=i))
                task = asyncio.create_task(consume())
                stream.publish(_event(i=3))
                stream.publish(_event(i=4))
                await _drain_until(lambda: 5 in received)
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                assert received == [2, 3, 4, 5]
            finally:
                stream.close()

        asyncio.run(scenario())

    def test_multiple_subscribers_all_receive_live_events(self) -> None:
        async def scenario() -> None:
            stream = EventStream(capacity=10)
            seen_a: list[int] = []
            seen_b: list[int] = []

            async def consume_a() -> None:
                async for event in await stream.subscribe(after_sequence=0):
                    seen_a.append(event.sequence)

            async def consume_b() -> None:
                async for event in await stream.subscribe(after_sequence=0):
                    seen_b.append(event.sequence)

            try:
                stream.publish(_event(i=0))
                task_a = asyncio.create_task(consume_a())
                task_b = asyncio.create_task(consume_b())
                stream.publish(_event(i=1))
                await _drain_until(lambda: 2 in seen_a and 2 in seen_b)
                task_a.cancel()
                task_b.cancel()
                with suppress(asyncio.CancelledError):
                    await task_a
                with suppress(asyncio.CancelledError):
                    await task_b
                assert seen_a == [1, 2]
                assert seen_b == [1, 2]
            finally:
                stream.close()

        asyncio.run(scenario())

    def test_cursor_expired_on_subscribe(self) -> None:
        async def scenario() -> None:
            stream = EventStream(capacity=2)
            try:
                for i in range(4):
                    stream.publish(_event(i=i))
                with pytest.raises(EventCursorExpiredError):
                    async for _ in await stream.subscribe(after_sequence=1):
                        pass
            finally:
                stream.close()

        asyncio.run(scenario())

    def test_subscribe_after_close_is_rejected(self) -> None:
        async def scenario() -> None:
            stream = EventStream(capacity=3)
            stream.close()
            with pytest.raises(EventStreamClosedError):
                await stream.subscribe(after_sequence=0)

        asyncio.run(scenario())

    def test_slow_subscriber_is_dropped_without_blocking_publisher(self) -> None:
        async def scenario() -> None:
            stream = EventStream(capacity=100, subscriber_queue_capacity=2)
            events: list[RpcEvent] = []

            async def consume() -> None:
                async for event in await stream.subscribe(after_sequence=0):
                    events.append(event)

            try:
                stream.publish(_event(i=0))
                task = asyncio.create_task(consume())
                await _drain_until(lambda: len(events) == 1)  # consumer registered
                for i in range(1, 6):
                    stream.publish(_event(i=i))
                with pytest.raises(EventSubscriberDroppedError):
                    await task
                # The publisher itself never blocked; sequences kept advancing.
                assert [e.sequence for e in stream.replay(after_sequence=0)] == [1, 2, 3, 4, 5, 6]
            finally:
                stream.close()

        asyncio.run(scenario())

    def test_cancelled_consumer_is_removed_and_does_not_leak(self) -> None:
        async def scenario() -> None:
            stream = EventStream(capacity=10)

            async def consume() -> None:
                async for _event in await stream.subscribe(after_sequence=0):
                    pass

            try:
                task = asyncio.create_task(consume())
                await asyncio.sleep(0)
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                # Publishing no longer delivers anywhere and never errors.
                stream.publish(_event(i=0))
                assert len(stream.replay(after_sequence=0)) == 1
                # A fresh subscription still receives replayed and live events.
                received: list[int] = []
                task2 = asyncio.create_task(
                    _collect_sequences(stream, after_sequence=0, into=received)
                )
                stream.publish(_event(i=1))
                stream.publish(_event(i=2))
                await _drain_until(lambda: 3 in received)
                task2.cancel()
                with suppress(asyncio.CancelledError):
                    await task2
                assert received == [1, 2, 3]
            finally:
                stream.close()

        asyncio.run(scenario())


class TestClose:
    def test_close_wakes_subscribers(self) -> None:
        async def scenario() -> None:
            stream = EventStream(capacity=10)
            consumed: list[int] = []

            async def consume() -> None:
                async for event in await stream.subscribe(after_sequence=0):
                    consumed.append(event.sequence)

            try:
                task = asyncio.create_task(consume())
                stream.publish(_event(i=1))
                await _drain_until(lambda: 1 in consumed)  # consumer registered
                stream.close()
                await asyncio.wait_for(task, timeout=2.0)
                assert consumed == [1]
            finally:
                stream.close()

        asyncio.run(scenario())

    def test_close_is_idempotent(self) -> None:
        stream = EventStream(capacity=3)
        stream.publish(_event())
        stream.close()
        stream.close()

    def test_publish_after_close_rejected(self) -> None:
        stream = EventStream(capacity=3)
        stream.close()
        with pytest.raises(EventPublishAfterCloseError):
            stream.publish(_event())

    def test_replay_after_close_rejected(self) -> None:
        stream = EventStream(capacity=3)
        stream.close()
        with pytest.raises(EventStreamClosedError):
            stream.replay(after_sequence=0)


class TestCrossThreadPublish:
    """publish()/close() must work from threads without a running event loop."""

    def test_publish_from_foreign_thread_delivers_in_order(self) -> None:
        stream = EventStream(capacity=10, subscriber_queue_capacity=10)
        received: list[int] = []
        thread_error: list[BaseException] = []

        def run_loop() -> None:
            loop = asyncio.new_event_loop()
            try:

                async def consume() -> None:
                    async for event in await stream.subscribe(after_sequence=0):
                        received.append(event.sequence)
                        if len(received) == 3:
                            return

                loop.run_until_complete(consume())
            except BaseException as exc:  # pragma: no cover - failure reporting only
                thread_error.append(exc)

        thread = threading.Thread(target=run_loop)
        thread.start()
        try:
            _wait_until(lambda: len(stream._subscribers) == 1)  # subscriber registered
            # The main thread has no running loop: this exercises the
            # cross-thread delivery path for every publish.
            stream.publish(_event(i=1))
            stream.publish(_event(i=2))
            stream.publish(_event(i=3))
            thread.join(timeout=3.0)
            assert not thread.is_alive()
            assert thread_error == []
            assert received == [1, 2, 3]  # per-subscriber ordering preserved
        finally:
            stream.close()

    def test_close_from_foreign_thread_wakes_subscribers(self) -> None:
        stream = EventStream(capacity=10)
        received: list[int] = []

        def run_loop() -> None:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_collect_sequences(stream, after_sequence=0, into=received))

        thread = threading.Thread(target=run_loop)
        thread.start()
        try:
            _wait_until(lambda: len(stream._subscribers) == 1)
            stream.publish(_event(i=1))
            _wait_until(lambda: 1 in received)
            stream.close()  # no running loop in this thread: sentinel is routed
            thread.join(timeout=3.0)
            assert not thread.is_alive()
            assert received == [1]
        finally:
            stream.close()

    def test_publish_after_subscriber_loop_closed_removes_subscriber(self) -> None:
        stream = EventStream(capacity=10)
        loop = asyncio.new_event_loop()

        def run_loop() -> None:
            # Registers a subscriber (returns the iterator) but never iterates
            # it, so the subscription survives and the loop can be closed.
            loop.run_until_complete(stream.subscribe(after_sequence=0))

        thread = threading.Thread(target=run_loop)
        thread.start()
        thread.join(timeout=3.0)
        assert not thread.is_alive()
        loop.close()
        assert len(stream._subscribers) == 1
        # Publishing with a dead subscriber loop must not raise and must drop
        # the subscription deterministically (no leaked queue).
        stream.publish(_event(i=1))
        assert len(stream._subscribers) == 0
        stream.close()
