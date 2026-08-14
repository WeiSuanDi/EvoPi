"""Versioned user model configuration and credential records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

_PROVIDERS = {"anthropic", "openai-compatible", "openai-responses"}


class UserConfigError(RuntimeError):
    """Raised when user configuration cannot be safely read or persisted."""


@dataclass(slots=True, frozen=True, kw_only=True)
class ModelProfile:
    name: str
    provider: str
    model: str
    base_url: str
    verified: bool = False

    def __post_init__(self) -> None:
        if not all((self.name, self.provider, self.model, self.base_url)):
            raise UserConfigError("profile fields must be non-empty")
        if self.provider not in _PROVIDERS:
            raise UserConfigError(f"unsupported profile provider: {self.provider}")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise UserConfigError("profile base_url must be an absolute HTTP(S) URL")


@dataclass(slots=True, frozen=True, kw_only=True)
class UserConfig:
    active_profile: str
    profiles: tuple[ModelProfile, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise UserConfigError(f"unsupported config schema_version: {self.schema_version}")
        names = [profile.name for profile in self.profiles]
        if len(names) != len(set(names)):
            raise UserConfigError("profile names must be unique")
        if self.active_profile not in names:
            raise UserConfigError("active_profile does not name an existing profile")

    @property
    def active(self) -> ModelProfile:
        return next(profile for profile in self.profiles if profile.name == self.active_profile)


@dataclass(slots=True, frozen=True, kw_only=True)
class CredentialRecord:
    profile: str
    provider: str
    base_url: str
    api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if not all((self.profile, self.provider, self.base_url, self.api_key)):
            raise UserConfigError("credential fields must be non-empty")
        if self.provider not in _PROVIDERS:
            raise UserConfigError(f"unsupported credential provider: {self.provider}")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise UserConfigError("credential base_url must be an absolute HTTP(S) URL")


def ensure_json_safe(value: Any) -> None:
    if value is None or isinstance(value, str | int | float | bool):
        return
    if isinstance(value, list):
        for item in value:
            ensure_json_safe(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            ensure_json_safe(item)
        return
    raise UserConfigError("configuration contains a non-JSON-safe value")


__all__ = [
    "CredentialRecord",
    "ModelProfile",
    "UserConfig",
    "UserConfigError",
]
