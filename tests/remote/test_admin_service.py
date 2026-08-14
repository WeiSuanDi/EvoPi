from __future__ import annotations

from pathlib import Path

from evopi.remote import (
    RemoteAdminRequest,
    RemoteAdminService,
    RemoteHostConfig,
    RemoteHostController,
    RemoteHostStore,
    generate_device_key,
    public_jwk_from_private_key,
)


def test_admin_service_owns_pair_approve_scope_and_revoke_transitions(
    tmp_path: Path,
) -> None:
    store = RemoteHostStore(tmp_path / "remote")
    store.initialize(RemoteHostConfig(name="host", workspace=tmp_path))
    controller = RemoteHostController(store, "host")
    disconnected: list[str] = []
    service = RemoteAdminService(controller, disconnect_device=disconnected.append)

    issued = service(RemoteAdminRequest(request_id="1", method="pair.issue", params={}))
    assert issued.ok and issued.result is not None
    request = controller.submit_pairing(
        code=issued.result["code"],
        device_name="phone",
        public_jwk=public_jwk_from_private_key(generate_device_key()),
    )
    approved = service(
        RemoteAdminRequest(
            request_id="2",
            method="requests.approve",
            params={"request_id": request.request_id, "scopes": ["control"]},
        )
    )
    assert approved.ok and approved.result is not None
    device_id = approved.result["device"]["device_id"]

    changed = service(
        RemoteAdminRequest(
            request_id="3",
            method="devices.scopes",
            params={"device_id": device_id, "scopes": ["confirm"]},
        )
    )
    assert changed.ok
    revoked = service(
        RemoteAdminRequest(
            request_id="4",
            method="devices.revoke",
            params={"device_id": device_id},
        )
    )
    assert revoked.ok
    assert disconnected == [device_id, device_id]
