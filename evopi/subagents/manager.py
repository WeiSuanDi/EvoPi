"""SubAgent lifecycle manager — spawn, run, collect, validate.

A SubAgent is a lightweight nested agent with a constrained scope.  It runs
independently and returns a validated result to the parent.  The parent
Harness governs spawning via Policy hooks.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import replace
from uuid import uuid4

from evopi.core.messages import AssistantMessage
from evopi.core.context import AgentContext
from evopi.core.model import Model
from evopi.core.run import AgentEndReason
from evopi.core.tool import Tool
from evopi.harness.base import BaseHarness
from evopi.harness.confirmation import ConfirmationRequest
from evopi.policy.types import PolicyContext
from evopi.subagents.context_scope import GovernanceEnvelope, SubAgentScope
from evopi.subagents.result_validation import SubAgentResult, validate_subagent_result

_logger = logging.getLogger(__name__)


class SubAgentError(RuntimeError):
    """Raised when a sub-agent cannot start or finishes abnormally."""


class SubAgentManager:
    """Create and run sub-agents with constrained scope.

    Usage::

        manager = SubAgentManager(model, available_tools)
        scope = SubAgentScope(
            system_prompt="You are a code reviewer.",
            messages=[UserMessage(content="Review this file")],
            tool_names=["read_file"],
        )
        result = await manager.run(scope)
    """

    def __init__(
        self,
        model: Model,
        tools: list[Tool] | None = None,
        *,
        max_output_chars: int = 10_000,
        governance: GovernanceEnvelope | None = None,
        parent_harness: BaseHarness | None = None,
    ) -> None:
        self._model = model
        self._tools = {t.name: t for t in (tools or [])}
        self._max_output_chars = max_output_chars
        self._governance = governance or GovernanceEnvelope()
        self._parent_harness = parent_harness

    def bind_parent(self, harness: BaseHarness) -> None:
        """Bind the parent after domain Harness assembly has completed."""

        self._parent_harness = harness

    async def run(
        self,
        scope: SubAgentScope,
        *,
        task_id: str | None = None,
    ) -> SubAgentResult:
        """Run a sub-agent with the given *scope* and return a validated result.

        The sub-agent runs synchronously within the parent process — it is
        not an OS-level sandbox.
        """
        governance = self._effective_governance()
        if governance.depth >= governance.max_depth:
            raise SubAgentError("Sub-agent maximum nesting depth reached")
        if not scope.messages:
            raise SubAgentError("Sub-agent requires at least one message")
        if scope.messages[-1].role != "user":
            raise SubAgentError("Sub-agent final scoped message must be a UserMessage")
        scope = await self._govern_scope(scope, governance)
        allowed_tools = self._resolve_tools(scope.tool_names, governance)
        child_id = task_id or uuid4().hex
        child_trace = None
        if self._parent_harness is not None and self._parent_harness.trace_path is not None:
            parent_trace = self._parent_harness.trace_path
            child_trace = parent_trace.with_name(
                f"{parent_trace.stem}.subagent-{child_id}{parent_trace.suffix}"
            )
        child = BaseHarness(
            model=self._model,
            model_route=(
                self._parent_harness.model_route
                if self._parent_harness is not None
                else None
            ),
            system_prompt=scope.system_prompt,
            max_turns=min(
                scope.max_turns,
                governance.max_turns or scope.max_turns,
            ),
            deadline=governance.deadline,
            tool_timeout=governance.tool_timeout,
            confirmation_handler=governance.confirmation_handler,
            retry_config=(
                self._parent_harness.agent.retry_config
                if self._parent_harness is not None
                else None
            ),
            approval_mode="off",
            trace_path=child_trace,
        )
        for tool in allowed_tools:
            child.register_tool(tool)
        for policy in governance.required_policies:
            child.register_policy(policy, replace=True)

        last_message: AssistantMessage | None = None
        end_reason: AgentEndReason = "completed"
        turns_used = 0
        tool_count = 0

        final_input = scope.messages[-1]
        child.agent.messages.extend(scope.messages[:-1])

        abort_watcher: asyncio.Task[None] | None = None
        if governance.parent_signal is not None:
            abort_watcher = asyncio.create_task(
                self._propagate_abort(child, governance.parent_signal)
            )
        try:
            last_message = await child.prompt(final_input.content)
        except asyncio.CancelledError:
            end_reason = "aborted"
        except Exception as exc:
            end_reason = "error"
            last_message = AssistantMessage(
                content=f"Sub-agent failed: {type(exc).__name__}: {exc}",
                stop_reason="error",
            )

        finally:
            if abort_watcher is not None:
                abort_watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await abort_watcher

        if child.agent.last_run is not None:
            end_reason = child.agent.last_run.end_reason
        # Count turns from assistant messages in the child
        turns_used = sum(
            1 for msg in child.messages if isinstance(msg, AssistantMessage)
        )

        # Count tool calls across child messages
        used_tools: list[str] = []
        for msg in child.messages:
            if isinstance(msg, AssistantMessage):
                tool_count += len(msg.tool_calls)
                used_tools.extend(call.name for call in msg.tool_calls)

        result = validate_subagent_result(
            SubAgentResult(
                content=last_message.content if last_message else "",
                success=end_reason in {"completed", "terminated"},
                end_reason=end_reason,
                messages=list(child.messages),
                tool_calls_made=tool_count,
                used_tool_names=tuple(used_tools),
                turns_used=turns_used,
                metadata={
                    key: value
                    for key, value in {
                        "task_id": task_id,
                        "child_id": child_id,
                        "parent_run_id": governance.parent_run_id,
                        "parent_tool_call_id": governance.parent_tool_call_id,
                    }.items()
                    if value is not None
                },
            ),
            allowed_tools=set(scope.tool_names),
            max_output_chars=self._max_output_chars,
        )
        if not child.is_running:
            child.close()
        await self._observe_result(result)
        return result

    def _resolve_tools(
        self,
        names: list[str],
        governance: GovernanceEnvelope,
    ) -> list[Tool]:
        """Resolve allowed tool names against the manager's tool set."""
        resolved: list[Tool] = []
        for name in names:
            ceiling = governance.allowed_tool_names
            if ceiling is not None and name not in ceiling:
                raise SubAgentError(
                    f"Tool '{name}' exceeds the parent capability ceiling"
                )
            tool = self._tools.get(name)
            if tool is None:
                raise SubAgentError(f"Tool '{name}' is not available to the parent")
            if name == "spawn_subagent":
                raise SubAgentError("Nested spawn_subagent is not available")
            resolved.append(tool)
        return resolved

    def _effective_governance(self) -> GovernanceEnvelope:
        parent = self._parent_harness
        if parent is None:
            return self._governance
        return replace(
            self._governance,
            required_policies=tuple(parent.policies.all()),
            confirmation_handler=parent.confirmation_handler,
            parent_signal=parent.signal,
            parent_run_id=parent.agent.current_run_id,
            deadline=parent.remaining_deadline,
            tool_timeout=parent.tool_timeout,
            max_turns=parent.agent.max_turns,
        )

    async def _govern_scope(
        self,
        scope: SubAgentScope,
        governance: GovernanceEnvelope,
    ) -> SubAgentScope:
        parent = self._parent_harness
        if parent is None:
            return scope
        arguments = {
            "tool_names": list(scope.tool_names),
            "max_turns": scope.max_turns,
        }
        evaluation = await parent._evaluate(
            PolicyContext(
                hook="before_subagent_spawn",
                agent_context=AgentContext(
                    messages=list(parent.messages),
                    tools=parent.tools.all(),
                ),
                arguments=arguments,
                aborted=bool(governance.parent_signal and governance.parent_signal.aborted),
            )
        )
        if evaluation.final.action == "block":
            raise SubAgentError(evaluation.final.reason or "Sub-agent spawn blocked by Policy")
        if evaluation.final.action == "require_confirmation":
            request = ConfirmationRequest(
                hook="before_subagent_spawn",
                reason=evaluation.final.reason or "Sub-agent spawn requires confirmation",
                risk_level=evaluation.final.risk_level,
                arguments=evaluation.arguments or arguments,
                metadata={"session_id": parent.session.session_id},
            )
            response = await parent._request_confirmation(
                request,
                signal=governance.parent_signal,
            )
            if not response.approved:
                raise SubAgentError(response.reason or "Sub-agent spawn confirmation denied")
        rewritten = evaluation.arguments or arguments
        raw_names = rewritten.get("tool_names", scope.tool_names)
        raw_turns = rewritten.get("max_turns", scope.max_turns)
        if not isinstance(raw_names, list) or any(not isinstance(name, str) for name in raw_names):
            raise SubAgentError("Policy rewrote sub-agent tool_names to an invalid value")
        if not isinstance(raw_turns, int) or isinstance(raw_turns, bool) or raw_turns < 1:
            raise SubAgentError("Policy rewrote sub-agent max_turns to an invalid value")
        return SubAgentScope(
            system_prompt=scope.system_prompt,
            messages=list(scope.messages),
            tool_names=list(raw_names),
            max_turns=min(scope.max_turns, raw_turns),
            metadata=dict(scope.metadata),
        )

    async def _observe_result(self, result: SubAgentResult) -> None:
        parent = self._parent_harness
        if parent is None:
            return
        await parent._evaluate(
            PolicyContext(
                hook="after_subagent_run",
                agent_context=AgentContext(
                    messages=list(parent.messages),
                    tools=parent.tools.all(),
                ),
                arguments={
                    "success": result.success,
                    "end_reason": result.end_reason,
                    "used_tool_names": list(result.used_tool_names),
                    "turns_used": result.turns_used,
                },
            )
        )

    @staticmethod
    async def _propagate_abort(child: BaseHarness, signal) -> None:
        await signal.wait()
        child.abort()


__all__ = ["SubAgentError", "SubAgentManager"]
