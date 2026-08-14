from __future__ import annotations

from pathlib import Path

from evopi.remote import RemoteDeviceKeyStore


def test_python_device_key_store_round_trips_private_identity(tmp_path: Path) -> None:
    store = RemoteDeviceKeyStore(tmp_path)
    created = store.create("laptop")
    loaded = store.load("laptop")

    assert loaded.device_name == "laptop"
    assert loaded.public_jwk == created.public_jwk
    assert loaded.fingerprint == created.fingerprint
    assert store.path_for("laptop").joinpath("private-key.pem").exists()
