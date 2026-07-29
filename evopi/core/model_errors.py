"""Provider-neutral model failures and retry configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias


ModelErrorKind: TypeAlias = Literal[
    "authentication",
    "permission",
    "invalid_request",
    "not_found",
    "context_overflow",
    "quota_exhausted",
    "rate_limited",
    "overloaded",
    "timeout",
    "connection",
    "server",
    "protocol",
    "route_unavailable",
    "unknown",
]

RETRYABLE_MODEL_ERROR_KINDS: frozenset[ModelErrorKind] = frozenset(
    {"rate_limited", "overloaded", "timeout", "connection", "server"}
)


@dataclass(slots=True, frozen=True, kw_only=True)
class ModelErrorInfo:
    """Safe, normalized information about one model-provider failure."""

    kind: ModelErrorKind
    message: str
    provider: str
    retryable: bool
    status_code: int | None = None
    code: str | None = None
    retry_after: float | None = None
    request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.retry_after is not None and self.retry_after < 0:
            raise ValueError("retry_after cannot be negative")
        object.__setattr__(self, "message", self.message[:1000])
        object.__setattr__(self, "metadata", dict(self.metadata))


class ModelError(RuntimeError):
    """A model failure carrying provider-neutral structured information."""

    def __init__(self, info: ModelErrorInfo) -> None:
        self.info = info
        super().__init__(info.message)


@dataclass(slots=True, frozen=True, kw_only=True)
class ModelRetryConfig:
    """Core retry budget for complete model attempts."""

    enabled: bool = False
    max_retries: int = 3
    base_delay: float = 2.0
    max_delay: float = 60.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.base_delay < 0:
            raise ValueError("base_delay cannot be negative")
        if self.max_delay < 0:
            raise ValueError("max_delay cannot be negative")


def error_info_from_exception(error: BaseException) -> ModelErrorInfo | None:
    return error.info if isinstance(error, ModelError) else None


__all__ = [
    "ModelError",
    "ModelErrorInfo",
    "ModelErrorKind",
    "ModelRetryConfig",
    "RETRYABLE_MODEL_ERROR_KINDS",
    "error_info_from_exception",
]
