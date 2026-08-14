"""Strict local persistence for Remote Host profiles and security secrets."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from evopi.configuration import harden_credential_permissions
from evopi.evolution.file_lock import EvolutionFileLock, EvolutionStoreLockError

from ._json import StrictRemoteJsonError, decode_strict_json_object
from .errors import RemoteContractError, RemoteStoreError
from .pairing import PairingRegistry

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
_CONFIG_FIELDS = {
    "schema_version",
    "host_id",
    "name",
    "workspace",
    "model_profile",
}


@dataclass(slots=True, frozen=True, kw_only=True)
class RemoteHostConfig:
    name: str
    workspace: Path
    model_profile: str = "default"
    host_id: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise RemoteContractError("unsupported Remote Host schema_version")
        if not _NAME.fullmatch(self.name):
            raise RemoteContractError("Host name must use lowercase letters, digits, and hyphens")
        if not self.model_profile.strip():
            raise RemoteContractError("model_profile must be non-empty")
        if self.host_id and (len(self.host_id) != 32 or not self.host_id.isalnum()):
            raise RemoteContractError("host_id must be a 32-character identifier")


PermissionHardener = Callable[[Path], None]


class RemoteHostStore:
    def __init__(
        self,
        root: Path,
        *,
        permission_hardener: PermissionHardener = harden_credential_permissions,
    ) -> None:
        self.root = root.expanduser().resolve()
        self._permission_hardener = permission_hardener

    def host_path(self, name: str) -> Path:
        if not _NAME.fullmatch(name):
            raise RemoteStoreError("invalid Remote Host name")
        return self.root / "hosts" / name

    def initialize(self, config: RemoteHostConfig) -> RemoteHostConfig:
        host = self.host_path(config.name)
        if host.exists() and any(host.iterdir()):
            raise RemoteStoreError(f"Remote Host already exists: {config.name}")
        host.mkdir(parents=True, exist_ok=True)
        self._make_private_directory(host)
        saved = replace(
            config,
            host_id=config.host_id or uuid4().hex,
            workspace=config.workspace.expanduser().resolve(),
        )
        try:
            with EvolutionFileLock(host / "host.lock"):
                self._atomic_json(host / "config.json", self._config_to_dict(saved))
                self._atomic_secret(host / "management.key", secrets.token_bytes(32))
                self._atomic_json(
                    host / "state.json",
                    PairingRegistry().to_dict(),
                )
        except (OSError, EvolutionStoreLockError) as exc:
            raise RemoteStoreError(f"Remote Host could not be initialized: {exc}") from exc
        return saved

    def load_config(self, name: str) -> RemoteHostConfig:
        host = self.host_path(name)
        path = host / "config.json"
        self._reject_symlink(path)
        raw = self._read_json(path)
        if set(raw) != _CONFIG_FIELDS:
            raise RemoteStoreError("Remote Host config has invalid fields")
        schema_version = raw.get("schema_version")
        host_id = raw.get("host_id")
        config_name = raw.get("name")
        workspace = raw.get("workspace")
        model_profile = raw.get("model_profile")
        if type(schema_version) is not int or schema_version != 1 or not all(
            isinstance(value, str)
            for value in (host_id, config_name, workspace, model_profile)
        ):
            raise RemoteStoreError("Remote Host config has invalid field types")
        assert isinstance(host_id, str)
        assert isinstance(config_name, str)
        assert isinstance(workspace, str)
        assert isinstance(model_profile, str)
        try:
            return RemoteHostConfig(
                schema_version=1,
                host_id=host_id,
                name=config_name,
                workspace=Path(workspace),
                model_profile=model_profile,
            )
        except RemoteContractError as exc:
            raise RemoteStoreError(str(exc)) from exc

    def load_management_secret(self, name: str) -> bytes:
        path = self.host_path(name) / "management.key"
        self._reject_symlink(path)
        try:
            value = path.read_bytes()
        except OSError as exc:
            raise RemoteStoreError(f"management key could not be read: {exc}") from exc
        if len(value) != 32:
            raise RemoteStoreError("management key has an invalid length")
        return value

    def load_registry(self, name: str) -> PairingRegistry:
        path = self.host_path(name) / "state.json"
        self._reject_symlink(path)
        try:
            return PairingRegistry.from_dict(self._read_json(path))
        except Exception as exc:
            raise RemoteStoreError(f"Remote security state is invalid: {exc}") from exc

    def save_registry(self, name: str, registry: PairingRegistry) -> None:
        host = self.host_path(name)
        try:
            with EvolutionFileLock(host / "host.lock"):
                self._atomic_json(host / "state.json", registry.to_dict())
        except (OSError, EvolutionStoreLockError) as exc:
            raise RemoteStoreError(f"Remote security state could not be saved: {exc}") from exc

    @staticmethod
    def _config_to_dict(config: RemoteHostConfig) -> dict[str, object]:
        return {
            "schema_version": config.schema_version,
            "host_id": config.host_id,
            "name": config.name,
            "workspace": str(config.workspace),
            "model_profile": config.model_profile,
        }

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        try:
            raw = decode_strict_json_object(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, StrictRemoteJsonError) as exc:
            raise RemoteStoreError(f"invalid Remote Store JSON: {exc}") from exc
        return raw

    def _atomic_json(self, path: Path, value: object) -> None:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._atomic_secret(path, payload)

    def _atomic_secret(self, path: Path, payload: bytes) -> None:
        self._reject_symlink(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                self._permission_hardener(temporary)
            except OSError as exc:
                raise RemoteStoreError("unable to establish private file permissions") from exc
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _reject_symlink(path: Path) -> None:
        if path.is_symlink() or path.parent.is_symlink():
            raise RemoteStoreError(f"refusing symbolic link: {path}")

    @staticmethod
    def _make_private_directory(path: Path) -> None:
        try:
            path.chmod(0o700)
        except OSError:
            pass


__all__ = ["RemoteHostConfig", "RemoteHostStore"]
