"""Public Remote trust and transport API."""

from __future__ import annotations

from .admin import (
    RemoteAdminClient,
    RemoteAdminCodec,
    RemoteAdminEndpoint,
    RemoteAdminProtocolError,
    RemoteAdminRequest,
    RemoteAdminResponse,
    RemoteAdminServer,
    resolve_admin_endpoint,
)
from .crypto import (
    create_auth_challenge,
    generate_device_key,
    jwk_fingerprint,
    public_jwk_from_private_key,
    sign_auth_challenge,
    verify_auth_challenge,
)
from .controller import RemoteHostController
from .errors import (
    RemoteAuthenticationError,
    RemoteAuthorizationError,
    RemoteContractError,
    RemoteError,
    RemotePairingError,
    RemoteStoreError,
)
from .models import (
    AuthChallenge,
    DeviceRecord,
    DeviceScope,
    PairingCode,
    PairingRequest,
    normalize_scopes,
)
from .pairing import PairingRegistry
from .store import RemoteHostConfig, RemoteHostStore

__all__ = [
    "AuthChallenge",
    "DeviceRecord",
    "DeviceScope",
    "PairingCode",
    "PairingRegistry",
    "PairingRequest",
    "RemoteAdminClient",
    "RemoteAdminCodec",
    "RemoteAdminEndpoint",
    "RemoteAdminProtocolError",
    "RemoteAdminRequest",
    "RemoteAdminResponse",
    "RemoteAdminServer",
    "RemoteAuthenticationError",
    "RemoteAuthorizationError",
    "RemoteContractError",
    "RemoteError",
    "RemoteHostConfig",
    "RemoteHostController",
    "RemoteHostStore",
    "RemotePairingError",
    "RemoteStoreError",
    "create_auth_challenge",
    "generate_device_key",
    "jwk_fingerprint",
    "normalize_scopes",
    "public_jwk_from_private_key",
    "resolve_admin_endpoint",
    "sign_auth_challenge",
    "verify_auth_challenge",
]
