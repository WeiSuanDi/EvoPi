"""EvoPi Memory — long-term agent knowledge store.

Memory entries are persisted across sessions and can be injected into the
agent's context via a :class:`ContextProvider`.  The default retrieval is
keyword-based with optional tag filtering.
"""

from evopi.memory.policy import MemoryWritePolicy
from evopi.memory.retriever import MemoryRetriever, TagFilteredRetriever
from evopi.memory.service import MemoryService
from evopi.memory.store import MemoryEntry, MemoryPersistenceError, MemoryStore

__all__ = [
    "MemoryEntry",
    "MemoryPersistenceError",
    "MemoryRetriever",
    "MemoryService",
    "MemoryStore",
    "MemoryWritePolicy",
    "TagFilteredRetriever",
]
