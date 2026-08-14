from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evopi.remote import (
    DeviceScope,
    RemoteAuthenticationError,
    RemoteProtocolError,
    challenge_from_dict,
    create_auth_challenge,
    generate_device_key,
    normalize_scopes,
    public_jwk_from_private_key,
    sign_auth_challenge,
    verify_auth_challenge,
)


def test_control_scope_implies_observe_but_confirm_is_independent() -> None:
    assert normalize_scopes(["control"]) == (
        DeviceScope.OBSERVE,
        DeviceScope.CONTROL,
    )
    assert normalize_scopes(["confirm"]) == (DeviceScope.CONFIRM,)


def test_p256_challenge_signature_is_bound_to_host_device_and_connection() -> None:
    private_key = generate_device_key()
    public_jwk = public_jwk_from_private_key(private_key)
    challenge = create_auth_challenge(
        host_id="a" * 32,
        device_id="b" * 32,
        connection_id="c" * 32,
    )
    signature = sign_auth_challenge(private_key, challenge)

    assert len(signature) == 86  # base64url for fixed 64-byte r || s
    verify_auth_challenge(public_jwk, challenge, signature)

    changed = challenge.with_connection_id("d" * 32)
    with pytest.raises(RemoteAuthenticationError):
        verify_auth_challenge(public_jwk, changed, signature)


def test_expired_challenge_is_rejected() -> None:
    private_key = generate_device_key()
    challenge = create_auth_challenge(
        host_id="a" * 32,
        device_id="b" * 32,
        connection_id="c" * 32,
        ttl=timedelta(seconds=-1),
    )
    signature = sign_auth_challenge(private_key, challenge)

    with pytest.raises(RemoteAuthenticationError, match="expired"):
        verify_auth_challenge(
            public_jwk_from_private_key(private_key), challenge, signature
        )


def test_remote_challenge_codec_rejects_naive_timestamps() -> None:
    with pytest.raises(RemoteProtocolError, match="timestamp"):
        challenge_from_dict(
            {
                "protocol": "evopi.remote.v1",
                "host_id": "a" * 32,
                "device_id": "b" * 32,
                "connection_id": "c" * 32,
                "nonce": "nonce",
                "issued_at": "2026-08-14T12:00:00",
                "expires_at": "2026-08-14T12:00:30",
            }
        )


def test_remote_challenge_codec_rejects_nonpositive_lifetime() -> None:
    instant = datetime(2026, 8, 14, 12, tzinfo=UTC).isoformat()

    with pytest.raises(RemoteProtocolError, match="lifetime"):
        challenge_from_dict(
            {
                "protocol": "evopi.remote.v1",
                "host_id": "a" * 32,
                "device_id": "b" * 32,
                "connection_id": "c" * 32,
                "nonce": "nonce",
                "issued_at": instant,
                "expires_at": instant,
            }
        )
