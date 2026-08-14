from __future__ import annotations

import json
from pathlib import Path

import pytest

from evopi.remote import RemoteDeviceKeyStore, RemoteStoreError


def test_python_device_key_store_round_trips_private_identity(tmp_path: Path) -> None:
    store = RemoteDeviceKeyStore(tmp_path)
    created = store.create("laptop")
    loaded = store.load("laptop")

    assert loaded.device_name == "laptop"
    assert loaded.public_jwk == created.public_jwk
    assert loaded.fingerprint == created.fingerprint
    assert store.path_for("laptop").joinpath("private-key.pem").exists()


def test_python_device_key_store_rejects_boolean_schema_version(tmp_path: Path) -> None:
    store = RemoteDeviceKeyStore(tmp_path)
    store.create("laptop")
    metadata_path = store.path_for("laptop") / "device.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["schema_version"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(RemoteStoreError, match="metadata"):
        store.load("laptop")


def test_python_device_key_store_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    store = RemoteDeviceKeyStore(tmp_path)
    store.create("laptop")
    metadata_path = store.path_for("laptop") / "device.json"
    payload = metadata_path.read_text(encoding="utf-8").replace(
        '"schema_version":1', '"schema_version":1,"schema_version":1'
    )
    metadata_path.write_text(payload, encoding="utf-8")

    with pytest.raises(RemoteStoreError, match="identity"):
        store.load("laptop")
