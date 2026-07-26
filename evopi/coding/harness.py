"""The first domain Harness shipped with EvoPi.

CodingHarness assembles workspace tools, memory, skills, sub-agents, and a
coding Policy Pack on top of BaseHarness.
"""

from __future__ import annotations

from pathlib import Path

from evopi.coding.policies import coding_policy_pack
from evopi.coding.prompts import CODING_SYSTEM_PROMPT
from evopi.coding.tools import (
    coding_tools,
    create_spawn_subagent_tool,
    memory_tools,
)
from evopi.core.context import AgentContext
from evopi.core.messages import SystemMessage
from evopi.core.model import Model
from evopi.core.model_errors import ModelRetryConfig
from evopi.harness.base import BaseHarness
from evopi.harness.confirmation import ConfirmationHandler
from evopi.memory import MemoryRetriever, MemoryStore
from evopi.policy.approval import ApprovalMode
from evopi.session import SessionManager
from evopi.session.compact import CompactionSettings
from evopi.skills import SkillLoader
from evopi.subagents.manager import SubAgentManager


class CodingHarness(BaseHarness):
    """Domain harness for coding tasks.

    Extends BaseHarness with workspace tools, optional memory persistence,
    skill-based guidance, and sub-agent delegation.
    """

    def __init__(
        self,
        *,
        model: Model,
        workspace: str | Path,
        trace_path: str | Path | None = None,
        max_turns: int = 20,
        retry_config: ModelRetryConfig | None = None,
        max_output_chars: int = 20_000,
        system_prompt: str = CODING_SYSTEM_PROMPT,
        confirmation_handler: ConfirmationHandler | None = None,
        session_manager: SessionManager | None = None,
        approvals_path: str | Path | None = None,
        approval_mode: ApprovalMode = "warn",
        deadline: float | None = None,
        tool_timeout: float | None = None,
        compaction_settings: CompactionSettings | None = None,
        plugin_paths: list[str | Path] | None = None,
        # -- optional modules -------------------------------------------------
        memory_path: str | Path | None = None,
        skills_root: str | Path | None = None,
        enable_subagent: bool = False,
    ) -> None:
        self.workspace = Path(workspace).resolve()

        # ------------------------------------------------------------------ #
        #  Memory (optional)
        # ------------------------------------------------------------------ #
        self._memory_store: MemoryStore | None = None
        if memory_path is not None:
            self._memory_store = MemoryStore(Path(memory_path))
            self._memory_retriever = MemoryRetriever(self._memory_store)

        # ------------------------------------------------------------------ #
        #  Skills (optional)
        # ------------------------------------------------------------------ #
        self._skill_loader: SkillLoader | None = None
        if skills_root is not None:
            self._skill_loader = SkillLoader(
                workspace=str(self.workspace),
                root=str(skills_root),
            )

        # ------------------------------------------------------------------ #
        #  Base harness (Plugin loading happens here)
        # ------------------------------------------------------------------ #
        super().__init__(
            model=model,
            system_prompt=system_prompt,
            trace_path=trace_path,
            max_turns=max_turns,
            retry_config=retry_config,
            confirmation_handler=confirmation_handler,
            session_manager=(
                session_manager or SessionManager.in_memory(self.workspace)
            ),
            approvals_path=approvals_path,
            approval_mode=approval_mode,
            deadline=deadline,
            tool_timeout=tool_timeout,
            compaction_settings=compaction_settings,
            plugin_paths=plugin_paths,
        )

        # ------------------------------------------------------------------ #
        #  Workspace tools
        # ------------------------------------------------------------------ #
        for tool in coding_tools(self.workspace):
            self.register_tool(tool)
        self.load_policy_pack(
            coding_policy_pack(self.workspace, max_output_chars=max_output_chars)
        )

        # ------------------------------------------------------------------ #
        #  Memory tools + ContextProvider
        # ------------------------------------------------------------------ #
        if self._memory_store is not None:
            for tool in memory_tools(self._memory_store):
                self.register_tool(tool)
            self.add_context_provider(self._memory_context_provider)

        # ------------------------------------------------------------------ #
        #  Skills ContextProvider
        # ------------------------------------------------------------------ #
        if self._skill_loader is not None:
            self.add_context_provider(self._skill_context_provider)

        # ------------------------------------------------------------------ #
        #  SubAgent tool
        # ------------------------------------------------------------------ #
        if enable_subagent:
            self._subagent_manager = SubAgentManager(model, tools=self.tools.all())
            self.register_tool(create_spawn_subagent_tool(self._subagent_manager))

    # -- context providers ---------------------------------------------------

    async def _memory_context_provider(self, ctx: AgentContext) -> AgentContext:
        """Inject relevant memory entries into the agent context."""
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
        """Inject relevant skill instructions into the agent context."""
        if self._skill_loader is None:
            return ctx
        # Use the last user message as a query
        for msg in reversed(ctx.messages):
            if msg.role == "user":
                skills = self._skill_loader.registry.search(msg.content, limit=3)
                for skill in reversed(skills):
                    ctx.messages.insert(
                        0,
                        SystemMessage(content=skill.prompt_segment()),
                    )
                break
        return ctx


__all__ = ["CodingHarness"]
