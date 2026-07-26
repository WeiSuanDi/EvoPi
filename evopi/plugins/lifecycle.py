"""Approved Plugin snapshot resolution for CLI and Harness assembly."""

from __future__ import annotations

import os
from pathlib import Path

from evopi.evolution import (
    ActivationDecision,
    ActivationRecord,
    ActivationStore,
    WorkspaceTrustStore,
)
from evopi.plugins.candidates import PluginArtifactStore, PluginCandidateError


def resolve_evopi_home() -> Path:
    configured = os.environ.get("EVOPI_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".evopi").resolve()


def approved_plugin_entrypoints(
    workspace: str | Path,
    *,
    home: str | Path | None = None,
) -> tuple[Path, ...]:
    """Return immutable approved snapshots allowed in the current workspace."""

    runtime_home = (
        Path(home).expanduser().resolve()
        if home is not None
        else resolve_evopi_home()
    )
    activation_path = runtime_home / "activations.json"
    if not activation_path.exists():
        return ()
    store = ActivationStore(activation_path)
    artifacts = PluginArtifactStore(runtime_home / "artifacts" / "plugins")
    trust_path = runtime_home / "workspace-trust.json"
    trust = WorkspaceTrustStore(trust_path) if trust_path.exists() else None
    workspace_path = Path(workspace).expanduser().resolve()
    latest: dict[str, ActivationRecord] = {}
    for record in store.records():
        if record.candidate.kind == "plugin":
            latest[record.candidate.name] = record
    entrypoints: list[Path] = []
    for record in latest.values():
        candidate = record.candidate
        if record.decision is not ActivationDecision.APPROVED:
            continue
        source = Path(candidate.source).expanduser().resolve()
        if _is_within(source, workspace_path) and not (
            trust is not None and trust.is_trusted(workspace_path)
        ):
            continue
        try:
            entrypoints.append(artifacts.entrypoint_for(candidate))
        except PluginCandidateError:
            continue
    return tuple(entrypoints)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


__all__ = ["approved_plugin_entrypoints", "resolve_evopi_home"]
