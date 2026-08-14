"""Permission-protected P-256 identity storage for Python Remote clients."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evopi.configuration import harden_credential_permissions

from .crypto import (
    generate_device_key,
    jwk_fingerprint,
    public_jwk_from_private_key,
)
from ._json import decode_strict_json_object
from .errors import RemoteStoreError

_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


@dataclass(slots=True, frozen=True, kw_only=True)
class RemoteDeviceIdentity:
    device_name: str
    public_jwk: dict[str, str]
    fingerprint: str
    private_key: Any


class RemoteDeviceKeyStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def path_for(self, name: str) -> Path:
        if not _NAME.fullmatch(name):
            raise RemoteStoreError("invalid device identity name")
        return self.root / name

    def create(self, name: str) -> RemoteDeviceIdentity:
        from cryptography.hazmat.primitives import serialization

        directory = self.path_for(name)
        if directory.exists() and any(directory.iterdir()):
            raise RemoteStoreError("device identity already exists")
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        private_key = generate_device_key()
        public_jwk = public_jwk_from_private_key(private_key)
        fingerprint = jwk_fingerprint(public_jwk)
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        self._atomic(directory / "private-key.pem", pem)
        self._atomic(
            directory / "device.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "device_name": name,
                    "public_jwk": public_jwk,
                    "fingerprint": fingerprint,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        return RemoteDeviceIdentity(
            device_name=name,
            public_jwk=public_jwk,
            fingerprint=fingerprint,
            private_key=private_key,
        )

    def load(self, name: str) -> RemoteDeviceIdentity:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        directory = self.path_for(name)
        metadata_path = directory / "device.json"
        key_path = directory / "private-key.pem"
        self._reject_links(metadata_path, key_path)
        try:
            raw = decode_strict_json_object(metadata_path.read_text(encoding="utf-8"))
            pem = key_path.read_bytes()
            private_key = serialization.load_pem_private_key(pem, password=None)
        except (OSError, ValueError) as exc:
            raise RemoteStoreError("device identity is invalid") from exc
        if (
            not isinstance(private_key, ec.EllipticCurvePrivateKey)
            or private_key.curve.name != "secp256r1"
        ):
            raise RemoteStoreError("device identity is not a P-256 EC private key")
        if (
            not isinstance(raw, dict)
            or set(raw) != {"schema_version", "device_name", "public_jwk", "fingerprint"}
            or type(raw.get("schema_version")) is not int
            or raw.get("schema_version") != 1
            or raw.get("device_name") != name
            or not isinstance(raw.get("public_jwk"), dict)
            or not isinstance(raw.get("fingerprint"), str)
        ):
            raise RemoteStoreError("device identity metadata is invalid")
        public_jwk = public_jwk_from_private_key(private_key)
        fingerprint = jwk_fingerprint(public_jwk)
        if raw["public_jwk"] != public_jwk or raw["fingerprint"] != fingerprint:
            raise RemoteStoreError("device identity metadata does not match private key")
        return RemoteDeviceIdentity(
            device_name=name,
            public_jwk=public_jwk,
            fingerprint=fingerprint,
            private_key=private_key,
        )

    @staticmethod
    def _atomic(path: Path, payload: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            harden_credential_permissions(temporary)
            os.replace(temporary, path)
        except OSError as exc:
            raise RemoteStoreError("unable to save device identity securely") from exc
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _reject_links(*paths: Path) -> None:
        if any(path.is_symlink() or path.parent.is_symlink() for path in paths):
            raise RemoteStoreError("refusing symbolic link in device identity")


__all__ = ["RemoteDeviceIdentity", "RemoteDeviceKeyStore"]
