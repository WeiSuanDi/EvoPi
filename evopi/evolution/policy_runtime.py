"""Runtime loading for explicitly activated, immutable Policy artifacts."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from evopi.evolution.activation import ArtifactActivationError
from evopi.evolution.policy_activation import (
    ActivePolicySelection,
    PolicyActivationService,
    PolicyReplacement,
)
from evopi.evolution.policy_candidates import (
    PolicyCandidate,
    inspect_policy_candidate,
    resolve_policy_entrypoint,
)
from evopi.policy.types import Policy


@dataclass(slots=True, frozen=True, kw_only=True)
class LoadedPolicyArtifact:
    policy: Policy
    digest: str
    approval_record_id: str
    selection_record_id: str
    artifact_path: Path
    replacement: PolicyReplacement | None = None


class PolicyArtifactLoader:
    """Load only selections already validated by the activation service."""

    def load_active(
        self,
        service: PolicyActivationService,
    ) -> tuple[LoadedPolicyArtifact, ...]:
        return tuple(self.load(selection) for selection in service.active())

    def load(self, active: ActivePolicySelection) -> LoadedPolicyArtifact:
        candidate = active.approval.candidate
        try:
            inspection = inspect_policy_candidate(active.artifact_path)
        except Exception as exc:
            raise ArtifactActivationError(
                f"approved Policy snapshot failed validation: {exc}"
            ) from exc
        if inspection.errors:
            raise ArtifactActivationError(
                "approved Policy snapshot contains invalid Python: "
                + "; ".join(inspection.errors)
            )
        if inspection.candidate.artifact.digest != candidate.digest:
            raise ArtifactActivationError(
                "approved Policy snapshot digest does not match its activation"
            )
        if (
            inspection.candidate.manifest.name != candidate.name
            or inspection.candidate.manifest.version != candidate.version
        ):
            raise ArtifactActivationError(
                "approved Policy snapshot identity does not match its activation"
            )
        policy = cast(
            Policy,
            _load_reference(
                inspection.candidate.path,
                inspection.candidate.manifest.entrypoint,
            ),
        )
        errors = _contract_errors(inspection.candidate, policy)
        if errors:
            raise ArtifactActivationError(
                "approved Policy runtime contract mismatch: " + "; ".join(errors)
            )
        policy.metadata.update(
            {
                "evolution_artifact_digest": candidate.digest,
                "evolution_activation_id": active.approval.record_id,
                "evolution_selection_id": active.selection.record_id,
            }
        )
        return LoadedPolicyArtifact(
            policy=policy,
            digest=candidate.digest,
            approval_record_id=active.approval.record_id,
            selection_record_id=active.selection.record_id,
            artifact_path=active.artifact_path,
            replacement=active.selection.replacement,
        )


def _load_reference(root: Path, spec: str) -> Any:
    path, attribute = resolve_policy_entrypoint(root, spec)
    module_name = f"_evopi_policy_artifact_{uuid4().hex}"
    module_spec = importlib.util.spec_from_file_location(module_name, path)
    if module_spec is None or module_spec.loader is None:
        raise ArtifactActivationError(f"could not load Policy artifact module: {path}")
    module = importlib.util.module_from_spec(module_spec)
    sys.path.insert(0, str(root))
    try:
        module_spec.loader.exec_module(module)
    except Exception as exc:
        raise ArtifactActivationError(
            f"approved Policy artifact raised during import: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        sys.path.remove(str(root))
    value: Any = module
    try:
        for part in attribute.split("."):
            value = getattr(value, part)
        if isinstance(value, type):
            value = value()
        elif not callable(getattr(value, "run", None)) and callable(value):
            value = value()
    except Exception as exc:
        raise ArtifactActivationError(
            f"approved Policy entrypoint could not be constructed: {exc}"
        ) from exc
    return value


def _contract_errors(candidate: PolicyCandidate, policy: Policy) -> list[str]:
    manifest = candidate.manifest
    expected = {
        "name": manifest.name,
        "version": manifest.version,
        "description": manifest.description,
        "hooks": manifest.hooks,
        "priority": manifest.priority,
        "source": manifest.source,
        "risk_level": manifest.risk_level,
        "metadata": manifest.metadata,
        "enabled": True,
    }
    errors = [
        f"Policy field '{name}' does not match its manifest"
        for name, value in expected.items()
        if getattr(policy, name, None) != value
    ]
    if not callable(getattr(policy, "run", None)):
        errors.append("Policy entrypoint does not provide run(context)")
    return errors


__all__ = ["LoadedPolicyArtifact", "PolicyArtifactLoader"]
