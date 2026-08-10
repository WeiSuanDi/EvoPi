"""CLI-only resolution of environment and persisted user model configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from evopi.ai.models import ModelEnvironmentConfig
from evopi.configuration import CredentialStore, UserConfigStore


class IncompleteModelConfigurationError(ValueError):
    """Raised when a non-interactive product entry needs ``evopi setup``."""


@dataclass(slots=True, frozen=True, kw_only=True)
class ResolvedCliModelConfiguration:
    safe: ModelEnvironmentConfig
    api_key: str | None = field(default=None, repr=False)
    profile: str | None = None
    verified: bool | None = None
    sources: dict[str, str]

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "provider": self.safe.provider,
            "model": self.safe.model,
            "base_url": self.safe.base_url,
            "credential_configured": self.safe.credential_configured,
            "profile": self.profile,
            "verified": self.verified,
            "sources": dict(self.sources),
        }


_DEFAULT_URLS = {
    "anthropic": "https://api.anthropic.com",
    "openai-compatible": "https://api.openai.com/v1",
    "openai-responses": "https://api.openai.com/v1",
}


def _normalize_provider(value: str) -> str:
    normalized = value.lower()
    if normalized == "openai":
        return "openai-compatible"
    if normalized not in _DEFAULT_URLS:
        raise ValueError(f"Unknown provider: {value}")
    return normalized


def _environment(provider: str) -> tuple[str | None, str | None, str | None]:
    if provider == "anthropic":
        return (
            os.getenv("ANTHROPIC_MODEL"),
            os.getenv("ANTHROPIC_BASE_URL"),
            (
                os.getenv("ANTHROPIC_AUTH_TOKEN")
                or os.getenv("ANTHROPIC_API_KEY")
                or None
            ),
        )
    return (
        os.getenv("OPENAI_MODEL") or os.getenv("EVOPI_MODEL"),
        os.getenv("OPENAI_BASE_URL"),
        os.getenv("OPENAI_API_KEY") or None,
    )


def resolve_cli_model_configuration(
    provider: str | None = None,
    *,
    model: str | None = None,
    base_url: str | None = None,
    home: Path | None = None,
    require_complete: bool = False,
) -> ResolvedCliModelConfiguration:
    """Resolve CLI > environment/.env > user profile > product defaults."""

    load_dotenv()
    config = UserConfigStore((home / "config.toml") if home else None).load_optional()
    profile = config.active if config is not None else None
    env_provider = os.getenv("EVOPI_PROVIDER")
    selected_provider = _normalize_provider(
        provider or env_provider or (profile.provider if profile else "anthropic")
    )
    matching_profile = profile if profile and profile.provider == selected_provider else None
    env_model, env_url, env_key = _environment(selected_provider)
    selected_model = model or env_model or (matching_profile.model if matching_profile else None)
    selected_url = (
        base_url
        or env_url
        or (matching_profile.base_url if matching_profile else None)
        or _DEFAULT_URLS[selected_provider]
    ).rstrip("/")
    profile_name = matching_profile.name if matching_profile else None
    stored_key = None
    if env_key is None and profile_name is not None:
        stored_key = CredentialStore((home / "credentials.json") if home else None).key_for(
            profile_name,
            selected_provider,
            selected_url,
        )
    key = env_key or stored_key
    if require_complete and (not selected_model or not key):
        raise IncompleteModelConfigurationError(
            "model configuration is incomplete; run 'evopi setup'"
        )
    if not selected_model:
        raise IncompleteModelConfigurationError(
            "model configuration is incomplete; run 'evopi setup'"
        )
    sources = {
        "provider": "cli" if provider else "environment" if env_provider else "user" if profile else "default",
        "model": "cli" if model else "environment" if env_model else "user",
        "base_url": "cli" if base_url else "environment" if env_url else "user" if matching_profile else "default",
        "credential": "environment" if env_key else "user" if stored_key else "missing",
    }
    return ResolvedCliModelConfiguration(
        safe=ModelEnvironmentConfig(
            provider=selected_provider,
            model=selected_model,
            base_url=selected_url,
            credential_configured=key is not None,
        ),
        api_key=key,
        profile=profile_name,
        verified=matching_profile.verified if matching_profile else None,
        sources=sources,
    )


__all__ = [
    "IncompleteModelConfigurationError",
    "ResolvedCliModelConfiguration",
    "resolve_cli_model_configuration",
]
