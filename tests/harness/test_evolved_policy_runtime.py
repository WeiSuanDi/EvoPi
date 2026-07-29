from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from evopi.core.context import AgentContext
from evopi.core.messages import AssistantMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.evolution import PolicyReplacement
from evopi.harness import BaseHarness
from evopi.policy.approval import policy_digest
from evopi.policy.decisions import PolicyDecision
from evopi.trace import read_trace

from tests.evolution.test_policy_activation_pipeline import reviewed, services
from tests.plugins.test_plugin_runtime_v1 import _write_runtime_plugin


class _Model:
    name = "evolved-policy-test"

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        yield ModelComplete(
            message=AssistantMessage(content="done", stop_reason="stop")
        )


def _active_policy(tmp_path: Path):
    source_store, evidence = reviewed(tmp_path / "source")
    _, _, _, _, approvals, runtime = services(tmp_path / "runtime")
    approval = approvals.approve(
        evidence,
        operator="tester",
        source_store=source_store,
    )
    runtime.activate(approval.record_id, operator="tester")
    return runtime


class _ExistingPolicy:
    name = "demo_policy"
    version = "0.9.0"
    description = "existing runtime target"
    hooks = ("before_model_call",)
    priority = 1
    enabled = True
    source = "builtins"
    risk_level = "low"
    metadata: dict[str, object] = {}

    def run(self, context):
        return PolicyDecision.allow(policy_name=self.name)


def test_bare_harness_only_loads_evolved_policies_when_explicitly_configured(
    tmp_path: Path,
) -> None:
    runtime = _active_policy(tmp_path)

    neutral = BaseHarness(model=_Model())
    governed = BaseHarness(
        model=_Model(),
        policy_activation_service=runtime,
    )

    assert "demo_policy" not in neutral.capabilities.policy_names
    assert "demo_policy" in governed.capabilities.policy_names
    descriptor = next(
        item for item in governed.capabilities.policies if item.name == "demo_policy"
    )
    assert descriptor.artifact_digest is not None
    assert descriptor.activation_id is not None


def test_runtime_reload_failure_preserves_previous_policy_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("EVOPI_HOME", str(tmp_path / "home"))
    runtime = _active_policy(tmp_path)
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    harness = BaseHarness(
        model=_Model(),
        policy_activation_service=runtime,
        plugin_paths=[_write_runtime_plugin(plugin_dir)],
    )
    before = harness.capabilities
    active = runtime.active()[0]
    (active.artifact_path / "policy.py").write_text(
        "\n# tampered\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Policy"):
        harness.reload_runtime()

    assert harness.capabilities == before
    assert harness.capabilities.plugin_names == ("runtime-plugin",)


def test_explicit_digest_bound_replacement_swaps_the_staged_policy(
    tmp_path: Path,
) -> None:
    source_store, evidence = reviewed(tmp_path / "source")
    _, _, _, _, approvals, runtime = services(tmp_path / "runtime")
    approval = approvals.approve(
        evidence,
        operator="tester",
        source_store=source_store,
    )
    existing = _ExistingPolicy()
    runtime.activate(
        approval.record_id,
        operator="tester",
        replacement=PolicyReplacement(
            policy_name=existing.name,
            expected_digest=policy_digest(existing),  # type: ignore[arg-type]
        ),
    )
    harness = BaseHarness(
        model=_Model(),
        policy_activation_service=runtime,
        defer_policy_activation=True,
    )
    harness.register_policy(existing)  # type: ignore[arg-type]

    harness.reload_runtime()

    active = harness.policies.registry.get("demo_policy")
    assert active.version == "1.0.0"
    assert harness.capabilities.policies[0].replaces == "demo_policy"


def test_policy_runtime_events_are_written_without_source_content(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("EVOPI_HOME", str(tmp_path / "home"))
    runtime = _active_policy(tmp_path)
    trace_path = tmp_path / "trace.jsonl"
    harness = BaseHarness(
        model=_Model(),
        trace_path=trace_path,
        policy_activation_service=runtime,
    )

    harness.reload_runtime()

    records = read_trace(trace_path)
    event_types = [record["type"] for record in records]
    assert "policy_artifact_loaded" in event_types
    assert "policy_runtime_reload_start" in event_types
    assert "policy_runtime_reload_end" in event_types
    serialized = trace_path.read_text(encoding="utf-8")
    assert "class DemoPolicy" not in serialized


def test_registration_is_frozen_while_a_run_is_active() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        class WaitingModel:
            name = "waiting"

            async def stream(
                self,
                context: AgentContext,
            ) -> AsyncIterator[ModelStreamEvent]:
                entered.set()
                await release.wait()
                yield ModelComplete(
                    message=AssistantMessage(content="done", stop_reason="stop")
                )

        harness = BaseHarness(model=WaitingModel())
        task = asyncio.create_task(harness.prompt("wait"))
        await entered.wait()
        try:
            with pytest.raises(RuntimeError, match="running"):
                harness.register_policy(object())  # type: ignore[arg-type]
        finally:
            release.set()
            await task

    asyncio.run(scenario())
