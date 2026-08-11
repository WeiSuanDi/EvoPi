"""Versioned user-level EvoPi runtime installation and rollback."""

from __future__ import annotations

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
        self._installer = installer or self._install_runtime

    @property
    def is_managed_process(self) -> bool:
        marker = os.getenv("EVOPI_MANAGED_ROOT")
        return marker is not None and Path(marker).expanduser().resolve() == self.home

    @property
    def current_version(self) -> str | None:
        if not self.current_path.exists():
            return None
        value = self.current_path.read_text(encoding="utf-8").strip()
        try:
            version_key(value)
        except Exception:
            return None
        return value

    def install(self, info: ReleaseInfo, wheel: bytes) -> UpdateResult:
        current = self.current_version
        target = self.versions_root / info.version
        try:
            with _UpdateLock(self.lock_path):
                self.versions_root.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    marker = target / ".evopi-runtime.json"
                    if not marker.exists():
                        raise RuntimeError("target runtime exists without a verification marker")
                else:
                    self._installer(info, wheel, target)
                    self._write_marker(target, info)
                self._switch(info.version)
                warnings = self._cleanup_versions(current, info.version)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            if target != self.versions_root / (current or ""):
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
                candidates = sorted(
                    (
                        path.name
                        for path in self.versions_root.iterdir()
                        if path.is_dir()
                        and path.name != current
                        and (path / ".evopi-runtime.json").is_file()
                    ),
                    key=version_key,
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
            current_version=target,
            target_version=target,
            message=f"rolled back EvoPi to {target}",
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
    def _write_marker(target: Path, info: ReleaseInfo) -> None:
        target.mkdir(parents=True, exist_ok=True)
        (target / ".evopi-runtime.json").write_text(
            json.dumps(
                {"schema_version": 1, "version": info.version, "sha256": info.sha256},
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

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
    def _install_runtime(info: ReleaseInfo, wheel: bytes, target: Path) -> None:
        venv.EnvBuilder(with_pip=True, clear=False).create(target)
        scripts = target / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        evopi = scripts / ("evopi.exe" if os.name == "nt" else "evopi")
        wheel_path = target.parent / f".{info.wheel_name}"
        try:
            wheel_path.write_bytes(wheel)
            subprocess.run(
                [str(python), "-m", "pip", "install", "--disable-pip-version-check", str(wheel_path)],
                check=True,
            )
            subprocess.run([str(python), "-c", "import evopi"], check=True)
            subprocess.run([str(evopi), "--version"], check=True)
            subprocess.run([str(evopi), "--help"], check=True)
        finally:
            wheel_path.unlink(missing_ok=True)


__all__ = ["ManagedRuntime", "RuntimeInstaller"]
