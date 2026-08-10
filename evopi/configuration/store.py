"""Strict, locked and atomic local configuration stores."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

from evopi.configuration.models import (
    CredentialRecord,
    ModelProfile,
    UserConfig,
    UserConfigError,
)

PermissionHardener = Callable[[Path], None]


def resolve_user_config_home() -> Path:
    configured = os.getenv("EVOPI_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".evopi"


class _FileLock(AbstractContextManager["_FileLock"]):
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0)
            if handle.read(1) == b"":
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB  # type: ignore[attr-defined]
                )
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise UserConfigError(f"configuration store is locked: {self.path}") from exc
        self._handle = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        finally:
            handle.close()


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise UserConfigError(f"refusing symbolic link: {path}")


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _encode_config(config: UserConfig) -> bytes:
    lines = [
        f"schema_version = {config.schema_version}",
        f"active_profile = {_toml_string(config.active_profile)}",
        "",
    ]
    for profile in config.profiles:
        lines.extend(
            [
                "[[profiles]]",
                f"name = {_toml_string(profile.name)}",
                f"provider = {_toml_string(profile.provider)}",
                f"model = {_toml_string(profile.model)}",
                f"base_url = {_toml_string(profile.base_url)}",
                f"verified = {'true' if profile.verified else 'false'}",
                "",
            ]
        )
    return "\n".join(lines).encode("utf-8")


def _decode_config(payload: bytes) -> UserConfig:
    try:
        raw = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise UserConfigError(f"invalid config.toml: {exc}") from exc
    expected = {"schema_version", "active_profile", "profiles"}
    unknown = set(raw) - expected
    if unknown:
        raise UserConfigError(f"unknown config fields: {sorted(unknown)}")
    if set(raw) != expected:
        raise UserConfigError("config.toml is missing required fields")
    profiles_raw = raw["profiles"]
    if not isinstance(profiles_raw, list):
        raise UserConfigError("profiles must be an array of tables")
    profiles: list[ModelProfile] = []
    profile_fields = {"name", "provider", "model", "base_url", "verified"}
    for index, item in enumerate(profiles_raw):
        if not isinstance(item, dict) or set(item) != profile_fields:
            raise UserConfigError(f"profile {index} has invalid fields")
        if not all(isinstance(item[key], str) for key in profile_fields - {"verified"}):
            raise UserConfigError(f"profile {index} contains invalid field types")
        if not isinstance(item["verified"], bool):
            raise UserConfigError(f"profile {index} verified must be boolean")
        profiles.append(ModelProfile(**item))
    if not isinstance(raw["schema_version"], int) or not isinstance(raw["active_profile"], str):
        raise UserConfigError("config root fields have invalid types")
    return UserConfig(
        schema_version=raw["schema_version"],
        active_profile=raw["active_profile"],
        profiles=tuple(profiles),
    )


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def harden_credential_permissions(path: Path) -> None:
    """Restrict a credential file to the current user or fail closed."""

    os.chmod(path, 0o600)
    if os.name != "nt":
        if path.stat().st_mode & 0o077:
            raise OSError("credential file permissions are not 0600")
        return
    identity = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    rows = list(csv.reader([identity]))
    if not rows or len(rows[0]) < 2 or not rows[0][1].startswith("S-"):
        raise OSError("unable to resolve current Windows user SID")
    sid = rows[0][1]
    subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(R,W)"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


class UserConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or resolve_user_config_home() / "config.toml"
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def load(self) -> UserConfig:
        _reject_symlink(self.path)
        if not self.path.exists():
            raise UserConfigError(f"user configuration does not exist: {self.path}")
        with _FileLock(self._lock_path):
            return _decode_config(self.path.read_bytes())

    def load_optional(self) -> UserConfig | None:
        _reject_symlink(self.path)
        return self.load() if self.path.exists() else None

    def save(self, config: UserConfig) -> None:
        _reject_symlink(self.path)
        with _FileLock(self._lock_path):
            temporary = _atomic_write(self.path, _encode_config(config))
            try:
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)


class CredentialStore:
    def __init__(
        self,
        path: Path | None = None,
        *,
        permission_hardener: PermissionHardener = harden_credential_permissions,
    ) -> None:
        self.path = path or resolve_user_config_home() / "credentials.json"
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._permission_hardener = permission_hardener

    def load(self) -> tuple[CredentialRecord, ...]:
        _reject_symlink(self.path)
        if not self.path.exists():
            return ()
        with _FileLock(self._lock_path):
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise UserConfigError(f"invalid credentials.json: {exc}") from exc
        if not isinstance(raw, dict) or set(raw) != {"schema_version", "credentials"}:
            raise UserConfigError("credentials.json has invalid root fields")
        if raw["schema_version"] != 1 or not isinstance(raw["credentials"], list):
            raise UserConfigError("credentials.json has unsupported structure")
        expected = {"profile", "provider", "base_url", "api_key"}
        records: list[CredentialRecord] = []
        for index, item in enumerate(raw["credentials"]):
            if (
                not isinstance(item, dict)
                or set(item) != expected
                or not all(isinstance(value, str) for value in item.values())
            ):
                raise UserConfigError(f"credential {index} has invalid fields")
            records.append(CredentialRecord(**item))
        identities = [(item.profile, item.provider, item.base_url) for item in records]
        if len(identities) != len(set(identities)):
            raise UserConfigError("credential identities must be unique")
        return tuple(records)

    def save(self, records: Iterable[CredentialRecord]) -> None:
        _reject_symlink(self.path)
        items = tuple(records)
        payload = json.dumps(
            {
                "schema_version": 1,
                "credentials": [
                    {
                        "profile": item.profile,
                        "provider": item.provider,
                        "base_url": item.base_url,
                        "api_key": item.api_key,
                    }
                    for item in items
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        with _FileLock(self._lock_path):
            temporary = _atomic_write(self.path, payload)
            try:
                try:
                    self._permission_hardener(temporary)
                except OSError as exc:
                    raise UserConfigError("unable to establish secure credential permissions") from exc
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def key_for(self, profile: str, provider: str, base_url: str) -> str | None:
        for record in self.load():
            if (
                record.profile == profile
                and record.provider == provider
                and record.base_url.rstrip("/") == base_url.rstrip("/")
            ):
                return record.api_key
        return None


__all__ = [
    "CredentialStore",
    "PermissionHardener",
    "UserConfigStore",
    "harden_credential_permissions",
    "resolve_user_config_home",
]
