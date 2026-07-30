"""CodingHarness tool set — workspace tools + memory + subagent."""

from __future__ import annotations

import json
from pathlib import Path

from evopi.core.messages import UserMessage
from evopi.core.tool import Tool, ToolResult
from evopi.memory import MemoryService
from evopi.memory.store import MemoryStore
from evopi.plugins import initialize_plugin_candidate, review_plugin
from evopi.subagents.context_scope import SubAgentScope
from evopi.subagents.manager import SubAgentManager
from evopi.tools.builtins import (
    create_edit_file_tool,
    create_list_dir_tool,
    create_read_file_tool,
    create_shell_command_tool,
    create_write_file_tool,
)
from evopi.tools.schema import object_schema
from evopi.tools.shell_environment import ShellEnvironment


# ---------------------------------------------------------------------------
# Workspace tools
# ---------------------------------------------------------------------------

def coding_tools(
    workspace: str | Path,
    *,
    shell_environment: ShellEnvironment | None = None,
) -> list[Tool]:
    """Workspace-scoped coding and candidate-authoring tools."""
    return [
        create_list_dir_tool(workspace),
        create_read_file_tool(workspace),
        create_edit_file_tool(workspace),
        create_write_file_tool(workspace),
        create_plugin_candidate_tool(workspace),
        create_shell_command_tool(
            workspace,
            shell_environment=shell_environment,
        ),
    ]


def create_plugin_candidate_tool(workspace: str | Path) -> Tool:
    """Create and statically inspect one inactive Plugin candidate."""

    root = Path(workspace).expanduser().resolve()

    def create_plugin_candidate(
        name: str,
        template: str = "basic",
    ) -> ToolResult:
        normalized = name.strip().lower()
        target = root / ".evopi" / "plugin-candidates" / normalized
        candidate_path = initialize_plugin_candidate(
            name,
            template=template,
            path=target,
        )
        report = review_plugin(candidate_path)
        payload = {
            "candidate_path": str(candidate_path),
            "static_check": "passed" if report.passed else "failed",
            "digest": report.candidate.artifact.digest,
            "warnings": list(report.warnings),
            "errors": list(report.errors),
            "next_steps": ["review", "approve", "reload"],
        }
        return ToolResult(
            content=json.dumps(payload, ensure_ascii=False),
            is_error=not report.passed,
            metadata={
                "candidate_path": str(candidate_path),
                "candidate_digest": report.candidate.artifact.digest,
                "static_check": payload["static_check"],
            },
        )

    return Tool(
        name="create_plugin_candidate",
        description=(
            "Create an inactive EvoPi Plugin candidate from a packaged template "
            "under .evopi/plugin-candidates and run non-executing static review."
        ),
        parameters=object_schema(
            {
                "name": {
                    "type": "string",
                    "description": (
                        "Lowercase Plugin name using letters, digits, and hyphens"
                    ),
                },
                "template": {
                    "type": "string",
                    "enum": ["basic", "plan-mode"],
                    "description": "Packaged candidate template (default: basic)",
                },
            },
            required=["name"],
        ),
        handler=create_plugin_candidate,
        metadata={
            "effects": ["write"],
            "prompt_snippet": (
                "Create an inactive, statically reviewed Plugin candidate scaffold."
            ),
            "prompt_guidelines": [
                (
                    "When the user explicitly requests an extension, use "
                    "`create_plugin_candidate` before editing; customize it with "
                    "`edit_file`, run its tests, and stop at the human "
                    "review → approve → reload boundary."
                )
            ],
        },
    )


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
        metadata={"effects": ["memory_write"]},
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
        metadata={"effects": ["read"]},
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
        metadata={"effects": ["delegate"]},
    )


__all__ = [
    "coding_tools",
    "create_plugin_candidate_tool",
    "create_recall_tool",
    "create_remember_tool",
    "create_spawn_subagent_tool",
    "memory_tools",
]
