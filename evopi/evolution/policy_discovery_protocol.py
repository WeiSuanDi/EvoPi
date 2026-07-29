"""Public protocol and strict codec for Policy Opportunity reports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast
from uuid import uuid4

from evopi.policy.types import RiskLevel

POLICY_DISCOVERY_SCHEMA_VERSION = 1
PolicyOpportunityTheme: TypeAlias = Literal[
    "repeated_denial",
    "mixed_decisions",
    "repeated_approval",
]
HumanConfirmationDecision: TypeAlias = Literal["approve", "deny"]

_THEMES = {"repeated_denial", "mixed_decisions", "repeated_approval"}
_RISK_LEVELS = {"low", "medium", "high", "critical"}


class PolicyDiscoveryError(ValueError):
    """Raised when discovery evidence or a stored report is invalid."""

    def __init__(
        self,
        reason: str,
        *,
        path: str | Path | None = None,
        line_number: int | None = None,
    ) -> None:
        self.path = Path(path).expanduser().resolve() if path is not None else None
        self.line_number = line_number
        location = ""
        if self.path is not None:
            location = str(self.path)
            if line_number is not None:
                location += f":{line_number}"
            location += ": "
        super().__init__(location + reason)


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyDiscoverySettings:
    min_occurrences: int = 3
    min_runs: int = 2
    max_evidence_refs: int = 50

    def __post_init__(self) -> None:
        if self.min_occurrences < 1:
            raise ValueError("min_occurrences must be at least 1")
        if self.min_runs < 1:
            raise ValueError("min_runs must be at least 1")
        if self.max_evidence_refs < 1:
            raise ValueError("max_evidence_refs must be at least 1")


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyDiscoverySource:
    name: str
    trace_digest: str
    record_count: int


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyOpportunityEvidence:
    trace_digest: str
    line_number: int
    run_id: str
    decision: HumanConfirmationDecision
    created_at: datetime | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyOpportunity:
    semantic_signature: str
    theme: PolicyOpportunityTheme
    hook: Literal["before_tool_call"]
    tool_name: str
    policy_names: tuple[str, ...]
    risk_level: RiskLevel
    argument_fields: tuple[str, ...]
    argument_shape_digest: str
    occurrence_count: int
    run_count: int
    approve_count: int
    deny_count: int
    first_seen: datetime | None
    last_seen: datetime | None
    evidence: tuple[PolicyOpportunityEvidence, ...]
    omitted_evidence_count: int = 0


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyDiscoveryStats:
    trace_count: int = 0
    record_count: int = 0
    matched_confirmations: int = 0
    eligible_human_decisions: int = 0
    excluded_automatic: int = 0
    excluded_cancelled: int = 0
    excluded_other_hooks: int = 0
    opportunity_count: int = 0


@dataclass(slots=True, frozen=True, kw_only=True)
class PolicyDiscoveryReport:
    input_digest: str
    settings: PolicyDiscoverySettings
    sources: tuple[PolicyDiscoverySource, ...]
    stats: PolicyDiscoveryStats
    opportunities: tuple[PolicyOpportunity, ...]
    warnings: tuple[str, ...] = ()
    report_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: int = POLICY_DISCOVERY_SCHEMA_VERSION
    report_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = _report_payload(self)
        payload["report_digest"] = self.report_digest or _payload_digest(payload)
        return payload


def policy_discovery_report_from_dict(value: object) -> PolicyDiscoveryReport:
    """Decode one strictly validated Policy Discovery report."""

    if not isinstance(value, dict):
        raise PolicyDiscoveryError("Policy Discovery report must be an object")
    raw = dict(value)
    digest = raw.pop("report_digest", None)
    if not isinstance(digest, str) or digest != _payload_digest(raw):
        raise PolicyDiscoveryError("Policy Discovery report digest does not match its content")
    if raw.get("schema_version") != POLICY_DISCOVERY_SCHEMA_VERSION:
        raise PolicyDiscoveryError("unsupported Policy Discovery report schema")
    try:
        report_id = _stored_identifier(raw["report_id"], "report_id", length=32)
        created_at = _stored_datetime(raw["created_at"], "created_at")
        input_digest = _stored_identifier(
            raw["input_digest"],
            "input_digest",
            length=64,
        )
        settings_raw = _stored_object(raw["settings"], "settings")
        settings = PolicyDiscoverySettings(
            min_occurrences=_stored_positive_int(
                settings_raw.get("min_occurrences"),
                "settings.min_occurrences",
            ),
            min_runs=_stored_positive_int(
                settings_raw.get("min_runs"),
                "settings.min_runs",
            ),
            max_evidence_refs=_stored_positive_int(
                settings_raw.get("max_evidence_refs"),
                "settings.max_evidence_refs",
            ),
        )
        sources = tuple(
            _stored_source(item)
            for item in _stored_list(raw["sources"], "sources")
        )
        opportunities = tuple(
            _stored_opportunity(item)
            for item in _stored_list(raw["opportunities"], "opportunities")
        )
        stats = _stored_stats(raw["stats"])
        warnings = tuple(
            _stored_string(item, "warnings item")
            for item in _stored_list(raw.get("warnings", []), "warnings")
        )
    except PolicyDiscoveryError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyDiscoveryError(f"invalid Policy Discovery report: {exc}") from exc
    if stats.trace_count != len(sources):
        raise PolicyDiscoveryError("Policy Discovery source count does not match stats")
    if stats.opportunity_count != len(opportunities):
        raise PolicyDiscoveryError("Policy Discovery opportunity count does not match stats")
    if input_digest != _source_digest(sources):
        raise PolicyDiscoveryError(
            "Policy Discovery input digest does not match its sources"
        )
    return PolicyDiscoveryReport(
        input_digest=input_digest,
        settings=settings,
        sources=sources,
        stats=stats,
        opportunities=opportunities,
        warnings=warnings,
        report_id=report_id,
        created_at=created_at,
        schema_version=POLICY_DISCOVERY_SCHEMA_VERSION,
        report_digest=digest,
    )


def _report_payload(report: PolicyDiscoveryReport) -> dict[str, Any]:
    return {
        "schema_version": report.schema_version,
        "report_id": report.report_id,
        "created_at": report.created_at.isoformat(),
        "input_digest": report.input_digest,
        "settings": {
            "min_occurrences": report.settings.min_occurrences,
            "min_runs": report.settings.min_runs,
            "max_evidence_refs": report.settings.max_evidence_refs,
        },
        "sources": [
            {
                "name": source.name,
                "trace_digest": source.trace_digest,
                "record_count": source.record_count,
            }
            for source in report.sources
        ],
        "stats": {
            "trace_count": report.stats.trace_count,
            "record_count": report.stats.record_count,
            "matched_confirmations": report.stats.matched_confirmations,
            "eligible_human_decisions": report.stats.eligible_human_decisions,
            "excluded_automatic": report.stats.excluded_automatic,
            "excluded_cancelled": report.stats.excluded_cancelled,
            "excluded_other_hooks": report.stats.excluded_other_hooks,
            "opportunity_count": report.stats.opportunity_count,
        },
        "opportunities": [
            {
                "semantic_signature": item.semantic_signature,
                "theme": item.theme,
                "hook": item.hook,
                "tool_name": item.tool_name,
                "policy_names": list(item.policy_names),
                "risk_level": item.risk_level,
                "argument_fields": list(item.argument_fields),
                "argument_shape_digest": item.argument_shape_digest,
                "occurrence_count": item.occurrence_count,
                "run_count": item.run_count,
                "approve_count": item.approve_count,
                "deny_count": item.deny_count,
                "first_seen": item.first_seen.isoformat() if item.first_seen else None,
                "last_seen": item.last_seen.isoformat() if item.last_seen else None,
                "evidence": [
                    {
                        "trace_digest": evidence.trace_digest,
                        "line_number": evidence.line_number,
                        "run_id": evidence.run_id,
                        "decision": evidence.decision,
                        "created_at": (
                            evidence.created_at.isoformat()
                            if evidence.created_at is not None
                            else None
                        ),
                    }
                    for evidence in item.evidence
                ],
                "omitted_evidence_count": item.omitted_evidence_count,
            }
            for item in report.opportunities
        ],
        "warnings": list(report.warnings),
    }


def _payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _source_digest(sources: tuple[PolicyDiscoverySource, ...]) -> str:
    return _payload_digest(
        {
            "traces": [
                {
                    "trace_digest": source.trace_digest,
                    "record_count": source.record_count,
                }
                for source in sources
            ]
        }
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _stored_source(value: object) -> PolicyDiscoverySource:
    raw = _stored_object(value, "source")
    return PolicyDiscoverySource(
        name=_stored_string(raw.get("name"), "source.name"),
        trace_digest=_stored_identifier(
            raw.get("trace_digest"),
            "source.trace_digest",
            length=64,
        ),
        record_count=_stored_non_negative_int(
            raw.get("record_count"),
            "source.record_count",
        ),
    )


def _stored_stats(value: object) -> PolicyDiscoveryStats:
    raw = _stored_object(value, "stats")
    return PolicyDiscoveryStats(
        trace_count=_stored_non_negative_int(raw.get("trace_count"), "stats.trace_count"),
        record_count=_stored_non_negative_int(raw.get("record_count"), "stats.record_count"),
        matched_confirmations=_stored_non_negative_int(
            raw.get("matched_confirmations"),
            "stats.matched_confirmations",
        ),
        eligible_human_decisions=_stored_non_negative_int(
            raw.get("eligible_human_decisions"),
            "stats.eligible_human_decisions",
        ),
        excluded_automatic=_stored_non_negative_int(
            raw.get("excluded_automatic"),
            "stats.excluded_automatic",
        ),
        excluded_cancelled=_stored_non_negative_int(
            raw.get("excluded_cancelled"),
            "stats.excluded_cancelled",
        ),
        excluded_other_hooks=_stored_non_negative_int(
            raw.get("excluded_other_hooks"),
            "stats.excluded_other_hooks",
        ),
        opportunity_count=_stored_non_negative_int(
            raw.get("opportunity_count"),
            "stats.opportunity_count",
        ),
    )


def _stored_opportunity(value: object) -> PolicyOpportunity:
    raw = _stored_object(value, "opportunity")
    theme = raw.get("theme")
    if theme not in _THEMES:
        raise PolicyDiscoveryError("opportunity.theme is invalid")
    risk = raw.get("risk_level")
    if risk not in _RISK_LEVELS:
        raise PolicyDiscoveryError("opportunity.risk_level is invalid")
    if raw.get("hook") != "before_tool_call":
        raise PolicyDiscoveryError("opportunity.hook must be before_tool_call")
    policy_names = tuple(
        _stored_string(item, "opportunity.policy_names item")
        for item in _stored_list(raw.get("policy_names"), "opportunity.policy_names")
    )
    argument_fields = tuple(
        _stored_string(item, "opportunity.argument_fields item")
        for item in _stored_list(raw.get("argument_fields"), "opportunity.argument_fields")
    )
    evidence = tuple(
        _stored_evidence(item)
        for item in _stored_list(raw.get("evidence"), "opportunity.evidence")
    )
    first_seen = _stored_optional_datetime(raw.get("first_seen"), "opportunity.first_seen")
    last_seen = _stored_optional_datetime(raw.get("last_seen"), "opportunity.last_seen")
    occurrence_count = _stored_positive_int(
        raw.get("occurrence_count"),
        "opportunity.occurrence_count",
    )
    approve_count = _stored_non_negative_int(
        raw.get("approve_count"),
        "opportunity.approve_count",
    )
    deny_count = _stored_non_negative_int(
        raw.get("deny_count"),
        "opportunity.deny_count",
    )
    omitted = _stored_non_negative_int(
        raw.get("omitted_evidence_count"),
        "opportunity.omitted_evidence_count",
    )
    if approve_count + deny_count != occurrence_count:
        raise PolicyDiscoveryError("opportunity decision counts do not match occurrences")
    if len(evidence) + omitted != occurrence_count:
        raise PolicyDiscoveryError("opportunity evidence counts do not match occurrences")
    return PolicyOpportunity(
        semantic_signature=_stored_identifier(
            raw.get("semantic_signature"),
            "opportunity.semantic_signature",
            length=64,
        ),
        theme=cast(PolicyOpportunityTheme, theme),
        hook="before_tool_call",
        tool_name=_stored_string(raw.get("tool_name"), "opportunity.tool_name"),
        policy_names=policy_names,
        risk_level=cast(RiskLevel, risk),
        argument_fields=argument_fields,
        argument_shape_digest=_stored_identifier(
            raw.get("argument_shape_digest"),
            "opportunity.argument_shape_digest",
            length=64,
        ),
        occurrence_count=occurrence_count,
        run_count=_stored_positive_int(raw.get("run_count"), "opportunity.run_count"),
        approve_count=approve_count,
        deny_count=deny_count,
        first_seen=first_seen,
        last_seen=last_seen,
        evidence=evidence,
        omitted_evidence_count=omitted,
    )


def _stored_evidence(value: object) -> PolicyOpportunityEvidence:
    raw = _stored_object(value, "opportunity evidence")
    decision = raw.get("decision")
    if decision not in {"approve", "deny"}:
        raise PolicyDiscoveryError("opportunity evidence decision is invalid")
    return PolicyOpportunityEvidence(
        trace_digest=_stored_identifier(
            raw.get("trace_digest"),
            "opportunity evidence trace_digest",
            length=64,
        ),
        line_number=_stored_positive_int(
            raw.get("line_number"),
            "opportunity evidence line_number",
        ),
        run_id=_stored_string(raw.get("run_id"), "opportunity evidence run_id"),
        decision=cast(HumanConfirmationDecision, decision),
        created_at=_stored_optional_datetime(
            raw.get("created_at"),
            "opportunity evidence created_at",
        ),
    )


def _stored_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyDiscoveryError(f"{label} must be an object")
    return value


def _stored_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise PolicyDiscoveryError(f"{label} must be an array")
    return value


def _stored_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PolicyDiscoveryError(f"{label} must be a non-empty string")
    return value


def _stored_identifier(value: object, label: str, *, length: int) -> str:
    identifier = _stored_string(value, label).lower()
    if len(identifier) != length or any(
        char not in "0123456789abcdef" for char in identifier
    ):
        raise PolicyDiscoveryError(
            f"{label} must be a {length}-character hexadecimal string"
        )
    return identifier


def _stored_non_negative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PolicyDiscoveryError(f"{label} must be a non-negative integer")
    return value


def _stored_positive_int(value: object, label: str) -> int:
    parsed = _stored_non_negative_int(value, label)
    if parsed == 0:
        raise PolicyDiscoveryError(f"{label} must be greater than zero")
    return parsed


def _stored_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise PolicyDiscoveryError(f"{label} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyDiscoveryError(f"{label} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise PolicyDiscoveryError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _stored_optional_datetime(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    return _stored_datetime(value, label)


__all__ = [
    "POLICY_DISCOVERY_SCHEMA_VERSION",
    "PolicyDiscoveryError",
    "PolicyDiscoveryReport",
    "PolicyDiscoverySettings",
    "PolicyDiscoverySource",
    "PolicyDiscoveryStats",
    "PolicyOpportunity",
    "PolicyOpportunityEvidence",
    "PolicyOpportunityTheme",
    "policy_discovery_report_from_dict",
]
