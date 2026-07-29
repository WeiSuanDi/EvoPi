"""Offline, secret-free EvoPi configuration and diagnostics."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from evopi.ai import ModelEnvironmentConfig, resolve_model_environment
from evopi.cli.runtime import parse_fallback_specs
from evopi.evolution import (
    ActivationDecision,
    ActivationStore,
    PolicyArtifactStore,
    PolicySelectionStore,
    WorkspaceTrustStore,
    resolve_evolution_home,
)
from evopi.plugins import (
    PluginArtifactStore,
    PluginCandidateError,
    resolve_evopi_home,
    review_plugin,
)
from evopi.session import resolve_session_root


@dataclass(slots=True, frozen=True, kw_only=True)
class ConfigSnapshot:
    workspace: str
    provider: str
    model: str
    base_url: str
    credential_configured: bool
    fallbacks: tuple[ModelEnvironmentConfig, ...]
    evopi_home: str
    session_root: str
    workspace_trusted: bool
    memory_enabled: bool
    memory_path: str
    skill_sources: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workspace": self.workspace,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "credential_configured": self.credential_configured,
            "fallbacks": [
                {
                    "provider": item.provider,
                    "model": item.model,
                    "base_url": item.base_url,
                    "credential_configured": item.credential_configured,
                }
                for item in self.fallbacks
            ],
            "evopi_home": self.evopi_home,
            "session_root": self.session_root,
            "workspace_trusted": self.workspace_trusted,
            "memory": {
                "enabled": self.memory_enabled,
                "path": self.memory_path,
            },
            "skill_sources": list(self.skill_sources),
            "warnings": list(self.warnings),
        }


class DoctorCheckStatus(str, Enum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"


class DoctorStatus(str, Enum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"


@dataclass(slots=True, frozen=True, kw_only=True)
class DoctorCheck:
    name: str
    status: DoctorCheckStatus
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
        }


@dataclass(slots=True, frozen=True, kw_only=True)
class DoctorReport:
    status: DoctorStatus
    checks: tuple[DoctorCheck, ...]
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "checks": [check.to_dict() for check in self.checks],
        }


def build_config_snapshot(
    *,
    workspace: str | Path,
    provider: str | None = None,
    model: str | None = None,
) -> ConfigSnapshot:
    """Resolve the effective product configuration without creating a model."""

    resolved_workspace = Path(workspace).expanduser().resolve()
    primary = resolve_model_environment(provider, model=model)
    raw_fallbacks = tuple(
        item.strip()
        for item in os.getenv("EVOPI_FALLBACKS", "").split(",")
        if item.strip()
    )
    fallback_configs = tuple(
        resolve_model_environment(fallback_provider, model=fallback_model)
        for fallback_provider, fallback_model in parse_fallback_specs(raw_fallbacks)
    )
    home = resolve_evopi_home()
    evolution_home = resolve_evolution_home()
    warnings: list[str] = []
    if home != evolution_home:
        warnings.append(
            f"Plugin home {home} differs from Evolution home {evolution_home}"
        )
    trust_path = home / "workspace-trust.json"
    trusted = False
    if trust_path.exists():
        try:
            trusted = WorkspaceTrustStore(trust_path).is_trusted(
                resolved_workspace
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            warnings.append(f"Workspace Trust store is invalid: {exc}")
    project_skills = resolved_workspace / ".evopi" / "skills"
    global_skills = home / "skills"
    skill_sources: list[str] = []
    if project_skills.exists() and trusted:
        skill_sources.append(str(project_skills))
    elif project_skills.exists():
        warnings.append("Project Skills are present but the workspace is not trusted")
    if global_skills.exists():
        skill_sources.append(str(global_skills))
    memory_path = resolved_workspace / ".evopi" / "memory.json"
    return ConfigSnapshot(
        workspace=str(resolved_workspace),
        provider=primary.provider,
        model=primary.model,
        base_url=primary.base_url,
        credential_configured=primary.credential_configured,
        fallbacks=fallback_configs,
        evopi_home=str(home),
        session_root=str(resolve_session_root()),
        workspace_trusted=trusted,
        memory_enabled=True,
        memory_path=str(memory_path),
        skill_sources=tuple(skill_sources),
        warnings=tuple(warnings),
    )


def run_doctor(
    *,
    workspace: str | Path,
    provider: str | None = None,
    model: str | None = None,
) -> DoctorReport:
    """Run deterministic offline checks without loading executable Plugins."""

    checks: list[DoctorCheck] = []
    checks.append(
        _check(
            "python",
            sys.version_info >= (3, 11),
            f"Python {sys.version_info.major}.{sys.version_info.minor}",
        )
    )
    workspace_path = Path(workspace).expanduser().resolve()
    checks.append(
        _check(
            "workspace",
            workspace_path.is_dir(),
            (
                f"Workspace is available: {workspace_path}"
                if workspace_path.is_dir()
                else f"Workspace is not a directory: {workspace_path}"
            ),
        )
    )
    snapshot: ConfigSnapshot | None = None
    try:
        snapshot = build_config_snapshot(
            workspace=workspace_path,
            provider=provider,
            model=model,
        )
    except (OSError, ValueError) as exc:
        checks.append(
            DoctorCheck(
                name="model_configuration",
                status=DoctorCheckStatus.FAILED,
                message=f"{type(exc).__name__}: {exc}",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="model_configuration",
                status=DoctorCheckStatus.PASSED,
                message=f"{snapshot.provider}:{snapshot.model}",
            )
        )
        checks.append(
            DoctorCheck(
                name="credential",
                status=(
                    DoctorCheckStatus.PASSED
                    if snapshot.credential_configured
                    else DoctorCheckStatus.WARNING
                ),
                message=(
                    "Provider credential is configured"
                    if snapshot.credential_configured
                    else "Provider credential is not configured"
                ),
            )
        )
        parsed = urlsplit(snapshot.base_url)
        valid_url = parsed.scheme in {"http", "https"} and bool(parsed.netloc)
        checks.append(
            _check(
                "base_url",
                valid_url,
                (
                    f"Base URL is valid: {snapshot.base_url}"
                    if valid_url
                    else f"Base URL is invalid: {snapshot.base_url}"
                ),
            )
        )
        checks.append(_writable_check("evopi_home", Path(snapshot.evopi_home)))
        checks.append(
            _writable_check("session_root", Path(snapshot.session_root))
        )
        checks.extend(_artifact_checks(Path(snapshot.evopi_home)))
        trust_status = (
            "trusted" if snapshot.workspace_trusted else "not trusted"
        )
        checks.append(
            DoctorCheck(
                name="workspace_trust",
                status=DoctorCheckStatus.PASSED,
                message=f"Workspace is {trust_status}; project resources follow this gate",
            )
        )
        checks.extend(
            DoctorCheck(
                name=f"configuration_warning_{index}",
                status=DoctorCheckStatus.WARNING,
                message=warning,
            )
            for index, warning in enumerate(snapshot.warnings, start=1)
        )
    status = _doctor_status(checks)
    return DoctorReport(status=status, checks=tuple(checks))


def _artifact_checks(home: Path) -> tuple[DoctorCheck, DoctorCheck]:
    activation_path = home / "activations.json"
    selection_path = home / "policy-selections.json"
    activations: ActivationStore | None = None
    try:
        activations = (
            ActivationStore(activation_path)
            if activation_path.exists()
            else None
        )
        policy_count = 0
        if selection_path.exists():
            selections = PolicySelectionStore(selection_path)
            if activations is None:
                raise ValueError("Policy selections exist without Activation records")
            by_id = {record.record_id: record for record in activations.records()}
            policy_artifacts = PolicyArtifactStore(home / "artifacts" / "policies")
            for selected in selections.active_records():
                approval = by_id.get(selected.approval_record_id or "")
                if (
                    approval is None
                    or approval.decision is not ActivationDecision.APPROVED
                ):
                    raise ValueError(
                        f"Active Policy '{selected.policy_name}' has no valid approval"
                    )
                policy_artifacts.validate(approval.candidate)
                policy_count += 1
        policy_check = DoctorCheck(
            name="policy_artifacts",
            status=DoctorCheckStatus.PASSED,
            message=f"Validated {policy_count} active Policy artifact(s)",
        )
    except Exception as exc:
        policy_check = DoctorCheck(
            name="policy_artifacts",
            status=DoctorCheckStatus.FAILED,
            message=f"{type(exc).__name__}: {exc}",
        )

    try:
        plugin_count = 0
        if activations is None and activation_path.exists():
            activations = ActivationStore(activation_path)
        if activations is not None:
            latest: dict[str, Any] = {}
            for record in activations.records():
                if record.candidate.kind == "plugin":
                    latest[record.candidate.name] = record
            plugin_artifacts = PluginArtifactStore(home / "artifacts" / "plugins")
            for record in latest.values():
                if record.decision is not ActivationDecision.APPROVED:
                    continue
                snapshot = plugin_artifacts.path_for(record.candidate.digest)
                reviewed = review_plugin(snapshot).candidate
                if reviewed.artifact.digest != record.candidate.digest:
                    raise ValueError(
                        f"Plugin '{record.candidate.name}' digest does not match"
                    )
                plugin_count += 1
        plugin_check = DoctorCheck(
            name="plugin_artifacts",
            status=DoctorCheckStatus.PASSED,
            message=f"Validated {plugin_count} approved Plugin artifact(s)",
        )
    except (OSError, ValueError, PluginCandidateError) as exc:
        plugin_check = DoctorCheck(
            name="plugin_artifacts",
            status=DoctorCheckStatus.FAILED,
            message=f"{type(exc).__name__}: {exc}",
        )
    return policy_check, plugin_check


def _writable_check(name: str, path: Path) -> DoctorCheck:
    try:
        path.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".evopi-doctor-",
            dir=path,
        )
        os.close(descriptor)
        Path(temporary).unlink()
    except OSError as exc:
        return DoctorCheck(
            name=name,
            status=DoctorCheckStatus.FAILED,
            message=f"Path is not writable: {path}: {exc}",
        )
    return DoctorCheck(
        name=name,
        status=DoctorCheckStatus.PASSED,
        message=f"Path is writable: {path}",
    )


def _check(name: str, passed: bool, message: str) -> DoctorCheck:
    return DoctorCheck(
        name=name,
        status=(
            DoctorCheckStatus.PASSED
            if passed
            else DoctorCheckStatus.FAILED
        ),
        message=message,
    )


def _doctor_status(checks: list[DoctorCheck]) -> DoctorStatus:
    if any(check.status is DoctorCheckStatus.FAILED for check in checks):
        return DoctorStatus.FAILED
    if any(check.status is DoctorCheckStatus.WARNING for check in checks):
        return DoctorStatus.WARNING
    return DoctorStatus.PASSED


def config_show_main(argv: list[str]) -> int:
    parser = _diagnostic_parser("evopi config show", "Show effective EvoPi configuration")
    args = parser.parse_args(argv)
    try:
        snapshot = build_config_snapshot(
            workspace=args.workspace,
            provider=args.provider,
            model=args.model,
        )
    except (OSError, ValueError) as exc:
        print(f"EvoPi config error: {exc}", file=sys.stderr)
        return 1
    payload = snapshot.to_dict()
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")
    return 0


def doctor_main(argv: list[str]) -> int:
    parser = _diagnostic_parser("evopi doctor", "Run offline EvoPi diagnostics")
    args = parser.parse_args(argv)
    report = run_doctor(
        workspace=args.workspace,
        provider=args.provider,
        model=args.model,
    )
    if args.json_output:
        print(
            json.dumps(
                report.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    else:
        print(f"EvoPi doctor: {report.status.value}")
        for check in report.checks:
            print(f"  [{check.status.value}] {check.name}: {check.message}")
    return {
        DoctorStatus.PASSED: 0,
        DoctorStatus.WARNING: 2,
        DoctorStatus.FAILED: 1,
    }[report.status]


def _diagnostic_parser(prog: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--provider",
        choices=[
            "anthropic",
            "openai",
            "openai-compatible",
            "openai-responses",
        ],
    )
    parser.add_argument("--model")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


__all__ = [
    "ConfigSnapshot",
    "DoctorCheck",
    "DoctorCheckStatus",
    "DoctorReport",
    "DoctorStatus",
    "build_config_snapshot",
    "config_show_main",
    "doctor_main",
    "run_doctor",
]
