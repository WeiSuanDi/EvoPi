import os

from dotenv import load_dotenv

from evopi.ai.api.anthropic_messages import AnthropicMessagesModel
from evopi.ai.api.openai_chat_completions import OpenAICompatibleModel


def model_from_environment(
    provider: str | None = None,
    *,
    timeout: float = 120.0,
    model: str | None = None,
    context_window: int = 0,
):
    load_dotenv()
    selected = (provider or os.getenv("EVOPI_PROVIDER") or "anthropic").lower()
    if selected == "anthropic":
        resolved_model = model or os.getenv("ANTHROPIC_MODEL")
        if not resolved_model:
            raise ValueError("ANTHROPIC_MODEL is required")
        return AnthropicMessagesModel(
            model=resolved_model,
            base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            timeout=timeout,
            context_window=context_window,
        )
    if selected in {"openai", "openai-compatible"}:
        resolved_model = model or os.getenv("OPENAI_MODEL") or os.getenv("EVOPI_MODEL")
        if not resolved_model:
            raise ValueError("OPENAI_MODEL or EVOPI_MODEL is required")
        return OpenAICompatibleModel(
            model=resolved_model,
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            timeout=timeout,
            context_window=context_window,
        )
    raise ValueError(f"Unknown provider: {selected}")


__all__ = ["AnthropicMessagesModel", "OpenAICompatibleModel", "model_from_environment"]
