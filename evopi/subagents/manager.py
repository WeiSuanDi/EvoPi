"""SubAgent lifecycle manager — spawn, run, collect, validate.

A SubAgent is a lightweight nested agent with a constrained scope.  It runs
independently and returns a validated result to the parent.  The parent
Harness governs spawning via Policy hooks.
"""

from __future__ import annotations

import asyncio
import logging

from evopi.core.agent import Agent
from evopi.core.messages import AssistantMessage
from evopi.core.model import Model
from evopi.core.run import AgentEndReason
from evopi.core.tool import Tool
from evopi.subagents.context_scope import SubAgentScope
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
    ) -> None:
        self._model = model
        self._tools = {t.name: t for t in (tools or [])}
        self._max_output_chars = max_output_chars

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
        allowed_tools = self._resolve_tools(scope.tool_names)
        agent = Agent(
            model=self._model,
            system_prompt=scope.system_prompt,
            tools=allowed_tools,
            max_turns=scope.max_turns,
        )
        # Inject context messages
        agent.messages.extend(scope.messages)

        last_message: AssistantMessage | None = None
        end_reason: AgentEndReason = "completed"
        turns_used = 0
        tool_count = 0

        if not scope.messages:
            raise SubAgentError("Sub-agent requires at least one message")

        try:
            last_message = await agent.prompt(scope.messages[-1].content)
        except asyncio.CancelledError:
            end_reason = "aborted"
        except Exception as exc:
            end_reason = "error"
            last_message = AssistantMessage(
                content=f"Sub-agent failed: {type(exc).__name__}: {exc}",
                stop_reason="error",
            )

        if agent.last_run is not None:
            end_reason = agent.last_run.end_reason
        # Count turns from assistant messages in the child
        turns_used = sum(
            1 for msg in agent.messages if isinstance(msg, AssistantMessage)
        )

        # Count tool calls across child messages
        for msg in agent.messages:
            if isinstance(msg, AssistantMessage):
                tool_count += len(msg.tool_calls)

        return validate_subagent_result(
            SubAgentResult(
                content=last_message.content if last_message else "",
                success=end_reason == "completed",
                end_reason=end_reason,
                messages=list(agent.messages),
                tool_calls_made=tool_count,
                turns_used=turns_used,
                metadata={"task_id": task_id} if task_id else {},
            ),
            allowed_tools=set(scope.tool_names),
            max_output_chars=self._max_output_chars,
        )

    def _resolve_tools(self, names: list[str]) -> list[Tool]:
        """Resolve allowed tool names against the manager's tool set."""
        resolved: list[Tool] = []
        for name in names:
            tool = self._tools.get(name)
            if tool is not None:
                resolved.append(tool)
            else:
                _logger.warning("Sub-agent requested unknown tool: %s", name)
        return resolved


__all__ = ["SubAgentError", "SubAgentManager"]
