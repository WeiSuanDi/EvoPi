from __future__ import annotations

from datetime import timedelta

import pytest

from evopi.remote import PairingRegistry, RemotePairingError, generate_device_key
from evopi.remote.crypto import public_jwk_from_private_key


def test_pairing_code_is_single_use_and_only_creates_pending_request() -> None:
    registry = PairingRegistry()
    issued = registry.issue_code(ttl=timedelta(minutes=10))
    assert len(issued.code.replace("-", "")) == 12

    request = registry.submit(
        code=issued.code,
        device_name="Laptop",
        public_jwk=public_jwk_from_private_key(generate_device_key()),
    )

    assert request.status == "pending"
    assert registry.devices == ()
    with pytest.raises(RemotePairingError, match="invalid or expired"):
        registry.submit(
            code=issued.code,
            device_name="Other",
            public_jwk=public_jwk_from_private_key(generate_device_key()),
        )


def test_local_approval_assigns_normalized_scopes_and_revoke_is_terminal() -> None:
    registry = PairingRegistry()
    issued = registry.issue_code()
    request = registry.submit(
        code=issued.code,
        device_name="Browser",
        public_jwk=public_jwk_from_private_key(generate_device_key()),
    )

    device = registry.approve(request.request_id, scopes=["control", "confirm"])
    assert [scope.value for scope in device.scopes] == ["observe", "control", "confirm"]
    assert registry.pending_requests == ()

    revoked = registry.revoke(device.device_id)
    assert revoked.revoked_at is not None


def test_pairing_state_rejects_boolean_schema_version() -> None:
    with pytest.raises(RemotePairingError, match="unsupported"):
        PairingRegistry.from_dict(
            {"schema_version": True, "codes": [], "requests": [], "devices": []}
        )


def test_pairing_state_rejects_naive_timestamps() -> None:
    with pytest.raises(RemotePairingError, match="malformed"):
        PairingRegistry.from_dict(
            {
                "schema_version": 1,
                "codes": [
                    {"digest": "a" * 64, "expires_at": "2026-08-14T12:00:00"}
                ],
                "requests": [],
                "devices": [],
            }
        )
