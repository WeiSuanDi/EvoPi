"""RPC v2 identity and replay-window tests for EventStream."""

from __future__ import annotations

from evopi.core.events import CoreEvent
from evopi.rpc.event_stream import EventStream


def test_event_stream_exposes_stable_identity_and_atomic_bounds() -> None:
    stream = EventStream(capacity=2)
    identity = stream.stream_id
    stream.publish(CoreEvent(type="agent_start", run_id="run-1"))
    stream.publish(CoreEvent(type="turn_start", run_id="run-1"))
    stream.publish(CoreEvent(type="turn_end", run_id="run-1"))

    window = stream.snapshot(after_sequence=1)

    assert stream.stream_id == identity
    assert window.stream_id == identity
    assert window.oldest_sequence == 2
    assert window.latest_sequence == 3
    assert window.capacity == 2
    assert [event.sequence for event in window.events] == [2, 3]


def test_empty_event_stream_snapshot_uses_zero_bounds() -> None:
    stream = EventStream(capacity=3)

    window = stream.snapshot(after_sequence=0)

    assert window.oldest_sequence == 0
    assert window.latest_sequence == 0
    assert window.events == ()
