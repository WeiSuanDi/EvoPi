"""Context-aware memory retrieval strategies.

The default implementation is keyword-based.  Future versions can add
embedding-based semantic search without changing the public API.
"""

from __future__ import annotations

from evopi.core.context import AgentContext
from evopi.memory.store import MemoryEntry, MemoryStore


class MemoryRetriever:
    """Query a :class:`MemoryStore` using the current agent context.

    The base implementation performs keyword overlap between the latest user
    message and memory content.  Subclass to add embedding-based or
    metadata-filtered retrieval.
    """

    def __init__(self, store: MemoryStore, *, max_results: int = 5) -> None:
        self._store = store
        self._max_results = max_results

    async def retrieve(self, agent_context: AgentContext) -> list[MemoryEntry]:
        """Return memories relevant to the current *agent_context*."""
        query = self._extract_query(agent_context)
        if not query or not query.strip():
            return []
        return self._store.search(query.strip(), limit=self._max_results)

    def _extract_query(self, context: AgentContext) -> str | None:
        # Use the last user message as the query
        for msg in reversed(context.messages):
            if msg.role == "user":
                return msg.content
        return None


class TagFilteredRetriever(MemoryRetriever):
    """Retrieve only memories with specific tags."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        tags: list[str],
        max_results: int = 5,
    ) -> None:
        super().__init__(store, max_results=max_results)
        self._tags = tags

    async def retrieve(self, agent_context: AgentContext) -> list[MemoryEntry]:
        query = self._extract_query(agent_context)
        if not query or not query.strip():
            return self._store.search("", tags=self._tags, limit=self._max_results)
        return self._store.search(query.strip(), tags=self._tags, limit=self._max_results)


__all__ = ["MemoryRetriever", "TagFilteredRetriever"]
