"""Versioned user-level EvoPi runtime installation and rollback."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Self

from evopi.distribution.models import ReleaseInfo, UpdateResult, UpdateStatus
from evopi.distribution.release import parse_stable_tag, version_key

RuntimeInstaller = Callable[[ReleaseInfo, bytes, Path], None]


@dataclass(slots=True, frozen=True, kw_only=True)
class _RuntimeMarker:
    version: str
    features: tuple[str, ...]
    sha256: str


class _UpdateLock(AbstractContextManager["_UpdateLock"]):
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
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB  # type: ignore[attr-defined]
                )
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise RuntimeError("another EvoPi update is already running") from exc
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
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        finally:
            handle.close()


class ManagedRuntime:
    def __init__(self, home: Path, *, installer: RuntimeInstaller | None = None) -> None:
        self.home = home.expanduser().resolve()
        self.runtime_root = self.home / "runtime"
        self.versions_root = self.runtime_root / "versions"
        self.current_path = self.runtime_root / "current.txt"
        self.lock_path = self.runtime_root / "update.lock"
        self._installer = installer

    @property
    def is_managed_process(self) -> bool:
        marker = os.getenv("EVOPI_MANAGED_ROOT")
        return marker is not None and Path(marker).expanduser().resolve() == self.home

    @property
    def current_version(self) -> str | None:
        if not self.current_path.exists():
            return None
        value = self.current_runtime_id
        if value is None:
            return None
        version = value.split("--", 1)[0]
        try:
            version_key(version)
        except Exception:
            return None
        return version

    @property
    def current_runtime_id(self) -> str | None:
        if self.current_path.is_symlink() or not self.current_path.is_file():
            return None
        try:
            value = self.current_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        return value if _valid_runtime_id(value) else None

    @property
    def current_features(self) -> tuple[str, ...]:
        runtime_id = self.current_runtime_id
        if runtime_id is None:
            return ()
        try:
            marker = _read_runtime_marker(self.versions_root / runtime_id)
        except RuntimeError:
            return ()
        return marker.features

    def install(
        self,
        info: ReleaseInfo,
        wheel: bytes,
        *,
        features: tuple[str, ...] | None = None,
    ) -> UpdateResult:
        current = self.current_version
        try:
            _validate_release_input(info, wheel)
        except RuntimeError as exc:
            return UpdateResult(
                status=UpdateStatus.FAILED,
                current_version=current,
                target_version=info.version,
                release_url=info.release_url,
                message=f"update failed: {type(exc).__name__}: {str(exc)[:500]}",
            )
        selected_features = _normalize_features(
            self.current_features if features is None else features
        )
        runtime_id = _runtime_id(info.version, selected_features)
        target = self.versions_root / runtime_id
        previous_runtime_id = self.current_runtime_id
        switched = False
        created_target = False
        try:
            with _UpdateLock(self.lock_path):
                self.versions_root.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    self._validate_marker(target, info, selected_features)
                else:
                    created_target = True
                    if self._installer is None:
                        self._install_runtime(info, wheel, target, selected_features)
                    else:
                        self._installer(info, wheel, target)
                    self._write_marker(target, info, selected_features)
                self._switch(runtime_id)
                switched = True
                warnings = self._cleanup_versions(previous_runtime_id, runtime_id)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            if created_target and not switched:
                shutil.rmtree(target, ignore_errors=True)
            return UpdateResult(
                status=UpdateStatus.FAILED,
                current_version=current,
                target_version=info.version,
                release_url=info.release_url,
                message=(
                    f"update failed: {type(exc).__name__}: "
                    f"{str(exc).replace(str(self.home), '<EVOPI_HOME>')[:500]}"
                ),
            )
        return UpdateResult(
            status=UpdateStatus.UPDATED,
            current_version=info.version,
            target_version=info.version,
            release_url=info.release_url,
            message=f"updated EvoPi to {info.version}",
            warnings=warnings,
        )

    def rollback(self) -> UpdateResult:
        current = self.current_version
        try:
            with _UpdateLock(self.lock_path):
                current_id = self.current_runtime_id
                candidates: list[_RuntimeMarker] = []
                for path in self.versions_root.iterdir():
                    if not path.is_dir() or path.name == current_id:
                        continue
                    try:
                        candidates.append(_read_runtime_marker(path))
                    except RuntimeError:
                        continue
                candidates.sort(
                    key=lambda item: version_key(item.version),
                    reverse=True,
                )
                if not candidates:
                    raise RuntimeError("no previous verified EvoPi runtime is available")
                selected = candidates[0]
                target = _runtime_id(selected.version, selected.features)
                self._switch(target)
        except (OSError, RuntimeError) as exc:
            return UpdateResult(
                status=UpdateStatus.FAILED,
                current_version=current,
                message=(
                    f"rollback failed: {type(exc).__name__}: "
                    f"{str(exc).replace(str(self.home), '<EVOPI_HOME>')[:500]}"
                ),
            )
        return UpdateResult(
            status=UpdateStatus.ROLLED_BACK,
            current_version=target.split("--", 1)[0],
            target_version=target.split("--", 1)[0],
            message=f"rolled back EvoPi to {target.split('--', 1)[0]}",
        )

    def _switch(self, version: str) -> None:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".current.", dir=self.runtime_root)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(f"{version}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.current_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _write_marker(
        target: Path, info: ReleaseInfo, features: tuple[str, ...] = ()
    ) -> None:
        target.mkdir(parents=True, exist_ok=True)
        (target / ".evopi-runtime.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "version": info.version,
                    "features": list(features),
                    "sha256": info.sha256,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _validate_marker(
        target: Path, info: ReleaseInfo, features: tuple[str, ...]
    ) -> None:
        marker = _read_runtime_marker(target)
        if (
            marker.version != info.version
            or marker.sha256 != info.sha256
            or marker.features != features
        ):
            raise RuntimeError("target runtime verification marker does not match the Release")

    def _cleanup_versions(self, previous: str | None, current: str) -> tuple[str, ...]:
        keep = {current, *(item for item in (previous,) if item)}
        warnings: list[str] = []
        for path in self.versions_root.iterdir():
            if path.is_dir() and path.name not in keep and not path.name.startswith("."):
                try:
                    shutil.rmtree(path)
                except OSError as exc:
                    warnings.append(f"unable to remove old runtime {path.name}: {exc}")
        return tuple(warnings)

    @staticmethod
    def _install_runtime(
        info: ReleaseInfo,
        wheel: bytes,
        target: Path,
        features: tuple[str, ...] = (),
    ) -> None:
        venv.EnvBuilder(with_pip=True, clear=False).create(target)
        scripts = target / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        evopi = scripts / ("evopi.exe" if os.name == "nt" else "evopi")
        wheel_path = target.parent / f".{info.wheel_name}"
        try:
            wheel_path.write_bytes(wheel)
            subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    f"{wheel_path}[{','.join(features)}]" if features else str(wheel_path),
                ],
                check=True,
            )
            subprocess.run([str(python), "-c", "import evopi"], check=True)
            subprocess.run([str(evopi), "--version"], check=True)
            subprocess.run([str(evopi), "--help"], check=True)
            if "remote" in features:
                subprocess.run([str(evopi), "remote", "serve", "--help"], check=True)
        finally:
            wheel_path.unlink(missing_ok=True)


def _normalize_features(features: tuple[str, ...]) -> tuple[str, ...]:
    selected = tuple(sorted(set(features)))
    if any(item != "remote" for item in selected):
        raise RuntimeError("unsupported managed runtime feature")
    return selected


def _runtime_id(version: str, features: tuple[str, ...]) -> str:
    if not features:
        return version
    label = "-".join(features)
    digest = hashlib.sha256(",".join(features).encode("ascii")).hexdigest()[:8]
    return f"{version}--{label}-{digest}"


def _valid_runtime_id(value: str) -> bool:
    if not value:
        return False
    version = value.split("--", 1)[0]
    try:
        parse_stable_tag(f"v{version}")
    except Exception:
        return False
    return value in {version, _runtime_id(version, ("remote",))}


def _validate_release_input(info: ReleaseInfo, wheel: bytes) -> None:
    try:
        parse_stable_tag(f"v{info.version}")
    except Exception as exc:
        raise RuntimeError("release version is not stable SemVer") from exc
    if (
        not info.wheel_name.startswith(f"evopi-{info.version}-")
        or not info.wheel_name.endswith(".whl")
        or "/" in info.wheel_name
        or "\\" in info.wheel_name
    ):
        raise RuntimeError("release wheel filename is invalid")
    actual_digest = hashlib.sha256(wheel).hexdigest()
    if info.sha256 != actual_digest:
        raise RuntimeError("release wheel SHA-256 does not match")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _read_runtime_marker(target: Path) -> _RuntimeMarker:
    marker_path = target / ".evopi-runtime.json"
    if target.is_symlink() or marker_path.is_symlink() or not marker_path.is_file():
        raise RuntimeError("target runtime has no trusted verification marker")
    try:
        raw = json.loads(
            marker_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("target runtime verification marker is invalid") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("target runtime verification marker is invalid")
    schema_version = raw.get("schema_version")
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise RuntimeError("target runtime verification marker version is unsupported")
    expected_keys = (
        {"schema_version", "version", "sha256"}
        if schema_version == 1
        else {"schema_version", "version", "features", "sha256"}
    )
    version = raw.get("version")
    sha256 = raw.get("sha256")
    if (
        set(raw) != expected_keys
        or not isinstance(version, str)
        or not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        raise RuntimeError("target runtime verification marker is invalid")
    try:
        parse_stable_tag(f"v{version}")
    except Exception as exc:
        raise RuntimeError("target runtime verification marker is invalid") from exc
    features_raw = raw.get("features", [])
    if not isinstance(features_raw, list) or any(
        not isinstance(item, str) for item in features_raw
    ):
        raise RuntimeError("target runtime verification marker is invalid")
    features = _normalize_features(tuple(features_raw))
    if list(features) != features_raw or target.name != _runtime_id(version, features):
        raise RuntimeError("target runtime verification marker is invalid")
    return _RuntimeMarker(version=version, features=features, sha256=sha256)


__all__ = ["ManagedRuntime", "RuntimeInstaller"]
