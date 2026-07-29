from __future__ import annotations

from dataclasses import asdict

import pytest

from evopi.ai import ModelEnvironmentConfig, resolve_model_environment


def test_resolve_model_environment_is_safe_and_provider_normalized(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-value")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1/")

    config = resolve_model_environment("openai")

    assert config == ModelEnvironmentConfig(
        provider="openai-compatible",
        model="gpt-test",
        base_url="https://example.test/v1",
        credential_configured=True,
    )
    assert "secret-value" not in repr(config)
    assert set(asdict(config)) == {
        "provider",
        "model",
        "base_url",
        "credential_configured",
    }


def test_anthropic_credential_accepts_auth_token_or_api_key(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "configured")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")

    config = resolve_model_environment("anthropic")

    assert config.credential_configured is True
    assert config.base_url == "https://api.anthropic.com"


def test_resolve_model_environment_requires_model_and_known_provider(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("EVOPI_MODEL", raising=False)

    with pytest.raises(ValueError, match="OPENAI_MODEL"):
        resolve_model_environment("openai-compatible")
    with pytest.raises(ValueError, match="Unknown provider"):
        resolve_model_environment("made-up")
