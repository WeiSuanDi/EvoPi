"""P-256 device identity and challenge-response primitives."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Mapping

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import (
        decode_dss_signature,
        encode_dss_signature,
    )
except ImportError as exc:  # pragma: no cover - exercised by installation smoke tests.
    raise RuntimeError("Remote support requires 'evopi[remote]'") from exc

from .errors import RemoteAuthenticationError, RemoteContractError
from .models import AuthChallenge


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str, *, size: int | None = None) -> bytes:
    if not value or "=" in value:
        raise RemoteContractError("invalid unpadded base64url value")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, TypeError) as exc:
        raise RemoteContractError("invalid base64url value") from exc
    if size is not None and len(decoded) != size:
        raise RemoteContractError("base64url value has an invalid length")
    return decoded


def generate_device_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def public_jwk_from_private_key(
    private_key: ec.EllipticCurvePrivateKey,
) -> dict[str, str]:
    numbers = private_key.public_key().public_numbers()
    return {
        "crv": "P-256",
        "kty": "EC",
        "x": _b64url_encode(numbers.x.to_bytes(32, "big")),
        "y": _b64url_encode(numbers.y.to_bytes(32, "big")),
    }


def public_key_from_jwk(value: Mapping[str, str]) -> ec.EllipticCurvePublicKey:
    if set(value) != {"crv", "kty", "x", "y"}:
        raise RemoteContractError("P-256 JWK has invalid fields")
    if value["kty"] != "EC" or value["crv"] != "P-256":
        raise RemoteContractError("only P-256 EC JWKs are supported")
    x = int.from_bytes(_b64url_decode(value["x"], size=32), "big")
    y = int.from_bytes(_b64url_decode(value["y"], size=32), "big")
    try:
        return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
    except ValueError as exc:
        raise RemoteContractError("P-256 JWK point is invalid") from exc


def jwk_fingerprint(value: Mapping[str, str]) -> str:
    public_key_from_jwk(value)
    canonical = json.dumps(dict(value), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def create_auth_challenge(
    *,
    host_id: str,
    device_id: str,
    connection_id: str,
    ttl: timedelta = timedelta(seconds=30),
    now: datetime | None = None,
) -> AuthChallenge:
    issued_at = now or datetime.now(UTC)
    return AuthChallenge(
        host_id=host_id,
        device_id=device_id,
        connection_id=connection_id,
        nonce=_b64url_encode(secrets.token_bytes(32)),
        issued_at=issued_at,
        expires_at=issued_at + ttl,
    )


def challenge_payload(challenge: AuthChallenge) -> bytes:
    return json.dumps(
        {
            "connection_id": challenge.connection_id,
            "device_id": challenge.device_id,
            "expires_at": challenge.expires_at.isoformat(),
            "host_id": challenge.host_id,
            "issued_at": challenge.issued_at.isoformat(),
            "nonce": challenge.nonce,
            "protocol": challenge.protocol,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sign_auth_challenge(
    private_key: ec.EllipticCurvePrivateKey, challenge: AuthChallenge
) -> str:
    der = private_key.sign(challenge_payload(challenge), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return _b64url_encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def verify_auth_challenge(
    public_jwk: Mapping[str, str],
    challenge: AuthChallenge,
    signature: str,
    *,
    now: datetime | None = None,
) -> None:
    current = now or datetime.now(UTC)
    if challenge.expires_at <= current:
        raise RemoteAuthenticationError("authentication challenge expired")
    if challenge.issued_at > current + timedelta(seconds=5):
        raise RemoteAuthenticationError("authentication challenge is from the future")
    raw = _b64url_decode(signature, size=64)
    der = encode_dss_signature(
        int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
    )
    try:
        public_key_from_jwk(public_jwk).verify(
            der, challenge_payload(challenge), ec.ECDSA(hashes.SHA256())
        )
    except (InvalidSignature, ValueError, RemoteContractError) as exc:
        raise RemoteAuthenticationError("device signature is invalid") from exc


__all__ = [
    "challenge_payload",
    "create_auth_challenge",
    "generate_device_key",
    "jwk_fingerprint",
    "public_jwk_from_private_key",
    "public_key_from_jwk",
    "sign_auth_challenge",
    "verify_auth_challenge",
]
