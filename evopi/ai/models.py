from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from evopi.ai.api.anthropic_messages import AnthropicMessagesModel
from evopi.ai.api.openai_chat_completions import OpenAICompatibleModel
from evopi.ai.api.openai_responses import OpenAIResponsesModel
from evopi.core.model import Model


@dataclass(slots=True, frozen=True, kw_only=True)
class ModelEnvironmentConfig:
    """Safe effective model configuration without credential values."""

    provider: str
    model: str
    base_url: str
    credential_configured: bool


def resolve_model_environment(
    provider: str | None = None,
    *,
    model: str | None = None,
) -> ModelEnvironmentConfig:
    """Resolve model environment without constructing an adapter or exposing secrets."""

    load_dotenv()
    selected = (provider or os.getenv("EVOPI_PROVIDER") or "anthropic").lower()
    if selected == "openai":
        selected = "openai-compatible"
    if selected == "anthropic":
        resolved_model = model or os.getenv("ANTHROPIC_MODEL")
        if not resolved_model:
            raise ValueError("ANTHROPIC_MODEL is required")
        return ModelEnvironmentConfig(
            provider=selected,
            model=resolved_model,
            base_url=os.getenv(
                "ANTHROPIC_BASE_URL",
                "https://api.anthropic.com",
            ).rstrip("/"),
            credential_configured=bool(
                os.getenv("ANTHROPIC_AUTH_TOKEN")
                or os.getenv("ANTHROPIC_API_KEY")
            ),
        )
    if selected in {"openai-compatible", "openai-responses"}:
        resolved_model = model or os.getenv("OPENAI_MODEL") or os.getenv("EVOPI_MODEL")
        if not resolved_model:
            raise ValueError("OPENAI_MODEL or EVOPI_MODEL is required")
        return ModelEnvironmentConfig(
            provider=selected,
            model=resolved_model,
            base_url=os.getenv(
                "OPENAI_BASE_URL",
                "https://api.openai.com/v1",
            ).rstrip("/"),
            credential_configured=bool(os.getenv("OPENAI_API_KEY")),
        )
    raise ValueError(f"Unknown provider: {selected}")


def model_from_config(
    config: ModelEnvironmentConfig,
    *,
    api_key: str | None = None,
    timeout: float = 120.0,
    context_window: int = 0,
    max_tokens: int = 4096,
) -> Model:
    """Construct a model adapter from an already validated safe configuration."""

    if config.provider == "anthropic":
        return AnthropicMessagesModel(
            model=config.model,
            api_key=api_key,
            base_url=config.base_url,
            timeout=timeout,
            context_window=context_window,
            max_tokens=max_tokens,
        )
    if config.provider == "openai-compatible":
        return OpenAICompatibleModel(
            model=config.model,
            api_key=api_key,
            base_url=config.base_url,
            timeout=timeout,
            context_window=context_window,
            max_tokens=max_tokens,
        )
    if config.provider == "openai-responses":
        return OpenAIResponsesModel(
            model=config.model,
            api_key=api_key,
            base_url=config.base_url,
            timeout=timeout,
            context_window=context_window,
            max_tokens=max_tokens,
        )
    raise ValueError(f"Unknown provider: {config.provider}")


def model_from_environment(
    provider: str | None = None,
    *,
    timeout: float = 120.0,
    model: str | None = None,
    context_window: int = 0,
    max_tokens: int = 4096,
) -> Model:
    return model_from_config(
        resolve_model_environment(provider, model=model),
        timeout=timeout,
        context_window=context_window,
        max_tokens=max_tokens,
    )


__all__ = [
    "AnthropicMessagesModel",
    "ModelEnvironmentConfig",
    "OpenAICompatibleModel",
    "OpenAIResponsesModel",
    "model_from_config",
    "model_from_environment",
    "resolve_model_environment",
]
