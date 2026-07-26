"""Non-executing discovery and review of Plugin candidates."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from evopi.evolution import ActivationStore, ArtifactCandidate

PLUGIN_MANIFEST_SCHEMA_VERSION = 1


class PluginCandidateError(RuntimeError):
    """Raised when a candidate cannot be inspected safely."""


class PluginCandidateStatus(str, Enum):
    DISCOVERED = "discovered"
    CANDIDATE = "candidate"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    ACTIVE = "active"
    DENIED = "denied"
    STALE = "stale"
    FAILED = "failed"


@dataclass(slots=True, frozen=True, kw_only=True)
class PluginManifest:
    name: str
    version: str
    entrypoint: str
    description: str = ""
    dependencies: tuple[str, ...] = ()
    requested_capabilities: tuple[str, ...] = ()
    schema_version: int = PLUGIN_MANIFEST_SCHEMA_VERSION


@dataclass(slots=True, frozen=True, kw_only=True)
class PluginCandidate:
    path: Path
    manifest: PluginManifest
    artifact: ArtifactCandidate

    @property
    def name(self) -> str:
        return self.manifest.name


@dataclass(slots=True, frozen=True, kw_only=True)
class PluginReviewReport:
    candidate: PluginCandidate
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.errors


@dataclass(slots=True, frozen=True, kw_only=True)
class PluginState:
    candidate: PluginCandidate
    status: PluginCandidateStatus
    errors: tuple[str, ...] = ()


def review_plugin(path: str | Path) -> PluginReviewReport:
    """Review syntax and manifest without importing or executing candidate code."""

    candidate_path = Path(path).expanduser().resolve()
    manifest = _read_manifest(candidate_path)
    entry = (candidate_path / manifest.entrypoint).resolve()
    try:
        entry.relative_to(candidate_path)
    except ValueError as exc:
        raise PluginCandidateError("plugin entrypoint escapes candidate directory") from exc
    if not entry.is_file():
        raise PluginCandidateError(f"plugin entrypoint does not exist: {entry}")
    errors: list[str] = []
    try:
        ast.parse(entry.read_text(encoding="utf-8"), filename=str(entry))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        errors.append(f"entrypoint is not valid UTF-8 Python: {exc}")
    digest = _candidate_digest(candidate_path)
    artifact = ArtifactCandidate(
        kind="plugin",
        name=manifest.name,
        version=manifest.version,
        source=str(candidate_path),
        risk_level="high",
        digest=digest,
        metadata={
            "entrypoint": manifest.entrypoint,
            "dependencies": list(manifest.dependencies),
            "requested_capabilities": list(manifest.requested_capabilities),
        },
    )
    return PluginReviewReport(
        candidate=PluginCandidate(
            path=candidate_path,
            manifest=manifest,
            artifact=artifact,
        ),
        errors=tuple(errors),
    )


class PluginManager:
    """Resolve candidate status without importing unapproved source."""

    def __init__(
        self,
        *,
        workspace: str | Path,
        activation_store: ActivationStore,
        candidate_paths: list[str | Path] | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.activation_store = activation_store
        self.candidate_paths = [Path(path) for path in (candidate_paths or [])]

    def states(self) -> tuple[PluginState, ...]:
        states: list[PluginState] = []
        for path in self.candidate_paths:
            try:
                report = review_plugin(path)
            except PluginCandidateError as exc:
                states.append(
                    PluginState(
                        candidate=_failed_candidate(path),
                        status=PluginCandidateStatus.FAILED,
                        errors=(str(exc),),
                    )
                )
                continue
            candidate = report.candidate
            exact = self.activation_store.check(candidate.artifact)
            if exact.record is not None:
                status = (
                    PluginCandidateStatus.APPROVED
                    if exact.approved
                    else PluginCandidateStatus.DENIED
                )
            elif any(
                record.candidate.kind == "plugin"
                and record.candidate.name == candidate.name
                and record.candidate.version == candidate.manifest.version
                for record in self.activation_store.records()
            ):
                status = PluginCandidateStatus.STALE
            else:
                status = (
                    PluginCandidateStatus.REVIEWED
                    if report.passed
                    else PluginCandidateStatus.FAILED
                )
            states.append(
                PluginState(candidate=candidate, status=status, errors=report.errors)
            )
        return tuple(states)


class PluginArtifactStore:
    """Content-addressed immutable snapshots of approved Plugin candidates."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def install(self, candidate: PluginCandidate) -> Path:
        current = review_plugin(candidate.path).candidate
        if current.artifact.digest != candidate.artifact.digest:
            raise PluginCandidateError(
                "Plugin source changed after review; review the candidate again"
            )
        target = self.path_for(candidate.artifact.digest)
        if target.exists():
            snapshot = review_plugin(target).candidate
            if snapshot.artifact.digest != candidate.artifact.digest:
                raise PluginCandidateError(
                    "approved Plugin snapshot digest does not match its path"
                )
            return target
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f".{candidate.artifact.digest}.{os.getpid()}.tmp"
        try:
            shutil.copytree(candidate.path, temporary)
            snapshot = review_plugin(temporary).candidate
            if snapshot.artifact.digest != candidate.artifact.digest:
                raise PluginCandidateError(
                    "copied Plugin snapshot failed digest verification"
                )
            os.replace(temporary, target)
        except (OSError, shutil.Error, PluginCandidateError):
            if temporary.exists():
                shutil.rmtree(temporary)
            raise
        return target

    def path_for(self, digest: str) -> Path:
        return self.root / digest

    def entrypoint_for(self, artifact: ArtifactCandidate) -> Path:
        if artifact.kind != "plugin":
            raise PluginCandidateError("artifact is not a Plugin")
        entrypoint = artifact.metadata.get("entrypoint")
        if not isinstance(entrypoint, str) or not entrypoint:
            raise PluginCandidateError("Plugin artifact has no entrypoint")
        snapshot = self.path_for(artifact.digest)
        entry = (snapshot / entrypoint).resolve()
        try:
            entry.relative_to(snapshot)
        except ValueError as exc:
            raise PluginCandidateError(
                "approved Plugin entrypoint escapes its snapshot"
            ) from exc
        if not entry.is_file():
            raise PluginCandidateError(
                f"approved Plugin snapshot is missing: {entry}"
            )
        return entry


def _read_manifest(path: Path) -> PluginManifest:
    manifest_path = path / "evopi-plugin.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginCandidateError(f"invalid Plugin manifest: {exc}") from exc
    if not isinstance(raw, dict):
        raise PluginCandidateError("Plugin manifest must be an object")
    if raw.get("schema_version") != PLUGIN_MANIFEST_SCHEMA_VERSION:
        raise PluginCandidateError("unsupported Plugin manifest schema")
    try:
        name = raw["name"]
        version = raw["version"]
        entrypoint = raw["entrypoint"]
    except KeyError as exc:
        raise PluginCandidateError(f"missing Plugin manifest field: {exc}") from exc
    if not all(isinstance(value, str) and value.strip() for value in (name, version, entrypoint)):
        raise PluginCandidateError("Plugin name, version and entrypoint must be strings")
    dependencies = _string_tuple(raw.get("dependencies", []), "dependencies")
    capabilities = _string_tuple(
        raw.get("requested_capabilities", []), "requested_capabilities"
    )
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise PluginCandidateError("Plugin description must be a string")
    return PluginManifest(
        name=name,
        version=version,
        entrypoint=entrypoint,
        description=description,
        dependencies=dependencies,
        requested_capabilities=capabilities,
    )


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PluginCandidateError(f"Plugin {name} must be an array of strings")
    return tuple(value)


def _candidate_digest(path: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        item
        for item in path.rglob("*")
        if item.is_file() and "__pycache__" not in item.parts
    )
    for item in files:
        relative = item.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        content = item.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _failed_candidate(path: Path) -> PluginCandidate:
    resolved = path.expanduser().resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
    manifest = PluginManifest(
        name=resolved.name or "unknown",
        version="unknown",
        entrypoint="",
    )
    return PluginCandidate(
        path=resolved,
        manifest=manifest,
        artifact=ArtifactCandidate(
            kind="plugin",
            name=manifest.name,
            version=manifest.version,
            source=str(resolved),
            risk_level="high",
            digest=digest,
        ),
    )


__all__ = [
    "PLUGIN_MANIFEST_SCHEMA_VERSION",
    "PluginCandidate",
    "PluginCandidateError",
    "PluginCandidateStatus",
    "PluginArtifactStore",
    "PluginManager",
    "PluginManifest",
    "PluginReviewReport",
    "PluginState",
    "review_plugin",
]
