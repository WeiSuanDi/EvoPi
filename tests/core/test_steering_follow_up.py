"""Steering and follow-up interaction semantics for the Core runtime.

Part 1 covers the interaction queue protocol (types, validation, admission,
FIFO, modes, and the atomic admission/seal gate). Part 2 covers the Agent
safe-point integration (initial steering, post-turn steering, terminal
follow-up, and fail-closed clearing).
"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime
from typing import Any

import pytest

from evopi.core.events import CoreEvent
from evopi.core.interaction import (
    InteractionContentError,
    InteractionContentTooLargeError,
    InteractionLimits,
    InteractionModeError,
    InteractionQueueClosedError,
    InteractionQueueController,
    InteractionQueueFullError,
    InteractionQueueSnapshot,
)
from evopi.core.messages import UserMessage


# ---------------------------------------------------------------------------
# Part 1 — queue protocol and admission controller
# ---------------------------------------------------------------------------


def _open_queue(**kwargs: Any) -> InteractionQueueController:
    queue = InteractionQueueController(**kwargs)
    queue.open("run-1")
    return queue


def test_controller_rejects_invalid_queue_modes() -> None:
    for bad in ("", "all-at-once", 3, True, None):
        with pytest.raises(InteractionModeError):
            InteractionQueueController(steering_mode=bad)  # type: ignore[arg-type]
        with pytest.raises(InteractionModeError):
            InteractionQueueController(follow_up_mode=bad)  # type: ignore[arg-type]


def test_controller_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError):
        InteractionQueueController(limits=InteractionLimits(max_pending_items=0))
    with pytest.raises(ValueError):
        InteractionQueueController(limits=InteractionLimits(max_pending_items=-3))
    with pytest.raises(ValueError):
        InteractionQueueController(limits=InteractionLimits(max_content_bytes=0))
    with pytest.raises(ValueError):
        InteractionQueueController(limits=InteractionLimits(max_content_bytes=-1))
    # booleans do not satisfy integer fields
    with pytest.raises(ValueError):
        InteractionQueueController(
            limits=InteractionLimits(max_pending_items=True)  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        InteractionQueueController(
            limits=InteractionLimits(max_content_bytes=True)  # type: ignore[arg-type]
        )


def test_controller_rejects_invalid_content() -> None:
    queue = _open_queue()
    for bad in ("", "   ", "\n\t", None, 42, ["x"], b"bytes"):
        with pytest.raises(InteractionContentError):
            asyncio.run(queue.admit("steer", "api", bad, emit=None))  # type: ignore[arg-type]


def test_controller_rejects_oversized_content_by_utf8_bytes() -> None:
    queue = _open_queue(limits=InteractionLimits(max_content_bytes=4))
    # exactly at the limit is accepted
    asyncio.run(queue.admit("steer", "api", "éé", emit=None))
    with pytest.raises(InteractionContentTooLargeError):
        asyncio.run(queue.admit("steer", "api", "ééé", emit=None))
    assert queue.snapshot().pending_steering_count == 1


def test_controller_rejects_invalid_kind_and_origin() -> None:
    queue = _open_queue()
    with pytest.raises(InteractionModeError):
        asyncio.run(queue.admit("steer_all", "api", "x", emit=None))  # type: ignore[arg-type]
    with pytest.raises(InteractionModeError):
        asyncio.run(queue.admit("steer", "web", "x", emit=None))  # type: ignore[arg-type]
    with pytest.raises(InteractionModeError):
        asyncio.run(queue.admit("steer", True, "x", emit=None))  # type: ignore[arg-type]


def test_controller_rejects_admission_while_idle() -> None:
    queue = InteractionQueueController()
    with pytest.raises(InteractionQueueClosedError):
        asyncio.run(queue.admit("steer", "api", "x", emit=None))
    with pytest.raises(InteractionQueueClosedError):
        asyncio.run(queue.admit("follow_up", "api", "x", emit=None))


def test_controller_admission_fifo_positions_and_counts() -> None:
    queue = _open_queue()
    events: list[CoreEvent] = []
    r1 = asyncio.run(queue.admit("steer", "api", "first", emit=events.append))
    r2 = asyncio.run(queue.admit("steer", "api", "second", emit=events.append))
    r3 = asyncio.run(queue.admit("follow_up", "repl", "third", emit=events.append))
    assert (r1.position, r2.position, r3.position) == (1, 2, 3)
    assert r1.run_id == "run-1"
    assert r2.kind == "steer"
    assert r3.origin == "repl"
    assert isinstance(r1.created_at, datetime)
    snapshot = queue.snapshot()
    assert snapshot.pending_steering_count == 2
    assert snapshot.pending_follow_up_count == 1
    assert [r.input_id for r in snapshot.pending] == [
        r1.input_id,
        r2.input_id,
        r3.input_id,
    ]
    assert snapshot.steering_mode == "one-at-a-time"
    assert snapshot.follow_up_mode == "one-at-a-time"
    # every admission emitted a queued event with correlation data only
    queued = [event for event in events if event.type == "interaction_queued"]
    assert [event.data["position"] for event in queued] == [1, 2, 3]
    assert all(event.data["run_id"] == "run-1" for event in queued)
    assert all("pending_steering_count" in event.data for event in queued)
    assert all("pending_follow_up_count" in event.data for event in queued)
    assert queued[0].data["mode"] == "one-at-a-time"


def test_controller_receipts_and_snapshots_are_immutable_without_content() -> None:
    queue = _open_queue()
    receipt = asyncio.run(queue.admit("steer", "api", "secret text", emit=None))
    assert not hasattr(receipt, "content")
    with pytest.raises(FrozenInstanceError):
        receipt.input_id = "changed"  # type: ignore[misc]
    snapshot = queue.snapshot()
    assert isinstance(snapshot, InteractionQueueSnapshot)
    assert all(not hasattr(item, "content") for item in snapshot.pending)
    with pytest.raises(FrozenInstanceError):
        snapshot.pending = ()  # type: ignore[misc]
    # snapshots are fresh immutable tuples, not live queue views
    asyncio.run(queue.admit("follow_up", "api", "another", emit=None))
    later = queue.snapshot()
    assert len(later.pending) == 2
    assert len(snapshot.pending) == 1


def test_controller_capacity_is_total_across_both_queues() -> None:
    queue = _open_queue(limits=InteractionLimits(max_pending_items=2))
    asyncio.run(queue.admit("steer", "api", "a", emit=None))
    asyncio.run(queue.admit("follow_up", "api", "b", emit=None))
    with pytest.raises(InteractionQueueFullError):
        asyncio.run(queue.admit("steer", "api", "c", emit=None))


def test_controller_seal_rejects_admission_and_clears_pending() -> None:
    queue = _open_queue()
    r1 = asyncio.run(queue.admit("steer", "api", "a", emit=None))
    r2 = asyncio.run(queue.admit("follow_up", "api", "b", emit=None))
    events: list[CoreEvent] = []
    asyncio.run(queue.seal(emit=events.append, run_id="run-1", reason="aborted"))
    snapshot = queue.snapshot()
    assert snapshot.pending_steering_count == 0
    assert snapshot.pending_follow_up_count == 0
    assert queue.sealed
    cleared = [event for event in events if event.type == "interaction_cleared"]
    assert len(cleared) == 1
    assert cleared[0].data["reason"] == "aborted"
    assert cleared[0].data["count"] == 2
    assert cleared[0].data["input_ids"] == [r1.input_id, r2.input_id]
    assert cleared[0].data["kinds"] == ["steer", "follow_up"]
    with pytest.raises(InteractionQueueClosedError):
        asyncio.run(queue.admit("steer", "api", "late", emit=None))
    # sealing again is a no-op: no duplicate cleared event
    asyncio.run(queue.seal(emit=events.append, run_id="run-1", reason="completed"))
    assert len([event for event in events if event.type == "interaction_cleared"]) == 1


def test_controller_drain_steering_one_at_a_time() -> None:
    queue = _open_queue()
    asyncio.run(queue.admit("steer", "api", "a", emit=None))
    asyncio.run(queue.admit("steer", "api", "b", emit=None))
    appended: list[UserMessage] = []
    delivered = asyncio.run(
        queue.drain_steering(emit=None, run_id="run-1", append=appended.append)
    )
    assert [message.content for message in delivered] == ["a"]
    assert [message.content for message in appended] == ["a"]
    assert queue.snapshot().pending_steering_count == 1
    second = asyncio.run(
        queue.drain_steering(emit=None, run_id="run-1", append=appended.append)
    )
    assert [message.content for message in second] == ["b"]
    assert queue.snapshot().pending_steering_count == 0


def test_controller_drain_steering_all_delivers_snapshot_as_separate_messages() -> None:
    queue = _open_queue(steering_mode="all")
    for text in ("a", "b", "c"):
        asyncio.run(queue.admit("steer", "api", text, emit=None))
    appended: list[UserMessage] = []
    delivered = asyncio.run(
        queue.drain_steering(emit=None, run_id="run-1", append=appended.append)
    )
    assert [message.content for message in delivered] == ["a", "b", "c"]
    assert [message.content for message in appended] == ["a", "b", "c"]
    assert queue.snapshot().pending_steering_count == 0


def test_controller_follow_up_one_at_a_time_per_terminal_gate() -> None:
    queue = _open_queue()
    asyncio.run(queue.admit("follow_up", "api", "a", emit=None))
    asyncio.run(queue.admit("follow_up", "api", "b", emit=None))
    delivered = asyncio.run(
        queue.drain_follow_up_at_terminal(
            emit=None,
            run_id="run-1",
            reason="completed",
            append=lambda message: None,
        )
    )
    assert [message.content for message in delivered] == ["a"]
    assert not queue.sealed
    second = asyncio.run(
        queue.drain_follow_up_at_terminal(
            emit=None,
            run_id="run-1",
            reason="completed",
            append=lambda message: None,
        )
    )
    assert [message.content for message in second] == ["b"]
    # third candidate has nothing left: the gate seals admission atomically
    assert (
        asyncio.run(
            queue.drain_follow_up_at_terminal(
                emit=None, run_id="run-1", reason="completed",
                append=lambda message: None,
            )
        )
        == ()
    )
    assert queue.sealed
    with pytest.raises(InteractionQueueClosedError):
        asyncio.run(queue.admit("steer", "api", "late", emit=None))


def test_controller_follow_up_all_delivers_snapshot_before_one_request() -> None:
    queue = _open_queue(follow_up_mode="all")
    for text in ("a", "b", "c"):
        asyncio.run(queue.admit("follow_up", "api", text, emit=None))
    delivered = asyncio.run(
        queue.drain_follow_up_at_terminal(
            emit=None,
            run_id="run-1",
            reason="completed",
            append=lambda message: None,
        )
    )
    assert [message.content for message in delivered] == ["a", "b", "c"]
    assert queue.snapshot().pending_follow_up_count == 0


def test_controller_terminal_gate_gives_steering_priority() -> None:
    queue = _open_queue()
    asyncio.run(queue.admit("steer", "api", "s", emit=None))
    asyncio.run(queue.admit("follow_up", "api", "f", emit=None))
    delivered = asyncio.run(
        queue.drain_follow_up_at_terminal(
            emit=None,
            run_id="run-1",
            reason="completed",
            append=lambda message: None,
        )
    )
    assert delivered == ()
    assert not queue.sealed
    steers = asyncio.run(
        queue.drain_steering(emit=None, run_id="run-1", append=lambda message: None)
    )
    assert [message.content for message in steers] == ["s"]


def test_controller_delivered_messages_carry_interaction_metadata() -> None:
    queue = _open_queue()
    asyncio.run(queue.admit("steer", "rpc", "hello", emit=None))
    delivered = asyncio.run(
        queue.drain_steering(emit=None, run_id="run-1", append=lambda message: None)
    )
    (message,) = delivered
    assert isinstance(message, UserMessage)
    assert message.role == "user"
    metadata = message.metadata["interaction"]
    assert metadata["schema_version"] == 1
    assert metadata["kind"] == "steer"
    assert metadata["origin"] == "rpc"
    assert metadata["input_id"]
    datetime.fromisoformat(metadata["created_at"])


def test_controller_preserves_content_exactly_after_validation() -> None:
    queue = _open_queue()
    text = "  padded  \n"
    asyncio.run(queue.admit("steer", "api", text, emit=None))
    delivered = asyncio.run(
        queue.drain_steering(emit=None, run_id="run-1", append=lambda message: None)
    )
    assert delivered[0].content == text


def test_controller_delivery_events_order_and_never_contain_content() -> None:
    queue = _open_queue(steering_mode="all", follow_up_mode="all")
    events: list[CoreEvent] = []
    asyncio.run(queue.admit("steer", "rpc", "top secret", emit=events.append))
    asyncio.run(queue.admit("follow_up", "api", "also secret", emit=events.append))
    asyncio.run(
        queue.drain_steering(emit=events.append, run_id="run-1", append=lambda m: None)
    )
    asyncio.run(
        queue.drain_follow_up_at_terminal(
            emit=events.append,
            run_id="run-1",
            reason="completed",
            append=lambda message: None,
        )
    )
    asyncio.run(queue.seal(emit=events.append, run_id="run-1", reason="aborted"))
    interaction_events = [
        event
        for event in events
        if event.type.startswith("interaction_")
    ]
    serialized = "\n".join(str(event.data) for event in interaction_events)
    assert "top secret" not in serialized
    assert "also secret" not in serialized
    delivered_events = [event for event in events if event.type == "interaction_delivered"]
    assert len(delivered_events) == 2
    assert delivered_events[0].data["kind"] == "steer"
    assert delivered_events[0].data["message_id"]
    assert "pending_steering_count" in delivered_events[0].data
    assert "pending_follow_up_count" in delivered_events[0].data
