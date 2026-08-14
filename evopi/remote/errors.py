"""Structured failures for the Remote trust layer."""

from __future__ import annotations


class RemoteError(RuntimeError):
    """Base class for public Remote errors."""


class RemoteContractError(RemoteError):
    """Raised when an identity or protocol object is malformed."""


class RemoteAuthenticationError(RemoteError):
    """Raised when a device cannot prove possession of its key."""


class RemotePairingError(RemoteError):
    """Raised when a pairing transition is invalid."""


class RemoteStoreError(RemoteError):
    """Raised when trusted Remote state cannot be persisted."""


class RemoteAuthorizationError(RemoteError):
    """Raised when a device lacks the required scope."""


class RemoteLeaseError(RemoteError):
    """Raised when a control lease transition is invalid."""


class RemoteRateLimitError(RemoteError):
    """Raised when a Remote resource or request limit is exceeded."""


class RemoteSecurityError(RemoteError):
    """Raised when network security configuration or metadata is unsafe."""


class RemoteAuditError(RemoteError):
    """Raised when the mandatory Remote audit trail cannot be trusted."""


__all__ = [
    "RemoteAuthenticationError",
    "RemoteAuditError",
    "RemoteAuthorizationError",
    "RemoteContractError",
    "RemoteError",
    "RemotePairingError",
    "RemoteLeaseError",
    "RemoteRateLimitError",
    "RemoteSecurityError",
    "RemoteStoreError",
]
