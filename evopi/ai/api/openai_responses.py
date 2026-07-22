"""Compatibility exports for the first OpenAI adapter.

The MVP targets the broadly supported Chat Completions wire protocol. A
native Responses API implementation can be added here without changing Core.
"""

from evopi.ai.api.openai_chat_completions import OpenAICompatibleModel

__all__ = ["OpenAICompatibleModel"]
