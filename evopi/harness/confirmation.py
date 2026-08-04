"""Transport-neutral contracts for human confirmation."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Awaitable, Callable, Literal, Protocol, TypeAlias
from uuid import uuid4

from evopi.core.tool import ToolCall
from evopi.core.types import JsonObject, Metadata
from evopi.policy.types import HookName, RiskLevel

ConfirmationDecision: TypeAlias = Literal["approve", "deny", "cancelled"]

ConfirmationStatus: TypeAlias = Literal[
    "pending", "approved", "denied", "cancelled", "expired", "orphaned"
]


def _new_confirmation_id() -> str:
    return uuid4().hex


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True, kw_only=True)
class ConfirmationRequest:
    """A policy-triggered operation that requires a human decision."""

    hook: HookName
    reason: str
    risk_level: RiskLevel = "low"
    policy_names: tuple[str, ...] = ()
    tool_call: ToolCall | None = None
    arguments: JsonObject | None = None
    id: str = field(default_factory=_new_confirmation_id)
    created_at: datetime = field(default_factory=_utc_now)
    metadata: Metadata = field(default_factory=dict)
    # Optional correlation and expiry fields; all existing constructor behavior
    # is preserved when these are omitted.
    run_id: str | None = None
    session_id: str | None = None
    expires_at: datetime | None = None


@dataclass(slots=True, kw_only=True)
class ConfirmationResponse:
    """A human decision correlated to one confirmation request."""

    request_id: str
    decision: ConfirmationDecision
    reason: str = ""
    metadata: Metadata = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.decision == "approve"


ConfirmationHandler: TypeAlias = Callable[
    ...,
    Awaitable[ConfirmationResponse] | ConfirmationResponse,
]


@dataclass(slots=True, frozen=True, kw_only=True)
class ConfirmationSettings:
    """Timeout policy for waiting on a confirmation decision."""

    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        timeout = self.timeout_seconds
        if timeout is None:
            return
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout_seconds must be a finite number or None")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(
                "timeout_seconds must be finite and strictly positive when set"
            )


@dataclass(slots=True, frozen=True, kw_only=True)
class ConfirmationRecord:
    """One durable, versioned snapshot of a confirmation request."""

    request: ConfirmationRequest
    status: ConfirmationStatus
    runtime_id: str
    revision: int
    response: ConfirmationResponse | None = None
    updated_at: datetime


@dataclass(slots=True, frozen=True, kw_only=True)
class ConfirmationTransition:
    """One optimistic status transition for a known request."""

    request_id: str
    expected_revision: int
    status: ConfirmationStatus
    response: ConfirmationResponse | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class ConfirmationBatchResponse:
    """An atomic set of human decisions for explicit pending request ids."""

    responses: tuple[ConfirmationResponse, ...]


class ConfirmationStore(Protocol):
    """Durable state machine for confirmation records.

    Implementations must fail closed on format, lock, conflict, expiry, and
    orphan states, and must never serialize live objects such as Futures,
    coroutines, Tool registries, Policy instances, or handlers.
    """

    def create(self, record: ConfirmationRecord) -> None: ...

    def get(self, request_id: str) -> ConfirmationRecord | None: ...

    def list_pending(self) -> tuple[ConfirmationRecord, ...]: ...

    def transition(
        self,
        request_id: str,
        *,
        expected_revision: int,
        status: ConfirmationStatus,
        response: ConfirmationResponse | None,
    ) -> ConfirmationRecord: ...

    def transition_batch(
        self,
        transitions: tuple[ConfirmationTransition, ...],
    ) -> tuple[ConfirmationRecord, ...]: ...

    def recover_orphans(self, *, runtime_id: str) -> tuple[ConfirmationRecord, ...]: ...

    def close(self) -> None: ...


class ConfirmationError(Exception):
    """Base class for structured confirmation protocol errors.

    Each error carries a stable machine-readable ``code`` and optional
    JSON-safe ``details`` so hosts can react without parsing messages.
    """

    code: str = "confirmation_error"

    def __init__(self, message: str, *, details: JsonObject | None = None) -> None:
        super().__init__(message)
        self.details: JsonObject = dict(details or {})


class ConfirmationFormatError(ConfirmationError):
    code = "invalid_format"


class ConfirmationLockError(ConfirmationError):
    code = "store_locked"


class ConfirmationConflictError(ConfirmationError):
    code = "conflict"


class ConfirmationUnknownRequestError(ConfirmationConflictError):
    code = "unknown_request"


class ConfirmationDuplicateRequestError(ConfirmationConflictError):
    code = "duplicate_request"


class ConfirmationDuplicateResponseError(ConfirmationConflictError):
    code = "duplicate_response"


class ConfirmationStaleRevisionError(ConfirmationConflictError):
    code = "stale_revision"


class ConfirmationExpiredError(ConfirmationConflictError):
    code = "expired"


class ConfirmationOrphanedError(ConfirmationConflictError):
    code = "orphaned"


class ConfirmationStoreClosedError(ConfirmationError):
    code = "store_closed"


class ConfirmationBrokerClosedError(ConfirmationError):
    code = "broker_closed"


__all__ = [
    "ConfirmationBatchResponse",
    "ConfirmationBrokerClosedError",
    "ConfirmationConflictError",
    "ConfirmationDecision",
    "ConfirmationDuplicateRequestError",
    "ConfirmationDuplicateResponseError",
    "ConfirmationError",
    "ConfirmationExpiredError",
    "ConfirmationFormatError",
    "ConfirmationHandler",
    "ConfirmationLockError",
    "ConfirmationOrphanedError",
    "ConfirmationRecord",
    "ConfirmationRequest",
    "ConfirmationResponse",
    "ConfirmationSettings",
    "ConfirmationStaleRevisionError",
    "ConfirmationStatus",
    "ConfirmationStore",
    "ConfirmationStoreClosedError",
    "ConfirmationTransition",
    "ConfirmationUnknownRequestError",
]
