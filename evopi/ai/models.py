import os

from dotenv import load_dotenv

from evopi.ai.api.anthropic_messages import AnthropicMessagesModel
from evopi.ai.api.openai_chat_completions import OpenAICompatibleModel


def model_from_environment(provider: str | None = None, *, timeout: float = 120.0):
    load_dotenv()
    selected = (provider or os.getenv("EVOPI_PROVIDER") or "anthropic").lower()
    if selected == "anthropic":
        model = os.getenv("ANTHROPIC_MODEL")
        if not model:
            raise ValueError("ANTHROPIC_MODEL is required")
        return AnthropicMessagesModel(
            model=model,
            base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            timeout=timeout,
        )
    if selected in {"openai", "openai-compatible"}:
        model = os.getenv("OPENAI_MODEL") or os.getenv("EVOPI_MODEL")
        if not model:
            raise ValueError("OPENAI_MODEL or EVOPI_MODEL is required")
        return OpenAICompatibleModel(
            model=model,
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            timeout=timeout,
        )
    raise ValueError(f"Unknown provider: {selected}")


__all__ = ["AnthropicMessagesModel", "OpenAICompatibleModel", "model_from_environment"]
