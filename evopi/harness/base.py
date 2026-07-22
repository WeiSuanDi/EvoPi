"""Base Harness: lifecycle, hooks, Policy dispatch, context and Trace."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from evopi.core.agent import Agent
from evopi.core.agent_loop import BeforeToolCallResult
from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent, EventListener
from evopi.core.messages import AssistantMessage, ToolResultMessage
from evopi.core.model import Model
from evopi.core.tool import Tool, ToolCall, ToolResult
from evopi.harness.context_manager import ContextManager, ContextProvider
from evopi.harness.confirmation import (
    ConfirmationHandler,
    ConfirmationRequest,
    ConfirmationResponse,
)
from evopi.harness.lifecycle import Lifecycle
from evopi.harness.policy_manager import PolicyManager
from evopi.harness.session_manager import SessionManager
from evopi.harness.tool_manager import ToolManager
from evopi.policy.decisions import PolicyEvaluation
from evopi.policy.registry import PolicyPack
from evopi.policy.types import Policy, PolicyContext
from evopi.trace.events import TraceRecord
from evopi.trace.writer import JsonlTraceWriter


class PolicyBlockedError(RuntimeError):
    pass


class BaseHarness:
    def __init__(
        self,
        *,
        model: Model,
        system_prompt: str = "",
        trace_path: str | Path | None = None,
        max_turns: int = 20,
        confirmation_handler: ConfirmationHandler | None = None,
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.tools = ToolManager()
        self.policies = PolicyManager()
        self.context = ContextManager()
        self.lifecycle = Lifecycle()
        self.session = SessionManager()
        self.trace_writer = JsonlTraceWriter(trace_path) if trace_path is not None else None
        self.confirmation_handler = confirmation_handler
        self.agent = Agent(
            model=model,
            system_prompt=system_prompt,
            max_turns=max_turns,
            before_tool_call=self._before_tool_call,
            after_tool_call=self._after_tool_call,
            prepare_context=self._prepare_context,
            after_model_call=self._after_model_call,
            after_turn=self._after_turn,
        )
        self.agent.subscribe(self._on_core_event)

    @property
    def state(self):
        return self.lifecycle.state

    @property
    def messages(self):
        return self.agent.messages

    def register_tool(self, tool: Tool, *, replace: bool = False) -> None:
        self.tools.register(tool, replace=replace)
        self.agent.tools = self.tools.all()

    def register_policy(self, policy: Policy, *, replace: bool = False) -> None:
        self.policies.register(policy, replace=replace)

    def load_policy_pack(self, pack: PolicyPack) -> None:
        self.policies.load_pack(pack)

    def add_context_provider(self, provider: ContextProvider) -> None:
        self.context.add(provider)

    def subscribe(self, listener: EventListener):
        return self.agent.subscribe(listener)

    async def prompt(self, content: str) -> AssistantMessage:
        self.lifecycle.start()
        self.agent.tools = self.tools.all()
        try:
            answer = await self.agent.prompt(content)
        except Exception as exc:
            self.lifecycle.fail(exc)
            raise
        self.lifecycle.complete()
        return answer

    def reset(self) -> None:
        self.agent.reset()
        self.session.reset()
        self.lifecycle.reset()

    async def _prepare_context(self, context: AgentContext) -> AgentContext:
        prepared = await self.context.prepare(context)
        evaluation = await self._evaluate(
            PolicyContext(hook="before_model_call", agent_context=prepared)
        )
        self._raise_if_blocked(evaluation, "Model call")
        return prepared

    async def _after_model_call(
        self, context: AgentContext, assistant: AssistantMessage
    ) -> AssistantMessage:
        evaluation = await self._evaluate(
            PolicyContext(
                hook="after_model_call",
                agent_context=context,
                assistant_message=assistant,
            )
        )
        self._raise_if_blocked(evaluation, "Model response")
        return assistant

    async def _before_tool_call(
        self,
        context: AgentContext,
        assistant: AssistantMessage,
        call: ToolCall,
    ) -> BeforeToolCallResult:
        evaluation = await self._evaluate(
            PolicyContext(
                hook="before_tool_call",
                agent_context=context,
                assistant_message=assistant,
                tool_call=call,
                arguments=dict(call.arguments),
            )
        )
        final = evaluation.final
        blocked = final.action == "block"
        reason = final.reason
        if final.action == "require_confirmation":
            request = ConfirmationRequest(
                hook="before_tool_call",
                reason=reason or "Human confirmation is required",
                risk_level=final.risk_level,
                policy_names=tuple(
                    decision.policy_name
                    for decision in evaluation.decisions
                    if decision.action == "require_confirmation"
                    and decision.policy_name is not None
                ),
                tool_call=call,
                arguments=dict(
                    evaluation.arguments
                    if evaluation.arguments is not None
                    else call.arguments
                ),
                metadata={"session_id": self.session.session_id},
            )
            response = await self._request_confirmation(request)
            blocked = not response.approved
            reason = response.reason or (
                "Human confirmation approved"
                if response.approved
                else "Human confirmation denied"
            )
        return BeforeToolCallResult(
            block=blocked,
            reason=reason,
            arguments=evaluation.arguments,
        )

    async def _request_confirmation(
        self, request: ConfirmationRequest
    ) -> ConfirmationResponse:
        self.lifecycle.wait_for_confirmation()
        try:
            await self.agent.emit_event(
                CoreEvent(type="confirmation_request", data={"request": request})
            )
            response = await self._resolve_confirmation(request)
        finally:
            self.lifecycle.resume()

        await self.agent.emit_event(
            CoreEvent(type="confirmation_response", data={"response": response})
        )
        return response

    async def _resolve_confirmation(
        self, request: ConfirmationRequest
    ) -> ConfirmationResponse:
        if self.confirmation_handler is None:
            return ConfirmationResponse(
                request_id=request.id,
                decision="deny",
                reason="Human confirmation is required but no handler is configured",
                metadata={"automatic": True},
            )

        try:
            response = self.confirmation_handler(request)
            if inspect.isawaitable(response):
                response = await response
        except Exception as exc:
            return ConfirmationResponse(
                request_id=request.id,
                decision="deny",
                reason=f"Confirmation handler failed: {type(exc).__name__}: {exc}",
                metadata={"automatic": True, "handler_error": type(exc).__name__},
            )

        if not isinstance(response, ConfirmationResponse):
            return ConfirmationResponse(
                request_id=request.id,
                decision="deny",
                reason="Confirmation handler returned an invalid response",
                metadata={"automatic": True, "handler_error": "invalid_response"},
            )
        if response.request_id != request.id:
            return ConfirmationResponse(
                request_id=request.id,
                decision="deny",
                reason="Confirmation response did not match the active request",
                metadata={"automatic": True, "handler_error": "request_id_mismatch"},
            )
        return response

    async def _after_tool_call(
        self,
        context: AgentContext,
        assistant: AssistantMessage,
        call: ToolCall,
        result: ToolResult,
    ) -> ToolResult:
        evaluation = await self._evaluate(
            PolicyContext(
                hook="after_tool_call",
                agent_context=context,
                assistant_message=assistant,
                tool_call=call,
                tool_result=result,
                arguments=dict(call.arguments),
            )
        )
        final_result = evaluation.tool_result or result
        if evaluation.final.action == "block":
            return ToolResult(
                content=evaluation.final.reason or "Tool result was blocked",
                is_error=True,
                metadata={**final_result.metadata, "blocked_after_execution": True},
            )
        if evaluation.final.action == "terminate":
            final_result.terminate = True
        return final_result

    async def _after_turn(
        self,
        context: AgentContext,
        assistant: AssistantMessage,
        results: list[ToolResultMessage],
    ) -> None:
        edited = [message.metadata.get("path") for message in results if message.tool_name == "write_file"]
        await self._evaluate(
            PolicyContext(
                hook="after_turn",
                agent_context=context,
                assistant_message=assistant,
                metadata={"edited_files": [path for path in edited if path]},
            )
        )

    async def _evaluate(self, context: PolicyContext) -> PolicyEvaluation:
        evaluation = await self.policies.engine.evaluate(context)
        for decision in evaluation.decisions:
            event = CoreEvent(
                type="policy_decision",
                data={"hook": context.hook, "decision": decision},
            )
            await self.agent.emit_event(event)
        await self.agent.emit_event(
            CoreEvent(
                type="policy_evaluation",
                data=self._policy_evaluation_data(context, evaluation),
            )
        )
        return evaluation

    async def _on_core_event(self, event: CoreEvent) -> None:
        if self.trace_writer is not None:
            self.trace_writer(event)
        if event.type == "error":
            context = PolicyContext(
                hook="on_error",
                agent_context=AgentContext(
                    messages=self.agent.messages,
                    tools=self.agent.tools,
                ),
                error=str(event.data.get("error", "")),
            )
            evaluation = await self.policies.engine.evaluate(context)
            if self.trace_writer is not None:
                for decision in evaluation.decisions:
                    self.trace_writer.write(
                        TraceRecord(
                            type="policy_decision",
                            run_id=event.run_id,
                            data={"hook": "on_error", "decision": decision},
                        )
                    )
                self.trace_writer.write(
                    TraceRecord(
                        type="policy_evaluation",
                        run_id=event.run_id,
                        data=self._policy_evaluation_data(context, evaluation),
                    )
                )

    @staticmethod
    def _policy_evaluation_data(
        context: PolicyContext,
        evaluation: PolicyEvaluation,
    ) -> dict[str, Any]:
        return {
            "hook": context.hook,
            "input": {
                "tool_call": context.tool_call,
                "arguments": context.arguments,
                "tool_result": context.tool_result,
                "error": context.error,
                "metadata": context.metadata,
            },
            "final": evaluation.final,
            "decisions": evaluation.decisions,
        }

    @staticmethod
    def _raise_if_blocked(evaluation: PolicyEvaluation, subject: str) -> None:
        if evaluation.final.action in {"block", "require_confirmation"}:
            raise PolicyBlockedError(evaluation.final.reason or f"{subject} was blocked")


__all__ = ["BaseHarness", "PolicyBlockedError"]
