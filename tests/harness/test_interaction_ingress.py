"""BaseHarness interaction ingress: delegation, modes, and Confirmation coexistence."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from evopi.coding.harness import CodingHarness
from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent
from evopi.core.interaction import (
    InteractionLimits,
    InteractionQueueClosedError,
)
from evopi.core.messages import AssistantMessage, UserMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.core.tool import Tool, ToolCall
from evopi.harness.base import BaseHarness
from evopi.harness.confirmation import (
    ConfirmationRequest,
    ConfirmationResponse,
)
from evopi.policy.decisions import PolicyDecision
from evopi.policy.types import HookName, PolicyContext, RiskLevel
from evopi.trace.reader import read_trace


class ScriptedModel:
    name = "scripted"

    def __init__(self, messages: list[AssistantMessage]) -> None:
        self._messages = iter(messages)
        self.contexts: list[AgentContext] = []

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        self.contexts.append(context)
        yield ModelComplete(message=next(self._messages))


class GatedModel:
    """Scripted model whose first stream waits on an optional release gate."""

    name = "gated"

    def __init__(
        self,
        messages: list[AssistantMessage],
        *,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self._messages = iter(messages)
        self._started = started
        self._release = release
        self.contexts: list[AgentContext] = []

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        self.contexts.append(context)
        message = next(self._messages)
        if self._started is not None:
            self._started.set()
        if self._release is not None:
            await self._release.wait()
        yield ModelComplete(message=message)


@dataclass(slots=True)
class RequireConfirmationPolicy:
    name: str = "confirm_echo"
    version: str = "1"
    description: str = "Require confirmation before echo"
    hooks: tuple[HookName, ...] = ("before_tool_call",)
    priority: int = 10
    enabled: bool = True
    source: str = "test"
    risk_level: RiskLevel = "medium"
    metadata: dict = field(default_factory=dict)

    def run(self, context: PolicyContext) -> PolicyDecision:
        if context.tool_call is None or context.tool_call.name != "echo":
            return PolicyDecision()
        return PolicyDecision(
            action="require_confirmation",
            reason="Echo requires human approval",
            risk_level=self.risk_level,
        )


def _echo_tool() -> Tool:
    return Tool(
        name="echo",
        description="Echo text",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        handler=lambda value: value,
    )


def _user_contents(messages: list[object]) -> list[str]:
    return [
        message.content for message in messages if isinstance(message, UserMessage)
    ]


def test_base_harness_exposes_interaction_ingress_and_idle_rejection() -> None:
    harness = BaseHarness(model=ScriptedModel([]))
    snapshot = harness.interaction_snapshot
    assert snapshot.pending_steering_count == 0
    assert snapshot.pending_follow_up_count == 0
    assert snapshot.steering_mode == "one-at-a-time"
    assert snapshot.follow_up_mode == "one-at-a-time"
    with pytest.raises(InteractionQueueClosedError):
        asyncio.run(harness.steer("hello"))
    with pytest.raises(InteractionQueueClosedError):
        asyncio.run(harness.follow_up("hello"))


def test_base_harness_threads_modes_and_limits() -> None:
    harness = BaseHarness(
        model=ScriptedModel([]),
        steering_mode="all",
        follow_up_mode="all",
        interaction_limits=InteractionLimits(max_pending_items=5),
    )
    snapshot = harness.interaction_snapshot
    assert snapshot.steering_mode == "all"
    assert snapshot.follow_up_mode == "all"


def test_coding_harness_threads_interaction_options(tmp_path: Path) -> None:
    harness = CodingHarness(
        model=ScriptedModel([]),
        workspace=tmp_path,
        steering_mode="all",
        follow_up_mode="all",
        interaction_limits=InteractionLimits(max_pending_items=5),
    )
    snapshot = harness.interaction_snapshot
    assert snapshot.steering_mode == "all"
    assert snapshot.follow_up_mode == "all"


def test_steer_during_confirmation_waits_and_does_not_bypass() -> None:
    async def scenario() -> None:
        confirmation_requested = asyncio.Event()
        approval = asyncio.Event()
        model = ScriptedModel(
            [
                AssistantMessage(
                    content="",
                    tool_calls=[
                        ToolCall(id="c-1", name="echo", arguments={"value": "x"})
                    ],
                    stop_reason="tool_use",
                ),
                AssistantMessage(content="done", stop_reason="stop"),
            ]
        )

        async def handler(request: ConfirmationRequest) -> ConfirmationResponse:
            confirmation_requested.set()
            await approval.wait()
            return ConfirmationResponse(
                request_id=request.id,
                decision="approve",
                reason="ok",
            )

        harness = BaseHarness(model=model, confirmation_handler=handler)
        harness.register_tool(_echo_tool())
        harness.register_policy(RequireConfirmationPolicy())
        events: list[CoreEvent] = []
        harness.subscribe(events.append)
        task = asyncio.create_task(harness.prompt("initial"))
        await confirmation_requested.wait()
        receipt = await harness.steer("during confirmation")
        approval.set()
        answer = await task

        assert answer.content == "done"
        assert receipt.kind == "steer"
        # the confirmation was answered, not cancelled or bypassed
        response = next(
            event for event in events if event.type == "confirmation_response"
        )
        assert response.data["response"].decision == "approve"
        sequence = [event.type for event in events]
        tool_end_at = max(
            i for i, t in enumerate(sequence) if t == "tool_execution_end"
        )
        delivered_at = sequence.index("interaction_delivered")
        assert sequence.index("confirmation_response") < delivered_at
        assert tool_end_at < delivered_at
        assert _user_contents(model.contexts[1].messages) == [
            "initial",
            "during confirmation",
        ]

    asyncio.run(scenario())


def test_interaction_trace_records_contain_no_content(tmp_path: Path) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        model = GatedModel(
            [
                AssistantMessage(content="one", stop_reason="stop"),
                AssistantMessage(content="two", stop_reason="stop"),
            ],
            started=started,
            release=release,
        )
        harness = BaseHarness(model=model, trace_path=tmp_path / "trace.jsonl")
        task = asyncio.create_task(harness.prompt("initial"))
        await started.wait()
        await harness.steer("secret steering text")
        release.set()
        await task

    asyncio.run(scenario())
    records = list(read_trace(tmp_path / "trace.jsonl"))
    interaction_records = [
        record for record in records if record["type"].startswith("interaction_")
    ]
    assert interaction_records
    serialized = "\n".join(json.dumps(record) for record in interaction_records)
    assert "secret steering text" not in serialized
