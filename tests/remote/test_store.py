from __future__ import annotations

import json
from pathlib import Path

import pytest

from evopi.remote import (
    RemoteHostConfig,
    RemoteHostStore,
    RemoteStoreError,
)


def test_host_store_round_trips_strict_profile_without_secrets(tmp_path: Path) -> None:
    store = RemoteHostStore(tmp_path / "remote", permission_hardener=lambda _path: None)
    saved = store.initialize(
        RemoteHostConfig(
            name="home",
            workspace=tmp_path / "workspace",
            model_profile="default",
        )
    )

    loaded = store.load_config("home")
    assert loaded == saved
    assert loaded.host_id
    assert "api_key" not in (store.host_path("home") / "config.json").read_text()


def test_host_store_rejects_unknown_fields_and_symbolic_links(tmp_path: Path) -> None:
    store = RemoteHostStore(tmp_path / "remote", permission_hardener=lambda _path: None)
    store.initialize(RemoteHostConfig(name="home", workspace=tmp_path))
    path = store.host_path("home") / "config.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["unexpected"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RemoteStoreError, match="fields"):
        store.load_config("home")


def test_host_store_rejects_boolean_schema_version(tmp_path: Path) -> None:
    store = RemoteHostStore(tmp_path / "remote", permission_hardener=lambda _path: None)
    store.initialize(RemoteHostConfig(name="home", workspace=tmp_path))
    path = store.host_path("home") / "config.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RemoteStoreError, match="field types"):
        store.load_config("home")


def test_host_store_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    store = RemoteHostStore(tmp_path / "remote", permission_hardener=lambda _path: None)
    store.initialize(RemoteHostConfig(name="home", workspace=tmp_path))
    path = store.host_path("home") / "config.json"
    payload = path.read_text(encoding="utf-8").replace(
        '"schema_version":1', '"schema_version":1,"schema_version":1'
    )
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(RemoteStoreError, match="duplicate"):
        store.load_config("home")


def test_management_secret_is_created_with_permission_hardening(tmp_path: Path) -> None:
    hardened: list[Path] = []
    store = RemoteHostStore(tmp_path / "remote", permission_hardener=hardened.append)
    store.initialize(RemoteHostConfig(name="home", workspace=tmp_path))

    secret = store.load_management_secret("home")
    assert len(secret) == 32
    assert hardened
    assert any("management.key" in path.name for path in hardened)
