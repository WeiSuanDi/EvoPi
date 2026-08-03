"""Public protocol and strict codec for Policy candidate generation.

Generation produces an immutable Generation Record that binds an
Opportunity report to an evidence-selected, model-proposed, user-confirmed
Policy candidate.  Records never contain raw Trace values, full prompts,
or full model responses.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast

POLICY_GENERATION_SCHEMA_VERSION = 1

PolicyGenerationStrategy: TypeAlias = Literal["additive", "replacement", "defer"]
PolicyGenerationOutcome: TypeAlias = Literal[
    "generated",
    "declined",
    "deferred",
    "failed",
]
PolicyGenerationConfirmation: TypeAlias = Literal[
    "interactive",
    "preauthorized",
    "declined",
    "none",
]
PolicyGenerationAction: TypeAlias = Literal["allow", "block", "require_confirmation"]

_STRATEGIES = {"additive", "replacement", "defer"}
_OUTCOMES = {"generated", "declined", "deferred", "failed"}
_CONFIRMATIONS = {"interactive", "preauthorized", "declined", "none"}
_ACTIONS = {"allow", "block", "require_confirmation"}
_RISK_LEVELS = {"low", "medium", "high", "critical"}


class PolicyGenerationError(ValueError):
    """Raised when a generation protocol violation is detected."""

    def __init__(
        self,
        reason: str,
        *,
        code: str = "generation_protocol_error",
        path: str | None = None,
        line_number: int | None = None,
    ) -> None:
        self.code = code
        self.path = path
        self.line_number = line_number
        location = ""
        if path is not None:
            location = path
            if line_number is not None:
                location += f":{line_number}"
            location += ": "
        super().__init__(location + reason)


# ---------------------------------------------------------------------------
# Settings and evidence samples
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyGenerationSettings:
    max_evidence: int = 12
    stage_timeout: float = 120.0
    max_schema_repairs: int = 1
    max_evidence_bytes: int = 131072
    max_files: int = 16
    max_file_bytes: int = 262144
    max_total_file_bytes: int = 1048576

    def __post_init__(self) -> None:
        if self.max_evidence < 1:
            raise ValueError("max_evidence must be at least 1")
        if self.stage_timeout <= 0:
            raise ValueError("stage_timeout must be positive")
        if self.max_schema_repairs < 0:
            raise ValueError("max_schema_repairs must be non-negative")
        if self.max_evidence_bytes < 1:
            raise ValueError("max_evidence_bytes must be at least 1")
        if self.max_files < 1:
            raise ValueError("max_files must be at least 1")
        if self.max_file_bytes < 1:
            raise ValueError("max_file_bytes must be at least 1")
        if self.max_total_file_bytes < 1:
            raise ValueError("max_total_file_bytes must be at least 1")


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyGenerationEvidenceSample:
    sample_id: str
    trace_digest: str
    line_number: int
    run_id: str
    human_decision: Literal["approve", "deny"]
    tool_name: str
    arguments: dict[str, Any]


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyGenerationSampleDecision:
    sample_id: str
    action: PolicyGenerationAction


# ---------------------------------------------------------------------------
# Proposal and model-run metadata
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyGenerationProposal:
    schema_version: int = POLICY_GENERATION_SCHEMA_VERSION
    strategy: PolicyGenerationStrategy = "additive"
    candidate_name: str = ""
    description: str = ""
    match_summary: str = ""
    rationale: str = ""
    fallback_action: PolicyGenerationAction = "allow"
    replacement_target: str | None = None
    sample_decisions: tuple[PolicyGenerationSampleDecision, ...] = ()
    warnings: tuple[str, ...] = ()
    proposal_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "strategy": self.strategy,
            "candidate_name": self.candidate_name,
            "description": self.description,
            "match_summary": self.match_summary,
            "rationale": self.rationale,
            "fallback_action": self.fallback_action,
            "replacement_target": self.replacement_target,
            "sample_decisions": [
                {"sample_id": d.sample_id, "action": d.action}
                for d in self.sample_decisions
            ],
            "warnings": list(self.warnings),
        }
        payload["proposal_digest"] = self.proposal_digest or _payload_digest(payload)
        return payload


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyGenerationModelRun:
    """Safe metadata about one model stage — never raw prompts or responses."""

    stage: str = ""
    model: str = ""
    provider: str = ""
    attempt: int = 1
    schema_repair_count: int = 0
    timed_out: bool = False
    aborted: bool = False
    failed: bool = False
    error_code: str = ""
    error_message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Immutable record and result
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyGenerationRecord:
    generation_id: str
    created_at: datetime
    outcome: PolicyGenerationOutcome
    report_id: str
    report_digest: str
    semantic_signature: str
    evidence_digest: str
    proposal: PolicyGenerationProposal | None
    confirmation: PolicyGenerationConfirmation
    model_runs: tuple[PolicyGenerationModelRun, ...] = ()
    candidate_name: str | None = None
    candidate_digest: str | None = None
    error_code: str = ""
    error_message: str = ""
    schema_version: int = POLICY_GENERATION_SCHEMA_VERSION
    record_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = _record_payload(self)
        payload["record_digest"] = self.record_digest or _payload_digest(payload)
        return payload


@dataclass(slots=True, kw_only=True)
class PolicyGenerationResult:
    record: PolicyGenerationRecord
    proposal: PolicyGenerationProposal | None = None
    candidate: Path | None = None


# ---------------------------------------------------------------------------
# Codecs
# ---------------------------------------------------------------------------


_PROPOSAL_ALLOWED_KEYS = frozenset(
    {
        "schema_version",
        "strategy",
        "candidate_name",
        "description",
        "match_summary",
        "rationale",
        "fallback_action",
        "replacement_target",
        "sample_decisions",
        "warnings",
        "proposal_digest",
    }
)


def policy_generation_proposal_from_dict(value: object) -> PolicyGenerationProposal:
    if not isinstance(value, dict):
        raise PolicyGenerationError("Proposal must be an object", code="invalid_proposal")
    raw = dict(value)
    unknown = set(raw) - _PROPOSAL_ALLOWED_KEYS
    if unknown:
        raise PolicyGenerationError(
            "Proposal contains unknown fields: " + ", ".join(sorted(unknown)),
            code="invalid_proposal",
        )
    digest = raw.pop("proposal_digest", None)
    if not isinstance(digest, str) or digest != _payload_digest(raw):
        raise PolicyGenerationError(
            "Proposal digest does not match its content",
            code="proposal_digest_mismatch",
        )
    if raw.get("schema_version") != POLICY_GENERATION_SCHEMA_VERSION:
        raise PolicyGenerationError(
            f"unsupported Proposal schema version: {raw.get('schema_version')!r}",
            code="invalid_proposal",
        )
    strategy = raw.get("strategy")
    if strategy not in _STRATEGIES:
        raise PolicyGenerationError("invalid proposal strategy", code="invalid_proposal")
    fallback = raw.get("fallback_action")
    if fallback not in _ACTIONS:
        raise PolicyGenerationError("invalid fallback action", code="invalid_proposal")
    sample_decisions = tuple(
        _stored_sample_decision(item)
        for item in _stored_list(raw.get("sample_decisions"), "sample_decisions")
    )
    return PolicyGenerationProposal(
        schema_version=POLICY_GENERATION_SCHEMA_VERSION,
        strategy=cast(PolicyGenerationStrategy, strategy),
        candidate_name=_stored_string(raw.get("candidate_name"), "candidate_name"),
        description=_stored_string(raw.get("description"), "description"),
        match_summary=_stored_string(raw.get("match_summary"), "match_summary"),
        rationale=_stored_string(raw.get("rationale"), "rationale"),
        fallback_action=cast(PolicyGenerationAction, fallback),
        replacement_target=(
            _stored_string(raw.get("replacement_target"), "replacement_target")
            if raw.get("replacement_target") is not None
            else None
        ),
        sample_decisions=sample_decisions,
        warnings=tuple(
            _stored_string(item, "warnings item")
            for item in _stored_list(raw.get("warnings"), "warnings")
        ),
        proposal_digest=digest,
    )


_RECORD_ALLOWED_KEYS = frozenset(
    {
        "schema_version",
        "generation_id",
        "created_at",
        "outcome",
        "report_id",
        "report_digest",
        "semantic_signature",
        "evidence_digest",
        "proposal",
        "confirmation",
        "model_runs",
        "candidate_name",
        "candidate_digest",
        "error_code",
        "error_message",
        "record_digest",
    }
)


_MODEL_RUN_ALLOWED_KEYS = frozenset(
    {
        "stage",
        "model",
        "provider",
        "attempt",
        "schema_repair_count",
        "timed_out",
        "aborted",
        "failed",
        "error_code",
        "error_message",
        "metadata",
    }
)


_SAMPLE_DECISION_ALLOWED_KEYS = frozenset({"sample_id", "action"})


def policy_generation_record_from_dict(value: object) -> PolicyGenerationRecord:
    if not isinstance(value, dict):
        raise PolicyGenerationError("Record must be an object", code="invalid_record")
    raw = dict(value)
    unknown = set(raw) - _RECORD_ALLOWED_KEYS
    if unknown:
        raise PolicyGenerationError(
            "Record contains unknown fields: " + ", ".join(sorted(unknown)),
            code="invalid_record",
        )
    digest = raw.pop("record_digest", None)
    if not isinstance(digest, str) or digest != _payload_digest(raw):
        raise PolicyGenerationError(
            "Record digest does not match its content",
            code="record_digest_mismatch",
        )
    if raw.get("schema_version") != POLICY_GENERATION_SCHEMA_VERSION:
        raise PolicyGenerationError(
            "unsupported generation record schema",
            code="invalid_record",
        )
    outcome = raw.get("outcome")
    if outcome not in _OUTCOMES:
        raise PolicyGenerationError("invalid record outcome", code="invalid_record")
    confirmation = raw.get("confirmation")
    if confirmation not in _CONFIRMATIONS:
        raise PolicyGenerationError(
            "invalid record confirmation mode",
            code="invalid_record",
        )
    try:
        proposal = (
            policy_generation_proposal_from_dict(raw["proposal"])
            if raw.get("proposal") is not None
            else None
        )
        record = PolicyGenerationRecord(
            generation_id=_stored_identifier(
                raw["generation_id"], "generation_id", length=32
            ),
            created_at=_stored_datetime(raw["created_at"], "created_at"),
            outcome=cast(PolicyGenerationOutcome, outcome),
            report_id=_stored_identifier(raw["report_id"], "report_id", length=32),
            report_digest=_stored_identifier(
                raw["report_digest"], "report_digest", length=64
            ),
            semantic_signature=_stored_identifier(
                raw["semantic_signature"], "semantic_signature", length=64
            ),
            evidence_digest=_stored_identifier(
                raw["evidence_digest"], "evidence_digest", length=64
            ),
            proposal=proposal,
            confirmation=cast(PolicyGenerationConfirmation, confirmation),
            model_runs=tuple(
                _stored_model_run(item)
                for item in _stored_list(raw.get("model_runs"), "model_runs")
            ),
            candidate_name=(
                _stored_string(raw["candidate_name"], "candidate_name")
                if raw.get("candidate_name") is not None
                else None
            ),
            candidate_digest=(
                _stored_identifier(raw["candidate_digest"], "candidate_digest", length=64)
                if raw.get("candidate_digest") is not None
                else None
            ),
            error_code=_stored_string(raw.get("error_code", ""), "error_code"),
            error_message=_stored_string(raw.get("error_message", ""), "error_message"),
            schema_version=POLICY_GENERATION_SCHEMA_VERSION,
            record_digest=digest,
        )
    except PolicyGenerationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyGenerationError(
            f"invalid generation record: {exc}",
            code="invalid_record",
        ) from exc
    _validate_outcome_invariants(record)
    return record


def _validate_outcome_invariants(record: PolicyGenerationRecord) -> None:
    """Enforce outcome-field combinations in the record codec."""
    if record.outcome == "generated":
        if record.candidate_name is None or record.candidate_digest is None:
            raise PolicyGenerationError(
                "generated record requires candidate identity and digest",
                code="invalid_record",
            )
        if record.proposal is None:
            raise PolicyGenerationError(
                "generated record requires a valid Proposal",
                code="invalid_record",
            )
        if record.proposal.strategy == "defer":
            raise PolicyGenerationError(
                "generated record requires a non-defer Proposal",
                code="invalid_record",
            )
        if record.confirmation not in {"interactive", "preauthorized"}:
            raise PolicyGenerationError(
                "generated record requires interactive or preauthorized confirmation",
                code="invalid_record",
            )
    if record.outcome == "declined":
        if record.candidate_name is not None or record.candidate_digest is not None:
            raise PolicyGenerationError(
                "declined record must not carry candidate identity",
                code="invalid_record",
            )
        if record.proposal is None or record.proposal.strategy == "defer":
            raise PolicyGenerationError(
                "declined record requires a non-defer Proposal",
                code="invalid_record",
            )
        if record.confirmation != "declined":
            raise PolicyGenerationError(
                "declined record requires confirmation=declined",
                code="invalid_record",
            )
    if record.outcome == "deferred":
        if record.candidate_name is not None or record.candidate_digest is not None:
            raise PolicyGenerationError(
                "deferred record must not carry candidate identity",
                code="invalid_record",
            )
        if record.proposal is None or record.proposal.strategy != "defer":
            raise PolicyGenerationError(
                "deferred record requires a defer Proposal",
                code="invalid_record",
            )
        if record.confirmation != "none":
            raise PolicyGenerationError(
                "deferred record requires confirmation=none",
                code="invalid_record",
            )
    if record.outcome == "failed":
        if record.candidate_name is not None or record.candidate_digest is not None:
            raise PolicyGenerationError(
                "failed record must not carry candidate identity",
                code="invalid_record",
            )


def _record_payload(record: PolicyGenerationRecord) -> dict[str, Any]:
    return {
        "schema_version": record.schema_version,
        "generation_id": record.generation_id,
        "created_at": record.created_at.isoformat(),
        "outcome": record.outcome,
        "report_id": record.report_id,
        "report_digest": record.report_digest,
        "semantic_signature": record.semantic_signature,
        "evidence_digest": record.evidence_digest,
        "proposal": record.proposal.to_dict() if record.proposal is not None else None,
        "confirmation": record.confirmation,
        "model_runs": [
            {
                "stage": run.stage,
                "model": run.model,
                "provider": run.provider,
                "attempt": run.attempt,
                "schema_repair_count": run.schema_repair_count,
                "timed_out": run.timed_out,
                "aborted": run.aborted,
                "failed": run.failed,
                "error_code": run.error_code,
                "error_message": run.error_message,
                "metadata": dict(run.metadata),
            }
            for run in record.model_runs
        ],
        "candidate_name": record.candidate_name,
        "candidate_digest": record.candidate_digest,
        "error_code": record.error_code,
        "error_message": record.error_message,
    }


def _stored_model_run(value: object) -> PolicyGenerationModelRun:
    raw = _stored_object(value, "model_run")
    unknown = set(raw) - _MODEL_RUN_ALLOWED_KEYS
    if unknown:
        raise PolicyGenerationError(
            "model_run contains unknown fields: " + ", ".join(sorted(unknown)),
            code="invalid_record",
        )
    return PolicyGenerationModelRun(
        stage=_stored_string(raw.get("stage", ""), "model_run.stage"),
        model=_stored_string(raw.get("model", ""), "model_run.model"),
        provider=_stored_string(raw.get("provider", ""), "model_run.provider"),
        attempt=_stored_non_negative_int(raw.get("attempt", 1), "model_run.attempt"),
        schema_repair_count=_stored_non_negative_int(
            raw.get("schema_repair_count", 0), "model_run.schema_repair_count"
        ),
        timed_out=_stored_bool(raw.get("timed_out", False), "model_run.timed_out"),
        aborted=_stored_bool(raw.get("aborted", False), "model_run.aborted"),
        failed=_stored_bool(raw.get("failed", False), "model_run.failed"),
        error_code=_stored_string(raw.get("error_code", ""), "model_run.error_code"),
        error_message=_stored_string(
            raw.get("error_message", ""), "model_run.error_message"
        ),
        metadata=dict(raw.get("metadata", {}) or {}),
    )


def _stored_sample_decision(value: object) -> PolicyGenerationSampleDecision:
    raw = _stored_object(value, "sample_decision")
    unknown = set(raw) - _SAMPLE_DECISION_ALLOWED_KEYS
    if unknown:
        raise PolicyGenerationError(
            "sample_decision contains unknown fields: " + ", ".join(sorted(unknown)),
            code="invalid_proposal",
        )
    action = raw.get("action")
    if action not in _ACTIONS:
        raise PolicyGenerationError(
            "invalid sample decision action",
            code="invalid_proposal",
        )
    return PolicyGenerationSampleDecision(
        sample_id=_stored_string(raw.get("sample_id"), "sample_decision.sample_id"),
        action=cast(PolicyGenerationAction, action),
    )


# ---------------------------------------------------------------------------
# Shared strict helpers
# ---------------------------------------------------------------------------


def _payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _stored_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyGenerationError(f"{label} must be an object", code="invalid_record")
    return value


def _stored_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise PolicyGenerationError(f"{label} must be an array", code="invalid_record")
    return value


def _stored_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise PolicyGenerationError(f"{label} must be a string", code="invalid_record")
    return value


def _stored_identifier(value: object, label: str, *, length: int) -> str:
    identifier = _stored_string(value, label).lower()
    if len(identifier) != length or any(
        char not in "0123456789abcdef" for char in identifier
    ):
        raise PolicyGenerationError(
            f"{label} must be a {length}-character hexadecimal string",
            code="invalid_record",
        )
    return identifier


def _stored_non_negative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PolicyGenerationError(
            f"{label} must be a non-negative integer",
            code="invalid_record",
        )
    return value


def _stored_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise PolicyGenerationError(
            f"{label} must be an actual boolean",
            code="invalid_record",
        )
    return value


def _stored_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise PolicyGenerationError(
            f"{label} must be an ISO-8601 string",
            code="invalid_record",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyGenerationError(
            f"{label} must be an ISO-8601 datetime",
            code="invalid_record",
        ) from exc
    if parsed.tzinfo is None:
        raise PolicyGenerationError(
            f"{label} must include a timezone",
            code="invalid_record",
        )
    return parsed.astimezone(UTC)


def stable_semantic_signature(report: object) -> str:
    """Derive the generation semantic signature from a Discovery report.

    The signature is the report's Opportunity semantic signature when a
    single opportunity is selected; callers pass the exact signature.
    """
    return hashlib.sha256(_canonical_json(report).encode("utf-8")).hexdigest()


__all__ = [
    "POLICY_GENERATION_SCHEMA_VERSION",
    "PolicyGenerationAction",
    "PolicyGenerationConfirmation",
    "PolicyGenerationError",
    "PolicyGenerationEvidenceSample",
    "PolicyGenerationModelRun",
    "PolicyGenerationOutcome",
    "PolicyGenerationProposal",
    "PolicyGenerationRecord",
    "PolicyGenerationResult",
    "PolicyGenerationSampleDecision",
    "PolicyGenerationSettings",
    "PolicyGenerationStrategy",
    "policy_generation_proposal_from_dict",
    "policy_generation_record_from_dict",
]
