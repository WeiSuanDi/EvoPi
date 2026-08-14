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
from .admin_service import RemoteAdminService
from .audit import RemoteAuditEntry, RemoteAuditLog, verify_remote_audit_chain
from .authorization import RemoteAuthorizedRpcHost
from .connections import RemoteConnectionRegistry, RemoteSendQueue
from .client import (
    EvoPiRemoteClient,
    RemoteClientConfig,
    RemoteLeaseInfo,
    RemotePairingSubmission,
    RemoteRunHandle,
    RemoteWebSocket,
    submit_remote_pairing,
)
from .crypto import (
    create_auth_challenge,
    generate_device_key,
    jwk_fingerprint,
    public_jwk_from_private_key,
    sign_auth_challenge,
    verify_auth_challenge,
)
from .device_keys import RemoteDeviceIdentity, RemoteDeviceKeyStore
from .controller import RemoteHostController
from .errors import (
    RemoteAuthenticationError,
    RemoteAuditError,
    RemoteAuthorizationError,
    RemoteContractError,
    RemoteConnectionError,
    RemoteError,
    RemoteLeaseError,
    RemoteOutcomeUnknownError,
    RemotePairingError,
    RemoteRateLimitError,
    RemoteSecurityError,
    RemoteStoreError,
)
from .gateway import REMOTE_SUBPROTOCOL, RemoteGateway, challenge_from_dict
from .lease import ControlLease, ControlLeaseManager
from .limits import RemoteRateLimiter
from .models import (
    AuthChallenge,
    DeviceRecord,
    DeviceScope,
    PairingCode,
    PairingRequest,
    normalize_scopes,
)
from .pairing import PairingRegistry
from .protocol import (
    MAX_INBOUND_FRAME_BYTES,
    MAX_OUTBOUND_FRAME_BYTES,
    RemoteFrame,
    RemoteFrameCodec,
    RemoteProtocolError,
    remote_frame,
)
from .security import (
    RemoteGatewayConfig,
    create_server_ssl_context,
    is_trusted_proxy_peer,
    resolve_remote_client_ip,
    validate_remote_request,
)
from .serve import ensure_tls_files, serve_remote_gateway
from .store import RemoteHostConfig, RemoteHostStore

__all__ = [
    "AuthChallenge",
    "ControlLease",
    "ControlLeaseManager",
    "DeviceRecord",
    "DeviceScope",
    "EvoPiRemoteClient",
    "MAX_INBOUND_FRAME_BYTES",
    "MAX_OUTBOUND_FRAME_BYTES",
    "PairingCode",
    "PairingRegistry",
    "PairingRequest",
    "REMOTE_SUBPROTOCOL",
    "RemoteAdminClient",
    "RemoteAdminCodec",
    "RemoteAdminEndpoint",
    "RemoteAdminProtocolError",
    "RemoteAdminRequest",
    "RemoteAdminResponse",
    "RemoteAdminServer",
    "RemoteAdminService",
    "RemoteAuthenticationError",
    "RemoteAuthorizedRpcHost",
    "RemoteAuditEntry",
    "RemoteAuditError",
    "RemoteAuditLog",
    "RemoteAuthorizationError",
    "RemoteContractError",
    "RemoteConnectionRegistry",
    "RemoteConnectionError",
    "RemoteError",
    "RemoteDeviceIdentity",
    "RemoteDeviceKeyStore",
    "RemoteGatewayConfig",
    "RemoteGateway",
    "RemoteClientConfig",
    "RemoteHostConfig",
    "RemoteHostController",
    "RemoteHostStore",
    "RemoteLeaseError",
    "RemoteLeaseInfo",
    "RemotePairingSubmission",
    "RemoteOutcomeUnknownError",
    "RemotePairingError",
    "RemoteFrame",
    "RemoteFrameCodec",
    "RemoteProtocolError",
    "RemoteRateLimitError",
    "RemoteRateLimiter",
    "RemoteSecurityError",
    "RemoteSendQueue",
    "RemoteRunHandle",
    "RemoteWebSocket",
    "RemoteStoreError",
    "create_auth_challenge",
    "challenge_from_dict",
    "create_server_ssl_context",
    "ensure_tls_files",
    "generate_device_key",
    "jwk_fingerprint",
    "is_trusted_proxy_peer",
    "normalize_scopes",
    "public_jwk_from_private_key",
    "resolve_admin_endpoint",
    "resolve_remote_client_ip",
    "remote_frame",
    "sign_auth_challenge",
    "serve_remote_gateway",
    "submit_remote_pairing",
    "verify_auth_challenge",
    "validate_remote_request",
    "verify_remote_audit_chain",
]
