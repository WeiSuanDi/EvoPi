"""Immutable review evidence and isolated Policy candidate orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from evopi.evolution.activation import ArtifactCandidate
from evopi.evolution.policy_candidates import (
    PolicyCandidate,
    PolicyCandidateInspection,
    PolicyCandidateSnapshotStore,
    inspect_policy_candidate,
    policy_candidate_digest,
)
from evopi.policy.decisions import PolicyDecision
from evopi.policy.types import PolicyContext
from evopi.validators import (
    SupervisorReport,
    ValidationResult,
    build_policy_review_report,
    supervisor_report_from_dict,
)

POLICY_EVIDENCE_SCHEMA_VERSION = 1
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
_ENV_ALLOWLIST = {
    "ALLUSERSPROFILE",
    "APPDATA",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
}


def resolve_evolution_home() -> Path:
    configured = os.environ.get("EVOPI_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".evopi").resolve()


class PolicyEvidenceError(RuntimeError):
    """Raised when immutable review evidence is invalid or inconsistent."""


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyReviewWorkerInfo:
    protocol_version: int = 1
    python_version: str = field(default_factory=lambda: sys.version.split()[0])
    isolated_process: bool = True
    timeout: float = 30.0


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyReviewEvidence:
    candidate: ArtifactCandidate
    supervisor_report: SupervisorReport
    worker: PolicyReviewWorkerInfo
    trace_digest: str | None = None
    review_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: int = POLICY_EVIDENCE_SCHEMA_VERSION
    evidence_digest: str = ""

    @property
    def status(self) -> str:
        return self.supervisor_report.status

    def to_dict(self) -> dict[str, Any]:
        payload = _evidence_payload(self)
        payload["evidence_digest"] = self.evidence_digest or _payload_digest(payload)
        return payload


class PolicyEvidenceStore:
    """Content-addressed snapshots plus immutable Supervisor evidence."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.snapshots = PolicyCandidateSnapshotStore(self.root)

    def snapshot_path(self, digest: str) -> Path:
        return self.snapshots.path_for(digest)

    def report_path(self, review_id: str) -> Path:
        _validate_identifier(review_id, "review_id")
        return self.root / "reports" / f"{review_id}.json"

    def save(self, evidence: PolicyReviewEvidence) -> PolicyReviewEvidence:
        payload = _evidence_payload(evidence)
        digest = _payload_digest(payload)
        stored = PolicyReviewEvidence(
            candidate=evidence.candidate,
            supervisor_report=evidence.supervisor_report,
            worker=evidence.worker,
            trace_digest=evidence.trace_digest,
            review_id=evidence.review_id,
            created_at=evidence.created_at,
            schema_version=evidence.schema_version,
            evidence_digest=digest,
        )
        payload["evidence_digest"] = digest
        path = self.report_path(stored.review_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            loaded = self.load(stored.review_id)
            if loaded != stored:
                raise PolicyEvidenceError(
                    f"review evidence already exists with different content: {stored.review_id}"
                )
            return loaded
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise PolicyEvidenceError(f"could not persist review evidence: {exc}") from exc
        return stored

    def load(self, review_id: str) -> PolicyReviewEvidence:
        path = self.report_path(review_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PolicyEvidenceError(f"invalid review evidence: {exc}") from exc
        if not isinstance(raw, dict):
            raise PolicyEvidenceError("review evidence must be an object")
        digest = raw.pop("evidence_digest", None)
        if not isinstance(digest, str) or digest != _payload_digest(raw):
            raise PolicyEvidenceError("review evidence digest does not match its content")
        try:
            evidence = _evidence_from_payload(raw, digest=digest)
        except (KeyError, TypeError, ValueError) as exc:
            raise PolicyEvidenceError(f"invalid review evidence: {exc}") from exc
        if evidence.review_id != review_id:
            raise PolicyEvidenceError("review evidence ID does not match its filename")
        return evidence


class PolicyReviewService:
    """Freeze, execute and persist one formal Policy candidate review."""

    def __init__(
        self,
        store: PolicyEvidenceStore,
        *,
        timeout: float = 30.0,
        environment: dict[str, str] | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("Policy review timeout must be greater than zero")
        self.store = store
        self.timeout = timeout
        self.environment = dict(environment or {})

    def review(
        self,
        candidate_path: str | Path,
        *,
        trace_path: str | Path | None = None,
    ) -> PolicyReviewEvidence:
        inspection = inspect_policy_candidate(candidate_path)
        snapshot = self.store.snapshots.freeze(inspection.candidate)
        trace = Path(trace_path).expanduser().resolve() if trace_path is not None else None
        trace_digest = _file_digest(trace) if trace is not None else None

        if inspection.errors:
            report = _failed_report(inspection, "; ".join(inspection.errors))
        else:
            report = self._run_worker(inspection.candidate, snapshot, trace)
        try:
            current_digest = policy_candidate_digest(snapshot)
        except Exception as exc:
            report = _failed_report(
                inspection,
                f"candidate snapshot could not be revalidated: {type(exc).__name__}: {exc}",
            )
        else:
            if current_digest != inspection.candidate.artifact.digest:
                report = _failed_report(
                    inspection,
                    "candidate snapshot changed while review was running",
                )

        evidence = PolicyReviewEvidence(
            candidate=inspection.candidate.artifact,
            supervisor_report=report,
            worker=PolicyReviewWorkerInfo(timeout=self.timeout),
            trace_digest=trace_digest,
        )
        return self.store.save(evidence)

    def _run_worker(
        self,
        candidate: PolicyCandidate,
        snapshot: Path,
        trace: Path | None,
    ) -> SupervisorReport:
        request = {
            "protocol_version": 1,
            "snapshot": str(snapshot),
            "manifest_digest": candidate.artifact.digest,
            "trace": str(trace) if trace is not None else None,
        }
        worker = Path(__file__).with_name("policy_review_worker.py")
        env = _sanitized_environment(self.environment)
        temp_root = self.store.root / "tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(
                prefix="worker-",
                dir=temp_root,
            ) as workdir:
                completed = subprocess.run(
                    [sys.executable, "-I", str(worker)],
                    input=json.dumps(request, separators=(",", ":")),
                    text=True,
                    capture_output=True,
                    cwd=workdir,
                    env=env,
                    timeout=self.timeout,
                    check=False,
                )
        except subprocess.TimeoutExpired:
            return _failed_report(
                PolicyCandidateInspection(candidate=candidate),
                f"Policy review worker timed out after {self.timeout:g} seconds",
            )
        except OSError as exc:
            return _failed_report(
                PolicyCandidateInspection(candidate=candidate),
                f"Policy review worker could not start: {type(exc).__name__}: {exc}",
            )
        if completed.returncode != 0:
            return _failed_report(
                PolicyCandidateInspection(candidate=candidate),
                f"Policy review worker failed with exit code {completed.returncode}",
            )
        try:
            payload = json.loads(completed.stdout)
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                error = payload.get("error") if isinstance(payload, dict) else None
                raise ValueError(str(error or "worker response was not successful"))
            return supervisor_report_from_dict(payload["report"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return _failed_report(
                PolicyCandidateInspection(candidate=candidate),
                f"Policy review worker returned an invalid protocol response: {exc}",
            )


class _FailedPolicy:
    def __init__(self, candidate: PolicyCandidate) -> None:
        manifest = candidate.manifest
        self.name = manifest.name
        self.version = manifest.version
        self.description = manifest.description
        self.hooks = manifest.hooks
        self.priority = manifest.priority
        self.enabled = True
        self.source = manifest.source
        self.risk_level = manifest.risk_level
        self.metadata = dict(manifest.metadata)

    def run(self, context: PolicyContext) -> PolicyDecision:
        return PolicyDecision(action="block", reason="candidate review failed")


def _failed_report(
    inspection: PolicyCandidateInspection,
    message: str,
) -> SupervisorReport:
    return build_policy_review_report(
        _FailedPolicy(inspection.candidate),
        schema_result=ValidationResult(passed=False, errors=[message]),
    )


def _sanitized_environment(overrides: dict[str, str]) -> dict[str, str]:
    combined = dict(os.environ)
    combined.update(overrides)
    return {
        key: value
        for key, value in combined.items()
        if key.upper() in _ENV_ALLOWLIST
        and not any(marker in key.upper() for marker in _SECRET_MARKERS)
    }


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise PolicyEvidenceError(f"could not read Trace evidence: {exc}") from exc


def _evidence_payload(evidence: PolicyReviewEvidence) -> dict[str, Any]:
    return {
        "schema_version": evidence.schema_version,
        "review_id": evidence.review_id,
        "created_at": evidence.created_at.isoformat(),
        "status": evidence.status,
        "candidate": asdict(evidence.candidate),
        "supervisor_report": evidence.supervisor_report.to_dict(),
        "worker": asdict(evidence.worker),
        "trace_digest": evidence.trace_digest,
    }


def _payload_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _evidence_from_payload(
    raw: dict[str, Any],
    *,
    digest: str,
) -> PolicyReviewEvidence:
    if raw.get("schema_version") != POLICY_EVIDENCE_SCHEMA_VERSION:
        raise ValueError("unsupported Policy review evidence schema")
    candidate_raw = raw["candidate"]
    worker_raw = raw["worker"]
    if not isinstance(candidate_raw, dict) or not isinstance(worker_raw, dict):
        raise TypeError("candidate and worker must be objects")
    created_at = datetime.fromisoformat(str(raw["created_at"]))
    if created_at.tzinfo is None:
        raise ValueError("created_at must include timezone")
    candidate = ArtifactCandidate(
        kind="policy",
        name=str(candidate_raw["name"]),
        version=str(candidate_raw["version"]),
        source=str(candidate_raw["source"]),
        risk_level=candidate_raw["risk_level"],
        digest=str(candidate_raw["digest"]),
        metadata=dict(candidate_raw.get("metadata", {})),
    )
    trace_digest = raw.get("trace_digest")
    if trace_digest is not None and not isinstance(trace_digest, str):
        raise TypeError("trace_digest must be a string or null")
    report = supervisor_report_from_dict(raw["supervisor_report"])
    if raw.get("status") != report.status:
        raise ValueError("evidence status does not match Supervisor Report")
    return PolicyReviewEvidence(
        schema_version=POLICY_EVIDENCE_SCHEMA_VERSION,
        review_id=str(raw["review_id"]),
        created_at=created_at.astimezone(UTC),
        candidate=candidate,
        supervisor_report=report,
        worker=PolicyReviewWorkerInfo(
            protocol_version=int(worker_raw["protocol_version"]),
            python_version=str(worker_raw["python_version"]),
            isolated_process=bool(worker_raw["isolated_process"]),
            timeout=float(worker_raw["timeout"]),
        ),
        trace_digest=trace_digest,
        evidence_digest=digest,
    )


def _validate_identifier(value: str, name: str) -> None:
    if not value or any(char not in "0123456789abcdef" for char in value.lower()):
        raise PolicyEvidenceError(f"{name} must be a hexadecimal identifier")


__all__ = [
    "POLICY_EVIDENCE_SCHEMA_VERSION",
    "PolicyEvidenceError",
    "PolicyEvidenceStore",
    "PolicyReviewEvidence",
    "PolicyReviewService",
    "PolicyReviewWorkerInfo",
    "resolve_evolution_home",
]
