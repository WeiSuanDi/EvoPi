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
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

from evopi.distribution.models import ReleaseInfo, UpdateResult, UpdateStatus
from evopi.distribution.release import version_key

RuntimeInstaller = Callable[[ReleaseInfo, bytes, Path], None]


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
        if not self.current_path.exists():
            return None
        value = self.current_path.read_text(encoding="utf-8").strip()
        return value or None

    @property
    def current_features(self) -> tuple[str, ...]:
        runtime_id = self.current_runtime_id
        if runtime_id is None:
            return ()
        marker = self.versions_root / runtime_id / ".evopi-runtime.json"
        try:
            raw = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ("remote",) if "--remote-" in runtime_id else ()
        features = raw.get("features", [])
        if not isinstance(features, list) or any(not isinstance(item, str) for item in features):
            return ()
        return tuple(sorted(set(features)))

    def install(
        self,
        info: ReleaseInfo,
        wheel: bytes,
        *,
        features: tuple[str, ...] | None = None,
    ) -> UpdateResult:
        current = self.current_version
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
                candidates = [
                    path.name
                    for path in self.versions_root.iterdir()
                    if path.is_dir()
                    and path.name != current_id
                    and (path / ".evopi-runtime.json").is_file()
                ]
                candidates.sort(
                    key=lambda item: version_key(item.split("--", 1)[0]),
                    reverse=True,
                )
                if not candidates:
                    raise RuntimeError("no previous verified EvoPi runtime is available")
                target = candidates[0]
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
        marker = target / ".evopi-runtime.json"
        if target.is_symlink() or marker.is_symlink() or not marker.is_file():
            raise RuntimeError("target runtime has no trusted verification marker")
        try:
            raw = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("target runtime verification marker is invalid") from exc
        expected_features = list(features)
        if raw.get("schema_version") == 1:
            expected_keys = {"schema_version", "version", "sha256"}
            actual_features: object = []
        elif raw.get("schema_version") == 2:
            expected_keys = {"schema_version", "version", "features", "sha256"}
            actual_features = raw.get("features")
        else:
            raise RuntimeError("target runtime verification marker version is unsupported")
        if (
            set(raw) != expected_keys
            or raw.get("version") != info.version
            or raw.get("sha256") != info.sha256
            or actual_features != expected_features
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


__all__ = ["ManagedRuntime", "RuntimeInstaller"]
