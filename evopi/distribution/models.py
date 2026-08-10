"""Public distribution and update protocol."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class DistributionError(RuntimeError):
    """Raised when an official release cannot be safely consumed."""


class UpdateStatus(str, Enum):
    UP_TO_DATE = "up_to_date"
    UPDATE_AVAILABLE = "update_available"
    UPDATED = "updated"
    ROLLED_BACK = "rolled_back"
    DECLINED = "declined"
    FAILED = "failed"
    UNSUPPORTED_INSTALL = "unsupported_install"


@dataclass(slots=True, frozen=True, kw_only=True)
class ReleaseInfo:
    version: str
    release_url: str
    wheel_name: str
    wheel_url: str
    sha256: str
    checksum_url: str
    provenance_url: str | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class UpdateResult:
    status: UpdateStatus
    current_version: str | None
    target_version: str | None = None
    release_url: str | None = None
    message: str = ""
    warnings: tuple[str, ...] = ()
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "current_version": self.current_version,
            "target_version": self.target_version,
            "release_url": self.release_url,
            "message": self.message,
            "warnings": list(self.warnings),
        }


__all__ = [
    "DistributionError",
    "ReleaseInfo",
    "UpdateResult",
    "UpdateStatus",
]
