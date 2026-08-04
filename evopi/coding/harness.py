"""The first domain Harness shipped with EvoPi.

CodingHarness assembles workspace tools, memory, skills, sub-agents, and a
coding Policy Pack on top of BaseHarness.  The system prompt is generated
dynamically from the actual tool registry.
"""

from __future__ import annotations

from pathlib import Path

from evopi.ai.routing import ModelRoute
from evopi.coding.policies import coding_policy_pack
from evopi.coding.prompts import build_system_prompt
from evopi.coding.capabilities import (
    CodingResources,
    MemoryResourceCapability,
    SkillResourceCapability,
)
from evopi.coding.tools import (
    coding_tools,
    create_spawn_subagent_tool,
    memory_tools,
)
from evopi.core.context import AgentContext
from evopi.core.cancellation import AbortSignal
from evopi.core.events import CoreEvent
from evopi.core.messages import SystemMessage
from evopi.core.model import Model
from evopi.core.model_attempts import ModelAttemptInfo
from evopi.core.interaction import InteractionLimits, InteractionQueueMode
from evopi.core.model_errors import ModelRetryConfig
from evopi.evolution import PolicyActivationService
from evopi.harness.base import BaseHarness
from evopi.harness.confirmation import ConfirmationHandler
from evopi.harness.confirmation_broker import ConfirmationBroker
from evopi.memory import (
    MemoryPersistenceError,
    MemoryRetriever,
    MemoryService,
    MemoryStore,
)
from evopi.policy.approval import ApprovalMode
from evopi.session import SessionManager
from evopi.session.compact import CompactionSettings
from evopi.session.merge import MergeSettings
from evopi.skills import SkillLoader
from evopi.subagents.manager import SubAgentManager
from evopi.subagents.context_scope import GovernanceEnvelope
from evopi.tools.registry import ToolRegistry
from evopi.tools.shell_environment import (
    ShellEnvironment,
    resolve_shell_environment,
)


class CodingHarness(BaseHarness):
    """Domain harness for coding tasks with dynamic capability awareness."""

    def __init__(
        self,
        *,
        model: Model,
        model_route: ModelRoute | None = None,
        workspace: str | Path,
        trace_path: str | Path | None = None,
        max_turns: int = 20,
        retry_config: ModelRetryConfig | None = None,
        max_output_chars: int = 20_000,
        system_prompt: str | None = None,
        append_system_prompt: str | None = None,
        confirmation_handler: ConfirmationHandler | None = None,
        confirmation_broker: ConfirmationBroker | None = None,
        session_manager: SessionManager | None = None,
        approvals_path: str | Path | None = None,
        approval_mode: ApprovalMode = "warn",
        deadline: float | None = None,
        tool_timeout: float | None = None,
        compaction_settings: CompactionSettings | None = None,
        merge_settings: MergeSettings | None = None,
        plugin_paths: list[str | Path] | None = None,
        # -- optional modules -------------------------------------------------
        memory_path: str | Path | None = None,
        skills_root: str | Path | None = None,
        enable_subagent: bool = False,
        tool_names: set[str] | frozenset[str] | None = None,
        excluded_tool_names: set[str] | frozenset[str] | None = None,
        reserved_plugin_commands: frozenset[str] = frozenset(),
        resource_warnings: tuple[str, ...] = (),
        policy_activation_service: PolicyActivationService | None = None,
        shell_environment: ShellEnvironment | None = None,
        steering_mode: InteractionQueueMode = "one-at-a-time",
        follow_up_mode: InteractionQueueMode = "one-at-a-time",
        interaction_limits: InteractionLimits | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.shell_environment = (
            shell_environment or resolve_shell_environment()
        )
        self._dynamic_system_prompt = system_prompt is None
        self._append_system_prompt = append_system_prompt
        if tool_names is not None and excluded_tool_names is not None:
            raise ValueError(
                "Tool include and exclude ceilings are mutually exclusive"
            )

        # ------------------------------------------------------------------ #
        #  Memory (optional)
        # ------------------------------------------------------------------ #
        self._memory_store: MemoryStore | None = None
        self._memory_service: MemoryService | None = None
        self._memory_retriever: MemoryRetriever | None = None
        assembly_warnings = list(resource_warnings)
        if memory_path is not None:
            try:
                self._memory_store = MemoryStore(Path(memory_path))
            except MemoryPersistenceError as exc:
                assembly_warnings.append(f"Memory disabled: {exc}")
            else:
                self._memory_retriever = MemoryRetriever(self._memory_store)
                self._memory_service = MemoryService(self._memory_store)

        # ------------------------------------------------------------------ #
        #  Skills (optional)
        # ------------------------------------------------------------------ #
        self._skill_loader: SkillLoader | None = None
        if skills_root is not None:
            self._skill_loader = SkillLoader(
                workspace=str(self.workspace),
                root=str(skills_root),
            )
            assembly_warnings.extend(self._skill_loader.errors)

        # ------------------------------------------------------------------ #
        #  Assemble tools FIRST so the prompt can see them
        # ------------------------------------------------------------------ #
        tool_registry = ToolRegistry()
        for tool in coding_tools(
            self.workspace,
            shell_environment=self.shell_environment,
        ):
            tool.metadata.setdefault("source", "coding")
            tool_registry.register(tool)
        if self._memory_store is not None:
            for tool in memory_tools(self._memory_store, self._memory_service):
                tool.metadata.setdefault("source", "memory")
                tool_registry.register(tool)
        if enable_subagent:
            child_tool_names = frozenset(
                tool.name
                for tool in tool_registry
                if (
                    tool_names is None or tool.name in tool_names
                )
                and (
                    excluded_tool_names is None
                    or tool.name not in excluded_tool_names
                )
            )
            self._subagent_manager: SubAgentManager | None = SubAgentManager(
                model,
                tools=list(tool_registry),
                governance=GovernanceEnvelope(allowed_tool_names=child_tool_names),
            )
            subagent_tool = create_spawn_subagent_tool(self._subagent_manager)
            subagent_tool.metadata.setdefault("source", "subagent")
            tool_registry.register(subagent_tool)
        else:
            self._subagent_manager = None

        # ------------------------------------------------------------------ #
        #  Dynamic system prompt — the model sees its actual tool set
        # ------------------------------------------------------------------ #
        prompt_tools = [
            tool
            for tool in tool_registry
            if (tool_names is None or tool.name in tool_names)
            and (
                excluded_tool_names is None
                or tool.name not in excluded_tool_names
            )
        ]
        resolved_prompt = (
            build_system_prompt(
                prompt_tools,
                workspace=self.workspace,
                append=append_system_prompt,
            )
            if system_prompt is None
            else _append_prompt(system_prompt, append_system_prompt)
        )

        # ------------------------------------------------------------------ #
        #  Base harness (Plugin loading + Agent creation with dynamic prompt)
        # ------------------------------------------------------------------ #
        super().__init__(
            model=model,
            model_route=model_route,
            system_prompt=resolved_prompt,
            trace_path=trace_path,
            max_turns=max_turns,
            retry_config=retry_config,
            confirmation_handler=confirmation_handler,
            confirmation_broker=confirmation_broker,
            session_manager=(
                session_manager or SessionManager.in_memory(self.workspace)
            ),
            approvals_path=approvals_path,
            approval_mode=approval_mode,
            deadline=deadline,
            tool_timeout=tool_timeout,
            compaction_settings=compaction_settings,
            merge_settings=merge_settings,
            plugin_paths=plugin_paths,
            reserved_plugin_commands=reserved_plugin_commands,
            memory_enabled=self._memory_store is not None,
            skills_enabled=self._skill_loader is not None,
            assembly_warnings=tuple(assembly_warnings),
            policy_activation_service=policy_activation_service,
            defer_policy_activation=True,
            steering_mode=steering_mode,
            follow_up_mode=follow_up_mode,
            interaction_limits=interaction_limits,
        )

        # ------------------------------------------------------------------ #
        #  Wire pre-assembled tools into the harness
        # ------------------------------------------------------------------ #
        for tool in tool_registry:
            existing = self.tools.registry.get(tool.name)
            plugin_source = (
                existing.metadata.get("plugin_source")
                if existing is not None
                else None
            )
            if (
                isinstance(plugin_source, str)
                and self.plugin_can_override_tool(plugin_source, tool.name)
            ):
                continue
            self.register_tool(tool)
        self.load_policy_pack(
            coding_policy_pack(
                self.workspace,
                max_output_chars=max_output_chars,
                is_final_turn=lambda: (
                    self.agent.current_turn == self.agent.max_turns
                ),
            )
        )
        if policy_activation_service is not None:
            self._install_active_policies()
        self.configure_tool_ceiling(
            include_names=tool_names,
            exclude_names=excluded_tool_names,
        )
        if self._subagent_manager is not None:
            self._subagent_manager.bind_parent(self)

        # ------------------------------------------------------------------ #
        #  ContextProviders (after Agent is created)
        # ------------------------------------------------------------------ #
        if self._memory_store is not None:
            self.add_context_provider(self._memory_context_provider)
        if self._memory_service is not None:
            self._memory_service.bind_harness(self)
        if self._skill_loader is not None:
            self.add_context_provider(self._skill_context_provider)

    # -- context providers ---------------------------------------------------

    @property
    def resources(self) -> CodingResources:
        """Return a content-free snapshot for CLI and host status surfaces."""

        skills = (
            tuple(
                SkillResourceCapability(
                    name=skill.name,
                    version=skill.version,
                    risk_level=skill.risk_level,
                    source=skill.source_path,
                )
                for skill in self._skill_loader.registry.all()
            )
            if self._skill_loader is not None
            else ()
        )
        return CodingResources(
            memory=MemoryResourceCapability(
                enabled=self._memory_store is not None,
                entry_count=(
                    len(self._memory_store.all())
                    if self._memory_store is not None
                    else 0
                ),
            ),
            skills=skills,
            subagent_enabled=self._subagent_manager is not None,
        )

    async def _memory_context_provider(self, ctx: AgentContext) -> AgentContext:
        if self._memory_store is None:
            return ctx
        memories = await self._memory_retriever.retrieve(ctx)  # type: ignore[union-attr]
        for entry in memories:
            ctx.messages.insert(
                0,
                SystemMessage(content=f"[Memory] {entry.content}"),
            )
        return ctx

    async def _skill_context_provider(self, ctx: AgentContext) -> AgentContext:
        if self._skill_loader is None:
            return ctx
        for msg in reversed(ctx.messages):
            if msg.role == "user":
                skills = self._skill_loader.registry.search(msg.content, limit=3)
                if skills:
                    from evopi.core.events import CoreEvent

                    await self.agent.emit_event(
                        CoreEvent(
                            type="skills_selected",
                            data={
                                "skills": [
                                    {
                                        "name": skill.name,
                                        "version": skill.version,
                                        "source": skill.source_path,
                                    }
                                    for skill in skills
                                ]
                            },
                        )
                    )
                for skill in reversed(skills):
                    ctx.messages.insert(
                        0,
                        SystemMessage(content=skill.prompt_segment()),
                    )
                break
        return ctx

    async def _prepare_domain_context(
        self,
        context: AgentContext,
        *,
        attempt_info: ModelAttemptInfo | None = None,
        signal: AbortSignal | None = None,
    ) -> AgentContext:
        del attempt_info, signal
        turn = self.agent.current_turn
        if turn < 1:
            return context
        remaining = self.agent.max_turns - turn + 1
        if remaining > 2:
            return context

        mode = "finalize" if remaining == 1 else "warning"
        if mode == "finalize":
            context.tools = []
            if self._dynamic_system_prompt:
                prompt = build_system_prompt(
                    [],
                    workspace=self.workspace,
                    append=self._append_system_prompt,
                )
                for index, message in enumerate(context.messages):
                    if (
                        isinstance(message, SystemMessage)
                        and message.content == self.system_prompt
                    ):
                        context.messages[index] = SystemMessage(content=prompt)
                        break
                else:
                    context.messages.insert(0, SystemMessage(content=prompt))
            budget_prompt = (
                "Only 1 model turn remains. Tools are unavailable in this final turn. "
                "Return a final answer now with the verified outcome, any unfinished "
                "items, and the concrete next step. Do not request or fabricate ToolCalls."
            )
        else:
            budget_prompt = (
                "Only 2 model turns remain. Prioritize essential verification and prepare "
                "to return a complete final answer on the next model turn."
            )
        context.messages.insert(0, SystemMessage(content=budget_prompt))
        await self.agent.emit_event(
            CoreEvent(
                type="turn_budget_applied",
                data={
                    "mode": mode,
                    "turn": turn,
                    "max_turns": self.agent.max_turns,
                    "remaining_turns": remaining,
                },
            )
        )
        return context

    def _refresh_system_prompt_after_capability_change(self) -> None:
        if not self._dynamic_system_prompt:
            return
        prompt = build_system_prompt(
            list(self.active_tools()),
            workspace=self.workspace,
            append=self._append_system_prompt,
        )
        self.system_prompt = prompt
        self.agent.system_prompt = prompt
        for index, message in enumerate(self.agent.messages):
            if message.role == "system":
                self.agent.messages[index] = SystemMessage(content=prompt)
                break
        else:
            self.agent.messages.insert(0, SystemMessage(content=prompt))


def _append_prompt(base: str, appendix: str | None) -> str:
    if appendix is None or not appendix.strip():
        return base
    if not base:
        return appendix.strip()
    return f"{base}\n\n{appendix.strip()}"


__all__ = ["CodingHarness"]
