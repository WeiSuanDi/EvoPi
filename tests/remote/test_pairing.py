from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

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


def test_pairing_state_rejects_unknown_request_fields_and_status() -> None:
    registry = PairingRegistry()
    code = registry.issue_code()
    registry.submit(
        code=code.code,
        device_name="Browser",
        public_jwk=public_jwk_from_private_key(generate_device_key()),
    )
    state = registry.to_dict()
    request = state["requests"][0]

    with_extra = copy.deepcopy(state)
    with_extra["requests"][0]["unexpected"] = True
    invalid_status = copy.deepcopy(state)
    invalid_status["requests"][0]["status"] = "trusted"

    for candidate in (with_extra, invalid_status):
        with pytest.raises(RemotePairingError, match="malformed"):
            PairingRegistry.from_dict(candidate)

    assert request["status"] == "pending"


def test_pairing_state_revalidates_device_identity_and_revision() -> None:
    state = _state_with_approved_device()
    wrong_fingerprint = copy.deepcopy(state)
    wrong_fingerprint["devices"][0]["fingerprint"] = "0" * 64
    boolean_revision = copy.deepcopy(state)
    boolean_revision["devices"][0]["revision"] = True

    for candidate in (wrong_fingerprint, boolean_revision):
        with pytest.raises(RemotePairingError, match="malformed"):
            PairingRegistry.from_dict(candidate)


def test_pairing_state_rejects_duplicate_device_identity() -> None:
    state = _state_with_approved_device()
    state["devices"].append(copy.deepcopy(state["devices"][0]))

    with pytest.raises(RemotePairingError, match="malformed"):
        PairingRegistry.from_dict(state)


def test_pairing_state_rejects_duplicate_pending_identity() -> None:
    registry = PairingRegistry()
    key = public_jwk_from_private_key(generate_device_key())
    first_code = registry.issue_code()
    registry.submit(code=first_code.code, device_name="Browser", public_jwk=key)
    state = registry.to_dict()
    duplicate = copy.deepcopy(state["requests"][0])
    duplicate["request_id"] = "f" * 32
    state["requests"].append(duplicate)

    with pytest.raises(RemotePairingError, match="malformed"):
        PairingRegistry.from_dict(state)


def test_pairing_state_requires_approved_request_device_binding() -> None:
    state = _state_with_approved_device()
    state["devices"] = []

    with pytest.raises(RemotePairingError, match="malformed"):
        PairingRegistry.from_dict(state)


def test_pairing_state_strict_round_trip_preserves_valid_records() -> None:
    state = _state_with_approved_device()

    assert PairingRegistry.from_dict(state).to_dict() == state


def _state_with_approved_device() -> dict[str, Any]:
    registry = PairingRegistry()
    code = registry.issue_code()
    request = registry.submit(
        code=code.code,
        device_name="Browser",
        public_jwk=public_jwk_from_private_key(generate_device_key()),
    )
    registry.approve(request.request_id, scopes=["control"])
    return registry.to_dict()
