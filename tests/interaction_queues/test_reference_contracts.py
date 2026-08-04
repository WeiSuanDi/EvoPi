"""Direct contract tests for the known-good reference adapter (SFU-3).

These are adversarial probes of the reference itself, covering the frozen
CONTEXT.md sections 2-4 invariants at unit level: strict limits and mode
validation, the UTF-8 byte boundary, the atomic admission gate, initial-safe-
point ordering, Turn/Model-Attempt accounting including retries, the
Confirmation boundary, snapshot immutability, multi-run support, and JSON
safety.  They prove the reference is a conformant known-good oracle that the
reusable scenarios then drive.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import UTC
from typing import Any, cast

import pytest

from .conformance import (
    FIXED_TS,
    ERROR_CLOSED,
    ERROR_RUN_NOT_ACTIVE,
    AdmissionResult,
    InteractionLimits,
    to_json_safe,
)
from .reference import ReferenceInteractionAdapter


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def test_limits_validation_rejects_bools_and_non_positive_values() -> None:
    with pytest.raises(ValueError):
        InteractionLimits(max_pending_items=0)
    with pytest.raises(ValueError):
        InteractionLimits(max_pending_items=-1)
    with pytest.raises(ValueError):
        InteractionLimits(max_content_bytes=0)
    with pytest.raises(ValueError):
        InteractionLimits(max_pending_items=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        InteractionLimits(max_content_bytes=True)  # type: ignore[arg-type]
    defaults = InteractionLimits()
    assert defaults.max_pending_items == 100
    assert defaults.max_content_bytes == 65_536


def test_reference_rejects_unknown_modes() -> None:
    with pytest.raises(ValueError):
        ReferenceInteractionAdapter(steering_mode="bogus")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ReferenceInteractionAdapter(follow_up_mode="bogus")  # type: ignore[arg-type]


def test_admission_without_run_is_run_not_active_and_creates_no_events() -> None:
    adapter = ReferenceInteractionAdapter()
    result = _run(adapter.steer("x"))
    assert result.receipt is None
    assert result.error is not None and result.error.code == ERROR_RUN_NOT_ACTIVE
    assert adapter.events() == ()


def test_content_validation_edge_cases() -> None:
    adapter = ReferenceInteractionAdapter(limits=InteractionLimits(max_content_bytes=6))
    _run(adapter.start_run("prompt-1"))
    _run(adapter.model_stream(tool_calls=0))
    _run(adapter.turn_end())
    for bad in ("", "   ", "\n\t", "\ud800"):
        result: AdmissionResult = _run(adapter.steer(cast(Any, bad)))
        assert result.receipt is None
        assert result.error is not None and result.error.code == "interaction_content_invalid"
    result = _run(adapter.steer("héllo"))  # exactly 6 UTF-8 bytes
    assert result.receipt is not None, "content at the byte boundary must be accepted"
    result = _run(adapter.steer("héllo!"))  # 7 UTF-8 bytes
    assert result.receipt is None
    assert result.error is not None and result.error.code == "interaction_content_too_large"


def test_content_preserved_exactly_with_whitespace() -> None:
    adapter = ReferenceInteractionAdapter()
    _run(adapter.start_run("prompt-1"))
    _run(adapter.model_stream(tool_calls=0))
    _run(adapter.turn_end())
    result = _run(adapter.steer("  spaced-content  "))
    assert result.receipt is not None
    outcome = _run(adapter.terminal_candidate())
    assert outcome == "continued"
    delivered = [
        m for m in adapter.committed_messages() if m.interaction is not None
    ]
    assert len(delivered) == 1
    assert delivered[0].content == "  spaced-content  "
    assert delivered[0].interaction is not None
    assert delivered[0].interaction.input_id == result.receipt.input_id
    assert delivered[0].interaction.schema_version == 1
    assert delivered[0].interaction.kind == "steer"
    assert delivered[0].interaction.origin == "api"
    assert delivered[0].interaction.created_at == result.receipt.created_at


def test_capacity_counts_both_queues_together() -> None:
    adapter = ReferenceInteractionAdapter(limits=InteractionLimits(max_pending_items=2))
    _run(adapter.start_run("prompt-1"))
    _run(adapter.model_stream(tool_calls=0))
    _run(adapter.turn_end())
    assert _run(adapter.steer("a")).receipt is not None
    assert _run(adapter.follow_up("b")).receipt is not None
    result = _run(adapter.steer("c"))
    assert result.receipt is None
    assert result.error is not None and result.error.code == "interaction_queue_full"


async def _initial_safe_point_body(adapter: ReferenceInteractionAdapter) -> None:
    task = asyncio.create_task(adapter.start_run("prompt-1"))
    await asyncio.sleep(0)
    result = await adapter.steer("early")
    assert result.receipt is not None
    await task
    turns = [e for e in adapter.events() if e.type == "turn_start"]
    assert len(turns) == 1 and turns[0].data.get("turn_number") == 1
    delivered = [e for e in adapter.events() if e.type == "interaction_delivered"]
    assert len(delivered) == 1 and delivered[0].data.get("input_id") == result.receipt.input_id
    assert delivered[0].sequence < turns[0].sequence
    assert adapter.turn_count() == 1


def test_initial_safe_point_drains_before_turn_start_one() -> None:
    _run(_initial_safe_point_body(ReferenceInteractionAdapter()))


def test_receipt_fields_are_exact() -> None:
    adapter = ReferenceInteractionAdapter()
    run_id = _run(adapter.start_run("prompt-1"))
    result = _run(adapter.steer("first"))
    assert result.receipt is not None
    assert result.receipt.run_id == run_id
    assert result.receipt.kind == "steer"
    assert result.receipt.position == 1
    assert result.receipt.created_at == FIXED_TS
    assert result.receipt.created_at.tzinfo is UTC
    second = _run(adapter.follow_up("second"))
    assert second.receipt is not None and second.receipt.position == 2


def test_sealed_admission_rejects_with_interaction_closed() -> None:
    adapter = ReferenceInteractionAdapter()
    _run(adapter.start_run("prompt-1"))
    _run(adapter.model_stream(tool_calls=0))
    _run(adapter.turn_end())
    assert _run(adapter.terminal_candidate()) == "completed"
    result = _run(adapter.steer("late"))
    assert result.receipt is None
    assert result.error is not None and result.error.code == ERROR_CLOSED


def test_turn_accounting_all_batch_consumes_one_turn() -> None:
    adapter = ReferenceInteractionAdapter(follow_up_mode="all")
    _run(adapter.start_run("prompt-1"))
    _run(adapter.model_stream(tool_calls=0))
    _run(adapter.turn_end())
    _run(adapter.follow_up("f1"))
    _run(adapter.follow_up("f2"))
    assert _run(adapter.terminal_candidate()) == "continued"
    assert adapter.turn_count() == 2, "an all-mode batch is one model request"
    assert adapter.attempt_count() == 1
    assert len([m for m in adapter.committed_messages() if m.interaction is not None]) == 2


def test_retry_is_an_attempt_not_a_turn() -> None:
    adapter = ReferenceInteractionAdapter()
    _run(adapter.start_run("prompt-1"))
    _run(adapter.model_stream(tool_calls=0))
    _run(adapter.turn_end())
    _run(adapter.steer("s"))
    assert _run(adapter.terminal_candidate()) == "continued"
    _run(adapter.model_stream(tool_calls=0, retry=True))
    assert adapter.turn_count() == 2
    assert adapter.attempt_count() == 2


async def _confirmation_deny_body(adapter: ReferenceInteractionAdapter) -> None:
    await adapter.start_run("prompt-1")
    await adapter.model_stream(tool_calls=1)
    step = asyncio.create_task(adapter.tool_step(0, confirmation=True))
    await asyncio.sleep(0)
    await adapter.decide(approved=False)
    await step
    assert adapter.tool_executions() == ()
    await adapter.turn_end()
    assert await adapter.terminal_candidate() == "completed"


def test_confirmation_deny_never_executes() -> None:
    _run(_confirmation_deny_body(ReferenceInteractionAdapter()))


def test_decide_without_pending_confirmation_is_a_kit_violation() -> None:
    adapter = ReferenceInteractionAdapter()
    _run(adapter.start_run("prompt-1"))
    with pytest.raises(Exception):
        _run(adapter.decide(approved=True))


def test_snapshot_is_immutable_and_not_a_live_view() -> None:
    adapter = ReferenceInteractionAdapter()
    _run(adapter.start_run("prompt-1"))
    _run(adapter.model_stream(tool_calls=0))
    _run(adapter.turn_end())
    before = adapter.snapshot()
    _run(adapter.steer("x"))
    after = adapter.snapshot()
    assert before.pending_steering_count == 0 and before.pending == ()
    assert after.pending_steering_count == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(after, "pending_steering_count", 99)
    with pytest.raises(TypeError):
        cast(Any, after.pending)[0] = object()


def test_cleared_event_is_exact_and_ordered() -> None:
    adapter = ReferenceInteractionAdapter()
    _run(adapter.start_run("prompt-1"))
    _run(adapter.model_stream(tool_calls=0))
    _run(adapter.turn_end())
    first = _run(adapter.steer("s1"))
    second = _run(adapter.follow_up("f1"))
    assert first.receipt is not None and second.receipt is not None
    _run(adapter.terminate("cancelled"))
    cleared = [e for e in adapter.events() if e.type == "interaction_cleared"]
    assert len(cleared) == 1
    data = cleared[0].data
    assert data["reason"] == "cancelled"
    assert data["count"] == 2
    assert data["input_ids"] == [first.receipt.input_id, second.receipt.input_id]
    assert data["kinds"] == ["steer", "follow_up"]
    agent_ends = [e for e in adapter.events() if e.type == "agent_end"]
    assert len(agent_ends) == 1 and agent_ends[0].data.get("outcome") == "cancelled"
    assert cleared[0].sequence < agent_ends[0].sequence
    assert _run(adapter.wait_for_idle()) is None


def test_multi_run_support_resets_counters_and_carries_run_ids() -> None:
    adapter = ReferenceInteractionAdapter()
    run_one = _run(adapter.start_run("prompt-1"))
    _run(adapter.model_stream(tool_calls=0))
    _run(adapter.turn_end())
    _run(adapter.terminate("aborted"))
    assert adapter.turn_count() == 1
    run_two = _run(adapter.start_run("prompt-2"))
    assert run_two != run_one
    assert adapter.turn_count() == 1
    result = _run(adapter.steer("fresh"))
    assert result.receipt is not None and result.receipt.position == 1
    assert {e.run_id for e in adapter.events()} == {run_one, run_two}


def test_events_never_carry_content_and_are_json_safe() -> None:
    adapter = ReferenceInteractionAdapter()
    _run(adapter.start_run("prompt-1"))
    _run(adapter.model_stream(tool_calls=0))
    _run(adapter.turn_end())
    _run(adapter.steer("SECRET-CONTENT-MARKER"))
    _run(adapter.terminate("closed"))
    for event in adapter.events():
        serialized = json.dumps(to_json_safe(event.data), sort_keys=True)
        assert "SECRET-CONTENT-MARKER" not in serialized
    for payload in adapter.trace_payloads():
        assert "SECRET-CONTENT-MARKER" not in payload
    assert "SECRET-CONTENT-MARKER" not in "".join(adapter.trace_payloads())


def test_to_json_safe_round_trips_and_rejects() -> None:
    assert to_json_safe({"a": [1, 2.5, True, None, "x"]}) == {
        "a": [1, 2.5, True, None, "x"]
    }
    assert to_json_safe(FIXED_TS) == "2026-01-01T00:00:00+00:00"
    with pytest.raises(ValueError):
        to_json_safe(float("nan"))
    with pytest.raises(ValueError):
        to_json_safe(object())
    with pytest.raises(ValueError):
        to_json_safe({1: "non-string-key"})
