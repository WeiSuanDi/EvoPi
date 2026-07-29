"""Governed Harness integration for Session branch merge."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import uuid4

import pytest

from evopi.core.context import AgentContext
from evopi.core.messages import AssistantMessage, UserMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.harness import BaseHarness
from evopi.harness.confirmation import ConfirmationRequest, ConfirmationResponse
from evopi.policy import PolicyContext, PolicyDecision
from evopi.session import SessionManager, SessionMergeError, build_runtime_fingerprint
from evopi.session.merge import MergeSettings


class MergeModel:
    name = "merge-model"
    context_window = 100_000

    def __init__(self, *, content: str = "generated summary", error: Exception | None = None):
        self.content = content
        self.error = error
        self.contexts: list[AgentContext] = []

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        yield ModelComplete(
            message=AssistantMessage(content=self.content, stop_reason="stop")
        )


def _fingerprint():
    return build_runtime_fingerprint(
        harness="test",
        model="test",
        system_prompt="",
        tools=[],
        policies=[],
    )


def _append_run(session: SessionManager, content: str) -> str:
    run_id = uuid4().hex
    session.append_run_start(run_id=run_id, runtime_fingerprint=_fingerprint())
    session.append_message(run_id=run_id, message=UserMessage(content=content))
    session.append_run_end(run_id=run_id, reason="completed")
    assert session.leaf_id is not None
    return session.leaf_id


def _branched_session() -> tuple[SessionManager, str]:
    session = SessionManager.in_memory()
    common = _append_run(session, "shared request")
    session.branch(from_entry_id=common, branch_name="source")
    source = _append_run(session, "source result")
    session.switch_leaf(common)
    _append_run(session, "target result")
    return session, source


@dataclass
class MergePolicy:
    action: str
    calls: list[PolicyContext] = field(default_factory=list)
    name: str = "merge-policy"
    version: str = "1"
    description: str = "govern merge"
    hooks: tuple = ("before_session_merge",)
    priority: int = 10
    enabled: bool = True
    source: str = "test"
    risk_level: str = "medium"
    metadata: dict = field(default_factory=dict)

    def run(self, context: PolicyContext) -> PolicyDecision:
        self.calls.append(context)
        return PolicyDecision(action=self.action, reason="test merge policy")


def test_manual_merge_skips_model_and_emits_closed_lifecycle() -> None:
    session, source = _branched_session()
    model = MergeModel()
    harness = BaseHarness(model=model, session_manager=session)
    events = []
    harness.subscribe(events.append)

    result = asyncio.run(
        harness.merge_session_branch(source, summary="manual branch knowledge")
    )

    assert result.origin == "manual"
    assert model.contexts == []
    assert [event.type for event in events if event.type.startswith("session_merge")] == [
        "session_merge_start",
        "session_merge_end",
    ]
    assert events[-1].data["summary_sha256"]
    assert "manual branch knowledge" not in events[-1].data.values()


def test_automatic_merge_uses_governed_tool_free_model_operation() -> None:
    session, source = _branched_session()
    model = MergeModel(content="automatic branch knowledge")
    harness = BaseHarness(model=model, session_manager=session)

    result = asyncio.run(harness.merge_session_branch(source))

    assert result.origin == "model"
    assert len(model.contexts) == 1
    assert model.contexts[0].tools == []
    prompt = model.contexts[0].messages[-1].content
    assert "<shared-context>" in prompt
    assert "<source-branch>" in prompt
    assert "shared request" in prompt
    assert "source result" in prompt
    assert "target result" not in prompt
    assert session.messages[-1].metadata["summary_origin"] == "model"


def test_merge_policy_block_and_unsupported_action_fail_before_commit() -> None:
    for action in ("block", "rewrite_args"):
        session, source = _branched_session()
        harness = BaseHarness(model=MergeModel(), session_manager=session)
        policy = MergePolicy(action)
        harness.register_policy(policy)  # type: ignore[arg-type]
        before = len(session.entries)

        with pytest.raises(SessionMergeError):
            asyncio.run(
                harness.merge_session_branch(source, summary="must not persist")
            )

        assert len(session.entries) == before
        assert policy.calls[0].arguments is not None
        assert "summary" not in policy.calls[0].arguments


def test_merge_confirmation_denial_preserves_target_branch() -> None:
    session, source = _branched_session()
    requests: list[ConfirmationRequest] = []

    async def deny(request: ConfirmationRequest) -> ConfirmationResponse:
        requests.append(request)
        return ConfirmationResponse(
            request_id=request.id,
            decision="deny",
            reason="not now",
        )

    harness = BaseHarness(
        model=MergeModel(),
        session_manager=session,
        confirmation_handler=deny,
    )
    harness.register_policy(MergePolicy("require_confirmation"))  # type: ignore[arg-type]
    before = len(session.entries)

    with pytest.raises(SessionMergeError, match="not now"):
        asyncio.run(harness.merge_session_branch(source, summary="manual"))

    assert len(session.entries) == before
    assert requests[0].hook == "before_session_merge"
    assert requests[0].arguments is not None
    assert "summary" not in requests[0].arguments


def test_automatic_merge_failure_emits_error_without_session_mutation() -> None:
    session, source = _branched_session()
    harness = BaseHarness(
        model=MergeModel(error=RuntimeError("summary unavailable")),
        session_manager=session,
    )
    events = []
    harness.subscribe(events.append)
    before = len(session.entries)

    with pytest.raises(SessionMergeError, match="summary"):
        asyncio.run(harness.merge_session_branch(source))

    assert len(session.entries) == before
    merge_events = [
        event.type for event in events if event.type.startswith("session_merge")
    ]
    assert merge_events == ["session_merge_start", "session_merge_error"]


def test_manual_merge_obeys_configured_summary_limit() -> None:
    session, source = _branched_session()
    harness = BaseHarness(
        model=MergeModel(),
        session_manager=session,
        merge_settings=MergeSettings(max_summary_bytes=4),
    )
    before = len(session.entries)

    with pytest.raises(SessionMergeError, match="configured size limit"):
        asyncio.run(harness.merge_session_branch(source, summary="too long"))

    assert len(session.entries) == before
