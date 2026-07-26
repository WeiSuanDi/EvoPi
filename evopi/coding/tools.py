"""CodingHarness tool set — workspace tools + memory + subagent."""

from __future__ import annotations

from pathlib import Path

from evopi.core.messages import UserMessage
from evopi.core.tool import Tool
from evopi.memory import MemoryService
from evopi.memory.store import MemoryStore
from evopi.subagents.context_scope import SubAgentScope
from evopi.subagents.manager import SubAgentManager
from evopi.tools.builtins import (
    create_list_dir_tool,
    create_read_file_tool,
    create_shell_command_tool,
    create_write_file_tool,
)
from evopi.tools.schema import object_schema


# ---------------------------------------------------------------------------
# Workspace tools
# ---------------------------------------------------------------------------

def coding_tools(workspace: str | Path) -> list[Tool]:
    """The four workspace-scoped file and shell tools."""
    return [
        create_list_dir_tool(workspace),
        create_read_file_tool(workspace),
        create_write_file_tool(workspace),
        create_shell_command_tool(workspace),
    ]


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------

def create_remember_tool(service: MemoryService) -> Tool:
    """Tool that lets the agent explicitly persist a fact to memory."""

    async def remember(content: str, tags: str = "") -> str:
        entry = await service.write(
            content=content,
            tags=[t.strip() for t in tags.split(",") if t.strip()],
        )
        return f"Stored memory {entry.id[:8]}"

    return Tool(
        name="remember",
        description="Persist a fact, preference, or learning to long-term memory.",
        parameters=object_schema(
            {
                "content": {"type": "string", "description": "The fact or learning to remember"},
                "tags": {
                    "type": "string",
                    "description": "Comma-separated tags for categorization (optional)",
                },
            },
            required=["content"],
        ),
        handler=remember,
    )


def create_recall_tool(store: MemoryStore) -> Tool:
    """Tool that lets the agent query long-term memory."""

    def recall(query: str) -> str:
        results = store.search(query, limit=5)
        if not results:
            return "No matching memories found."
        lines = [f"- [{', '.join(r.tags)}] {r.content}" for r in results]
        return "\n".join(lines)

    return Tool(
        name="recall",
        description="Search long-term memory for facts, preferences, or past learnings.",
        parameters=object_schema(
            {
                "query": {"type": "string", "description": "Keywords to search memory for"},
            },
            required=["query"],
        ),
        handler=recall,
    )


def memory_tools(
    store: MemoryStore,
    service: MemoryService | None = None,
) -> list[Tool]:
    resolved = service or MemoryService(store)
    return [create_remember_tool(resolved), create_recall_tool(store)]


# ---------------------------------------------------------------------------
# SubAgent tool
# ---------------------------------------------------------------------------

def create_spawn_subagent_tool(manager: SubAgentManager) -> Tool:
    """Tool that lets the agent delegate a task to a child sub-agent."""

    async def spawn_subagent(
        task: str,
        tools: str = "read_file",
        max_turns: int = 5,
    ) -> str:
        tool_names = [t.strip() for t in tools.split(",") if t.strip()]
        scope = SubAgentScope(
            system_prompt="You are a focused sub-agent. Complete the task and return the result.",
            messages=[UserMessage(content=task)],
            tool_names=tool_names,
            max_turns=max_turns,
        )
        result = await manager.run(scope)
        if result.success:
            return result.content
        return f"Sub-agent failed: {result.content}"

    return Tool(
        name="spawn_subagent",
        description=(
            "Delegate a focused task to a child sub-agent that runs independently "
            "with a limited set of tools. Use for parallel research, code review, "
            "or file analysis."
        ),
        parameters=object_schema(
            {
                "task": {"type": "string", "description": "The task for the sub-agent to complete"},
                "tools": {
                    "type": "string",
                    "description": "Comma-separated tool names (default: read_file)",
                },
                "max_turns": {
                    "type": "integer",
                    "description": "Maximum turns for the sub-agent (default: 5)",
                },
            },
            required=["task"],
        ),
        handler=spawn_subagent,
    )


__all__ = [
    "coding_tools",
    "create_recall_tool",
    "create_remember_tool",
    "create_spawn_subagent_tool",
    "memory_tools",
]
