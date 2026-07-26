"""Governed Memory write service used by Domain Harness tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent
from evopi.memory.policy import MemoryWritePolicy
from evopi.memory.store import MemoryEntry, MemoryPersistenceError, MemoryStore
from evopi.policy.types import PolicyContext

if TYPE_CHECKING:
    from evopi.harness.base import BaseHarness


class MemoryService:
    def __init__(
        self,
        store: MemoryStore,
        *,
        policy: MemoryWritePolicy | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or MemoryWritePolicy()
        self._harness: BaseHarness | None = None

    def bind_harness(self, harness: BaseHarness) -> None:
        self._harness = harness

    async def write(
        self,
        *,
        content: str,
        tags: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEntry:
        entry = MemoryEntry(content=content, tags=tags, metadata=dict(metadata or {}))
        error = self.policy.validate(entry, self.store)
        if error is not None:
            raise ValueError(error)
        if self.policy.is_sensitive(entry):
            raise ValueError("Memory entry contains sensitive content")
        harness = self._harness
        if harness is not None:
            evaluation = await harness._evaluate(
                PolicyContext(
                    hook="before_memory_write",
                    agent_context=AgentContext(
                        messages=list(harness.messages),
                        tools=harness.tools.all(),
                    ),
                    arguments={
                        "content": entry.content,
                        "tags": list(entry.tags),
                    },
                )
            )
            if evaluation.final.action in {"block", "require_confirmation"}:
                raise ValueError(
                    evaluation.final.reason or "Memory write blocked by Policy"
                )
            entry.metadata.update(
                source_session_id=harness.session.session_id,
                source_run_id=harness.agent.current_run_id,
                created_by="agent",
                entry_version=1,
            )
            await harness.agent.emit_event(
                CoreEvent(
                    type="memory_write_start",
                    data={"entry_id": entry.id, "tags": list(entry.tags)},
                )
            )
        try:
            stored = self.store.add(entry)
        except MemoryPersistenceError as exc:
            if harness is not None:
                await harness.agent.emit_event(
                    CoreEvent(
                        type="memory_write_error",
                        data={
                            "entry_id": entry.id,
                            "error": str(exc),
                        },
                    )
                )
            raise
        if harness is not None:
            await harness.agent.emit_event(
                CoreEvent(
                    type="memory_write_end",
                    data={"entry_id": stored.id, "tags": list(stored.tags)},
                )
            )
            await harness._evaluate(
                PolicyContext(
                    hook="after_memory_write",
                    agent_context=AgentContext(
                        messages=list(harness.messages),
                        tools=harness.tools.all(),
                    ),
                    arguments={"entry_id": stored.id, "tags": list(stored.tags)},
                )
            )
        return stored


__all__ = ["MemoryService"]
