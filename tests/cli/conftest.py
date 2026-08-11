from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_cli_tests_from_user_model_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep CLI tests independent from a developer's dotenv and user profile."""

    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("EVOPI_HOME", str(tmp_path / "evopi-home"))
    for name in (
        "EVOPI_PROVIDER",
        "EVOPI_MODEL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_MODEL",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def configured_anthropic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide explicit non-secret model configuration to tests that need a runtime."""

    monkeypatch.setenv("EVOPI_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "test-model")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-credential")
