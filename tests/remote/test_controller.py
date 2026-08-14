from __future__ import annotations

from pathlib import Path

from evopi.remote import (
    RemoteHostConfig,
    RemoteHostController,
    RemoteHostStore,
    generate_device_key,
    public_jwk_from_private_key,
)


def test_pairing_state_survives_controller_restart_without_plaintext_code(
    tmp_path: Path,
) -> None:
    store = RemoteHostStore(tmp_path / "remote", permission_hardener=lambda _path: None)
    store.initialize(RemoteHostConfig(name="home", workspace=tmp_path))
    first = RemoteHostController(store, "home")
    issued = first.issue_pairing_code()
    state_path = store.host_path("home") / "state.json"
    assert issued.code not in state_path.read_text(encoding="utf-8")

    request = first.submit_pairing(
        code=issued.code,
        device_name="Laptop",
        public_jwk=public_jwk_from_private_key(generate_device_key()),
    )

    restored = RemoteHostController(store, "home")
    assert restored.pending_requests[0].request_id == request.request_id
    device = restored.approve(request.request_id, scopes=["control"])
    assert device.active

    again = RemoteHostController(store, "home")
    assert again.devices[0].device_id == device.device_id
