from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from evopi.configuration import (
    CredentialRecord,
    CredentialStore,
    ModelProfile,
    UserConfig,
    UserConfigError,
    UserConfigStore,
)
from evopi.cli.model_configuration import resolve_cli_model_configuration
from evopi.cli.repl import build_repl_startup_config


def test_config_and_credentials_round_trip_without_exposing_secret(tmp_path: Path) -> None:
    config_store = UserConfigStore(tmp_path / "config.toml")
    credential_store = CredentialStore(
        tmp_path / "credentials.json",
        permission_hardener=lambda path: None,
    )
    config = UserConfig(
        active_profile="default",
        profiles=(
            ModelProfile(
                name="default",
                provider="openai-responses",
                model="gpt-test",
                base_url="https://api.openai.com/v1",
                verified=True,
            ),
        ),
    )
    credential = CredentialRecord(
        profile="default",
        provider="openai-responses",
        base_url="https://api.openai.com/v1",
        api_key="secret-value",
    )

    config_store.save(config)
    credential_store.save((credential,))

    assert config_store.load() == config
    assert credential_store.load() == (credential,)
    assert "secret-value" not in repr(credential)
    if os.name != "nt":
        assert os.stat(tmp_path / "credentials.json").st_mode & 0o077 == 0


def test_config_rejects_unknown_fields_and_symlinks(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'schema_version = 1\nactive_profile = "default"\nunknown = true\nprofiles = []\n',
        encoding="utf-8",
    )
    with pytest.raises(UserConfigError, match="unknown"):
        UserConfigStore(path).load()

    target = tmp_path / "real.toml"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "link.toml"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(UserConfigError, match="symbolic link"):
        UserConfigStore(link).load()


def test_permission_failure_refuses_to_save_api_key(tmp_path: Path) -> None:
    def fail(_: Path) -> None:
        raise OSError("ACL failed")

    store = CredentialStore(tmp_path / "credentials.json", permission_hardener=fail)
    record = CredentialRecord(
        profile="default",
        provider="anthropic",
        base_url="https://api.anthropic.com",
        api_key="secret",
    )
    with pytest.raises(UserConfigError, match="secure credential permissions"):
        store.save((record,))
    assert not (tmp_path / "credentials.json").exists()


def test_cli_environment_overrides_user_profile_and_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    UserConfigStore(tmp_path / "config.toml").save(
        UserConfig(
            active_profile="default",
            profiles=(
                ModelProfile(
                    name="default",
                    provider="anthropic",
                    model="stored-model",
                    base_url="https://stored.example",
                    verified=True,
                ),
            ),
        )
    )
    CredentialStore(
        tmp_path / "credentials.json", permission_hardener=lambda path: None
    ).save(
        (
            CredentialRecord(
                profile="default",
                provider="anthropic",
                base_url="https://stored.example",
                api_key="stored-secret",
            ),
        )
    )
    monkeypatch.setenv("ANTHROPIC_MODEL", "env-model")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://env.example")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-secret")

    resolved = resolve_cli_model_configuration(home=tmp_path)

    assert resolved.safe.model == "env-model"
    assert resolved.safe.base_url == "https://env.example"
    assert resolved.api_key == "env-secret"
    assert resolved.sources["model"] == "environment"
    assert "env-secret" not in repr(resolved)


def test_stored_key_is_not_reused_for_changed_base_url(tmp_path: Path) -> None:
    UserConfigStore(tmp_path / "config.toml").save(
        UserConfig(
            active_profile="default",
            profiles=(
                ModelProfile(
                    name="default",
                    provider="openai-compatible",
                    model="local-model",
                    base_url="https://old.example/v1",
                    verified=False,
                ),
            ),
        )
    )
    CredentialStore(
        tmp_path / "credentials.json", permission_hardener=lambda path: None
    ).save(
        (
            CredentialRecord(
                profile="default",
                provider="openai-compatible",
                base_url="https://old.example/v1",
                api_key="stored-secret",
            ),
        )
    )

    resolved = resolve_cli_model_configuration(
        provider="openai-compatible",
        model="local-model",
        base_url="https://new.example/v1",
        home=tmp_path,
    )

    assert resolved.api_key is None
    assert resolved.safe.credential_configured is False
    assert "stored-secret" not in json.dumps(resolved.to_safe_dict())


def test_cli_can_resolve_an_explicit_non_active_host_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "EVOPI_PROVIDER",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_MODEL",
        "EVOPI_MODEL",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
    ):
        monkeypatch.setenv(name, "")
    UserConfigStore(tmp_path / "config.toml").save(
        UserConfig(
            active_profile="default",
            profiles=(
                ModelProfile(
                    name="default",
                    provider="anthropic",
                    model="default-model",
                    base_url="https://default.example",
                    verified=True,
                ),
                ModelProfile(
                    name="remote-host",
                    provider="openai-responses",
                    model="remote-model",
                    base_url="https://remote.example/v1",
                    verified=True,
                ),
            ),
        )
    )
    CredentialStore(
        tmp_path / "credentials.json", permission_hardener=lambda path: None
    ).save(
        (
            CredentialRecord(
                profile="remote-host",
                provider="openai-responses",
                base_url="https://remote.example/v1",
                api_key="remote-secret",
            ),
        )
    )

    resolved = resolve_cli_model_configuration(
        home=tmp_path, profile_name="remote-host", require_complete=True
    )

    assert resolved.profile == "remote-host"
    assert resolved.safe.model == "remote-model"
    assert resolved.api_key == "remote-secret"


def test_repl_settings_use_the_same_persisted_configuration_resolver(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    UserConfigStore(tmp_path / "config.toml").save(
        UserConfig(
            active_profile="default",
            profiles=(
                ModelProfile(
                    name="default",
                    provider="openai-responses",
                    model="stored-model",
                    base_url="https://api.openai.com/v1",
                    verified=True,
                ),
            ),
        )
    )
    CredentialStore(
        tmp_path / "credentials.json", permission_hardener=lambda path: None
    ).save(
        (
            CredentialRecord(
                profile="default",
                provider="openai-responses",
                base_url="https://api.openai.com/v1",
                api_key="stored-secret",
            ),
        )
    )
    monkeypatch.setenv("EVOPI_HOME", str(tmp_path))
    for name in (
        "EVOPI_PROVIDER",
        "OPENAI_MODEL",
        "EVOPI_MODEL",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
    ):
        monkeypatch.setenv(name, "")
    args = SimpleNamespace(provider=None, model=None, base_url=None)
    harness = SimpleNamespace(
        model=SimpleNamespace(name="stored-model", base_url="https://api.openai.com/v1"),
        model_route=None,
        workspace=tmp_path,
        session=SimpleNamespace(is_persistent=True),
        agent=SimpleNamespace(max_turns=20),
        shell_environment=SimpleNamespace(
            requested_mode="auto", kind="cmd", executable="cmd.exe"
        ),
    )

    startup = build_repl_startup_config(args, harness)

    assert startup.provider == "openai-responses"
    assert startup.profile == "default"
    assert startup.profile_verified is True
    assert startup.configuration_source == "user"
    assert startup.credential_configured is True
