"""Deterministic, value-free discovery of Policy evolution opportunities."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from evopi.evolution.policy_discovery_protocol import (
    HumanConfirmationDecision,
    PolicyDiscoveryError,
    PolicyDiscoveryReport,
    PolicyDiscoverySettings,
    PolicyDiscoverySource,
    PolicyDiscoveryStats,
    PolicyOpportunity,
    PolicyOpportunityEvidence,
    PolicyOpportunityTheme,
    _canonical_json,
    _payload_digest,
    _source_digest,
)
from evopi.policy.types import RiskLevel

_RISK_LEVELS = {"low", "medium", "high", "critical"}
_THEME_PRIORITY = {
    "repeated_denial": 0,
    "mixed_decisions": 1,
    "repeated_approval": 2,
}
_RISK_PRIORITY = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass(slots=True, frozen=True)
class _Evaluation:
    tool_call_id: str
    tool_name: str
    policy_names: tuple[str, ...]
    risk_level: RiskLevel
    line_number: int


@dataclass(slots=True, frozen=True)
class _ConfirmationRequest:
    request_id: str
    run_id: str
    hook: str
    tool_call_id: str | None
    tool_name: str | None
    policy_names: tuple[str, ...]
    risk_level: RiskLevel
    arguments: dict[str, Any]
    trace_digest: str
    line_number: int


@dataclass(slots=True, frozen=True)
class _Sample:
    signature: str
    tool_name: str
    policy_names: tuple[str, ...]
    risk_level: RiskLevel
    argument_fields: tuple[str, ...]
    argument_shape_digest: str
    evidence: PolicyOpportunityEvidence


@dataclass(slots=True)
class _MutableStats:
    trace_count: int = 0
    record_count: int = 0
    matched_confirmations: int = 0
    eligible_human_decisions: int = 0
    excluded_automatic: int = 0
    excluded_cancelled: int = 0
    excluded_other_hooks: int = 0

    def freeze(self, *, opportunity_count: int) -> PolicyDiscoveryStats:
        return PolicyDiscoveryStats(
            trace_count=self.trace_count,
            record_count=self.record_count,
            matched_confirmations=self.matched_confirmations,
            eligible_human_decisions=self.eligible_human_decisions,
            excluded_automatic=self.excluded_automatic,
            excluded_cancelled=self.excluded_cancelled,
            excluded_other_hooks=self.excluded_other_hooks,
            opportunity_count=opportunity_count,
        )


def discover_policy_opportunities(
    paths: Iterable[str | Path],
    *,
    settings: PolicyDiscoverySettings | None = None,
) -> PolicyDiscoveryReport:
    """Discover recurring human confirmation patterns without executing runtime code."""

    configured = settings or PolicyDiscoverySettings()
    resolved = _resolve_trace_paths(paths)
    samples: list[_Sample] = []
    sources: list[PolicyDiscoverySource] = []
    stats = _MutableStats(trace_count=len(resolved))
    seen_digests: set[str] = set()

    for path in resolved:
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise PolicyDiscoveryError(
                f"Trace could not be read: {exc}",
                path=path,
            ) from exc
        trace_digest = hashlib.sha256(content).hexdigest()
        if trace_digest in seen_digests:
            continue
        seen_digests.add(trace_digest)
        trace_samples, record_count = _parse_trace(
            path,
            content,
            trace_digest=trace_digest,
            stats=stats,
        )
        samples.extend(trace_samples)
        sources.append(
            PolicyDiscoverySource(
                name=path.name,
                trace_digest=trace_digest,
                record_count=record_count,
            )
        )

    stats.trace_count = len(sources)
    opportunities = _build_opportunities(samples, configured)
    warnings = _discovery_warnings(stats)
    source_tuple = tuple(sources)
    return PolicyDiscoveryReport(
        input_digest=_source_digest(source_tuple),
        settings=configured,
        sources=source_tuple,
        stats=stats.freeze(opportunity_count=len(opportunities)),
        opportunities=tuple(opportunities),
        warnings=warnings,
    )


def _resolve_trace_paths(paths: Iterable[str | Path]) -> tuple[Path, ...]:
    resolved: dict[str, Path] = {}
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if path.is_file():
            resolved[str(path).casefold()] = path
            continue
        if path.is_dir():
            matches = [
                item.resolve()
                for item in path.rglob("*.jsonl")
                if item.is_file() and _is_conventional_trace_name(item.name)
            ]
            if not matches:
                raise PolicyDiscoveryError(
                    "Trace directory contains no conventional Trace files",
                    path=path,
                )
            for match in matches:
                resolved[str(match).casefold()] = match
            continue
        raise PolicyDiscoveryError("Trace path does not exist", path=path)
    if not resolved:
        raise PolicyDiscoveryError("at least one Trace path is required")
    return tuple(resolved[key] for key in sorted(resolved))


def _parse_trace(
    path: Path,
    content: bytes,
    *,
    trace_digest: str,
    stats: _MutableStats,
) -> tuple[list[_Sample], int]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyDiscoveryError("Trace is not valid UTF-8", path=path) from exc

    evaluations: dict[tuple[str, str], _Evaluation] = {}
    requests: dict[str, _ConfirmationRequest] = {}
    completed_requests: set[str] = set()
    samples: list[_Sample] = []
    record_count = 0

    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        record_count += 1
        stats.record_count += 1
        record = _load_record(path, line_number, line)
        event_type = record["type"]
        schema_version = record["schema_version"]
        run_id = record["run_id"]
        data = record["data"]
        created_at = record["created_at"]

        if event_type == "policy_evaluation":
            evaluation = _parse_policy_evaluation(path, line_number, run_id, data)
            if evaluation is not None:
                key = (run_id, evaluation.tool_call_id)
                if key in evaluations:
                    raise PolicyDiscoveryError(
                        "duplicate before_tool_call Policy evaluation",
                        path=path,
                        line_number=line_number,
                    )
                evaluations[key] = evaluation
            continue

        if event_type == "confirmation_request":
            request = _parse_confirmation_request(
                path,
                line_number,
                run_id,
                data,
                trace_digest=trace_digest,
            )
            if request.request_id in requests or request.request_id in completed_requests:
                raise PolicyDiscoveryError(
                    f"duplicate confirmation request ID '{request.request_id}'",
                    path=path,
                    line_number=line_number,
                )
            if schema_version == 2 and request.hook == "before_tool_call":
                assert request.tool_call_id is not None
                evaluation = evaluations.pop((run_id, request.tool_call_id), None)
                if evaluation is None:
                    raise PolicyDiscoveryError(
                        "confirmation request has no matching Policy evaluation",
                        path=path,
                        line_number=line_number,
                    )
                _validate_evaluation_match(path, line_number, evaluation, request)
            elif request.hook == "before_tool_call" and request.tool_call_id is not None:
                evaluation = evaluations.pop((run_id, request.tool_call_id), None)
                if evaluation is not None:
                    _validate_evaluation_match(path, line_number, evaluation, request)
            requests[request.request_id] = request
            continue

        if event_type == "confirmation_response":
            request_id, decision, automatic = _parse_confirmation_response(
                path,
                line_number,
                run_id,
                data,
            )
            if request_id not in requests:
                raise PolicyDiscoveryError(
                    f"confirmation response references unknown request '{request_id}'",
                    path=path,
                    line_number=line_number,
                )
            request = requests.pop(request_id)
            if request.run_id != run_id:
                raise PolicyDiscoveryError(
                    "confirmation response run_id does not match its request",
                    path=path,
                    line_number=line_number,
                )
            completed_requests.add(request_id)
            stats.matched_confirmations += 1
            if request.hook != "before_tool_call":
                stats.excluded_other_hooks += 1
            elif decision == "cancelled":
                stats.excluded_cancelled += 1
            elif automatic:
                stats.excluded_automatic += 1
            else:
                stats.eligible_human_decisions += 1
                samples.append(
                    _sample_from_confirmation(
                        request,
                        decision=cast(HumanConfirmationDecision, decision),
                        created_at=created_at,
                    )
                )

    if requests:
        request = min(requests.values(), key=lambda item: item.line_number)
        raise PolicyDiscoveryError(
            f"confirmation request '{request.request_id}' has no response",
            path=path,
            line_number=request.line_number,
        )
    if evaluations:
        evaluation = min(evaluations.values(), key=lambda item: item.line_number)
        raise PolicyDiscoveryError(
            "Policy evaluation requiring confirmation has no confirmation request",
            path=path,
            line_number=evaluation.line_number,
        )
    return samples, record_count


def _load_record(
    path: Path,
    line_number: int,
    line: str,
) -> dict[str, Any]:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        raise PolicyDiscoveryError(
            "invalid JSONL",
            path=path,
            line_number=line_number,
        ) from exc
    if not isinstance(raw, dict):
        raise PolicyDiscoveryError(
            "Trace record must be an object",
            path=path,
            line_number=line_number,
        )
    schema_version = raw.get("schema_version", 1)
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise PolicyDiscoveryError(
            "schema_version must be an integer",
            path=path,
            line_number=line_number,
        )
    if schema_version not in {1, 2}:
        raise PolicyDiscoveryError(
            f"unsupported Trace schema version {schema_version}",
            path=path,
            line_number=line_number,
        )
    event_type = raw.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise PolicyDiscoveryError(
            "Trace type must be a non-empty string",
            path=path,
            line_number=line_number,
        )
    data = raw.get("data")
    if not isinstance(data, dict):
        raise PolicyDiscoveryError(
            "Trace data must be an object",
            path=path,
            line_number=line_number,
        )
    run_id = raw.get("run_id")
    if event_type in {
        "policy_evaluation",
        "confirmation_request",
        "confirmation_response",
    } and (not isinstance(run_id, str) or not run_id):
        raise PolicyDiscoveryError(
            f"{event_type} run_id must be a non-empty string",
            path=path,
            line_number=line_number,
        )
    created_at = _optional_datetime(
        raw.get("created_at"),
        path=path,
        line_number=line_number,
    )
    return {
        "schema_version": schema_version,
        "type": event_type,
        "run_id": run_id,
        "created_at": created_at,
        "data": data,
    }


def _parse_policy_evaluation(
    path: Path,
    line_number: int,
    run_id: str,
    data: dict[str, Any],
) -> _Evaluation | None:
    if data.get("hook") != "before_tool_call":
        return None
    input_data = _required_object(data.get("input"), "policy_evaluation.input", path, line_number)
    tool_call = _required_object(
        input_data.get("tool_call"),
        "policy_evaluation.input.tool_call",
        path,
        line_number,
    )
    tool_call_id = _required_string(
        tool_call.get("id"),
        "policy_evaluation.input.tool_call.id",
        path,
        line_number,
    )
    tool_name = _required_string(
        tool_call.get("name"),
        "policy_evaluation.input.tool_call.name",
        path,
        line_number,
    )
    _required_object(
        tool_call.get("arguments"),
        "policy_evaluation.input.tool_call.arguments",
        path,
        line_number,
    )
    _required_object(
        input_data.get("arguments"),
        "policy_evaluation.input.arguments",
        path,
        line_number,
    )
    final = _required_object(data.get("final"), "policy_evaluation.final", path, line_number)
    if final.get("action") != "require_confirmation":
        return None
    risk_level = _risk_level(final.get("risk_level"), path, line_number)
    decisions = data.get("decisions")
    if not isinstance(decisions, list):
        raise PolicyDiscoveryError(
            "policy_evaluation.decisions must be an array",
            path=path,
            line_number=line_number,
        )
    names: set[str] = set()
    for item in decisions:
        if not isinstance(item, dict):
            raise PolicyDiscoveryError(
                "policy_evaluation.decisions item must be an object",
                path=path,
                line_number=line_number,
            )
        action = _required_string(
            item.get("action"),
            "policy_evaluation.decisions.action",
            path,
            line_number,
        )
        if action == "require_confirmation":
            names.add(
                _required_string(
                    item.get("policy_name"),
                    "policy_evaluation.decisions.policy_name",
                    path,
                    line_number,
                )
            )
    policy_names = tuple(sorted(names))
    return _Evaluation(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        policy_names=policy_names,
        risk_level=risk_level,
        line_number=line_number,
    )


def _parse_confirmation_request(
    path: Path,
    line_number: int,
    run_id: str,
    data: dict[str, Any],
    *,
    trace_digest: str,
) -> _ConfirmationRequest:
    request = _required_object(data.get("request"), "confirmation_request.request", path, line_number)
    request_id = _required_string(
        request.get("id"),
        "confirmation_request.request.id",
        path,
        line_number,
    )
    hook = _required_string(
        request.get("hook"),
        "confirmation_request.request.hook",
        path,
        line_number,
    )
    risk_level = _risk_level(request.get("risk_level"), path, line_number)
    raw_policy_names = request.get("policy_names")
    if not isinstance(raw_policy_names, list) or any(
        not isinstance(item, str) or not item for item in raw_policy_names
    ):
        raise PolicyDiscoveryError(
            "confirmation_request.request.policy_names must be an array of strings",
            path=path,
            line_number=line_number,
        )
    policy_names = tuple(sorted(set(raw_policy_names)))
    raw_arguments = request.get("arguments")
    arguments = (
        {}
        if hook != "before_tool_call" and raw_arguments is None
        else _required_object(
            raw_arguments,
            "confirmation_request.request.arguments",
            path,
            line_number,
        )
    )
    tool_call_id: str | None = None
    tool_name: str | None = None
    if hook == "before_tool_call":
        tool_call = _required_object(
            request.get("tool_call"),
            "confirmation_request.request.tool_call",
            path,
            line_number,
        )
        tool_call_id = _required_string(
            tool_call.get("id"),
            "confirmation_request.request.tool_call.id",
            path,
            line_number,
        )
        tool_name = _required_string(
            tool_call.get("name"),
            "confirmation_request.request.tool_call.name",
            path,
            line_number,
        )
        _required_object(
            tool_call.get("arguments"),
            "confirmation_request.request.tool_call.arguments",
            path,
            line_number,
        )
    return _ConfirmationRequest(
        request_id=request_id,
        run_id=run_id,
        hook=hook,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        policy_names=policy_names,
        risk_level=risk_level,
        arguments=arguments,
        trace_digest=trace_digest,
        line_number=line_number,
    )


def _parse_confirmation_response(
    path: Path,
    line_number: int,
    run_id: str,
    data: dict[str, Any],
) -> tuple[str, str, bool]:
    response = _required_object(
        data.get("response"),
        "confirmation_response.response",
        path,
        line_number,
    )
    request_id = _required_string(
        response.get("request_id"),
        "confirmation_response.response.request_id",
        path,
        line_number,
    )
    decision = response.get("decision")
    if decision not in {"approve", "deny", "cancelled"}:
        raise PolicyDiscoveryError(
            "confirmation_response.response.decision is invalid",
            path=path,
            line_number=line_number,
        )
    metadata = _required_object(
        response.get("metadata", {}),
        "confirmation_response.response.metadata",
        path,
        line_number,
    )
    automatic = metadata.get("automatic", False)
    if not isinstance(automatic, bool):
        raise PolicyDiscoveryError(
            "confirmation response automatic metadata must be a boolean",
            path=path,
            line_number=line_number,
        )
    return request_id, str(decision), automatic


def _validate_evaluation_match(
    path: Path,
    line_number: int,
    evaluation: _Evaluation,
    request: _ConfirmationRequest,
) -> None:
    if (
        request.tool_name != evaluation.tool_name
        or request.policy_names != evaluation.policy_names
        or request.risk_level != evaluation.risk_level
    ):
        raise PolicyDiscoveryError(
            "confirmation request does not match its Policy evaluation",
            path=path,
            line_number=line_number,
        )


def _sample_from_confirmation(
    request: _ConfirmationRequest,
    *,
    decision: HumanConfirmationDecision,
    created_at: datetime | None,
) -> _Sample:
    assert request.tool_name is not None
    shape = _argument_shape(request.arguments)
    shape_payload = json.dumps(shape, sort_keys=True, separators=(",", ":"))
    shape_digest = hashlib.sha256(shape_payload.encode("utf-8")).hexdigest()
    signature_payload = {
        "hook": "before_tool_call",
        "tool_name": request.tool_name,
        "policy_names": list(request.policy_names),
        "risk_level": request.risk_level,
        "argument_shape": shape,
    }
    signature = _payload_digest(signature_payload)
    return _Sample(
        signature=signature,
        tool_name=request.tool_name,
        policy_names=request.policy_names,
        risk_level=request.risk_level,
        argument_fields=tuple(sorted(request.arguments)),
        argument_shape_digest=shape_digest,
        evidence=PolicyOpportunityEvidence(
            trace_digest=request.trace_digest,
            line_number=request.line_number,
            run_id=request.run_id,
            decision=decision,
            created_at=created_at,
        ),
    )


def _build_opportunities(
    samples: Sequence[_Sample],
    settings: PolicyDiscoverySettings,
) -> list[PolicyOpportunity]:
    grouped: dict[str, list[_Sample]] = defaultdict(list)
    for sample in samples:
        grouped[sample.signature].append(sample)
    opportunities: list[PolicyOpportunity] = []
    for signature, group in grouped.items():
        run_ids = {sample.evidence.run_id for sample in group}
        if len(group) < settings.min_occurrences or len(run_ids) < settings.min_runs:
            continue
        approvals = sum(sample.evidence.decision == "approve" for sample in group)
        denials = len(group) - approvals
        if denials and not approvals:
            theme: PolicyOpportunityTheme = "repeated_denial"
        elif approvals and denials:
            theme = "mixed_decisions"
        else:
            theme = "repeated_approval"
        ordered_evidence = sorted(
            (sample.evidence for sample in group),
            key=_evidence_sort_key,
        )
        timestamps = [
            item.created_at for item in ordered_evidence if item.created_at is not None
        ]
        prototype = group[0]
        opportunities.append(
            PolicyOpportunity(
                semantic_signature=signature,
                theme=theme,
                hook="before_tool_call",
                tool_name=prototype.tool_name,
                policy_names=prototype.policy_names,
                risk_level=prototype.risk_level,
                argument_fields=prototype.argument_fields,
                argument_shape_digest=prototype.argument_shape_digest,
                occurrence_count=len(group),
                run_count=len(run_ids),
                approve_count=approvals,
                deny_count=denials,
                first_seen=min(timestamps) if timestamps else None,
                last_seen=max(timestamps) if timestamps else None,
                evidence=tuple(ordered_evidence[: settings.max_evidence_refs]),
                omitted_evidence_count=max(0, len(group) - settings.max_evidence_refs),
            )
        )
    opportunities.sort(key=_opportunity_sort_key)
    return opportunities


def _discovery_warnings(stats: _MutableStats) -> tuple[str, ...]:
    warnings: list[str] = []
    if stats.excluded_automatic:
        warnings.append(
            f"Excluded {stats.excluded_automatic} automatic confirmation response(s)"
        )
    if stats.excluded_cancelled:
        warnings.append(
            f"Excluded {stats.excluded_cancelled} cancelled confirmation response(s)"
        )
    if stats.excluded_other_hooks:
        warnings.append(
            f"Excluded {stats.excluded_other_hooks} confirmation response(s) "
            "outside before_tool_call"
        )
    return tuple(warnings)


def _is_conventional_trace_name(name: str) -> bool:
    normalized = name.casefold()
    return (
        normalized == "trace.jsonl"
        or normalized.startswith("trace-")
        or normalized.endswith(".trace.jsonl")
    )


def _argument_shape(value: Any) -> Any:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        shapes = {_canonical_json(_argument_shape(item)) for item in value}
        return {"array": [json.loads(item) for item in sorted(shapes)]}
    if isinstance(value, dict):
        return {
            "object": {
                str(key): _argument_shape(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        }
    raise ValueError(f"unsupported argument value type: {type(value).__name__}")


def _opportunity_sort_key(opportunity: PolicyOpportunity) -> tuple[Any, ...]:
    timestamp = opportunity.last_seen.timestamp() if opportunity.last_seen is not None else 0.0
    return (
        _THEME_PRIORITY[opportunity.theme],
        _RISK_PRIORITY[opportunity.risk_level],
        -opportunity.run_count,
        -opportunity.occurrence_count,
        -timestamp,
        opportunity.semantic_signature,
    )


def _evidence_sort_key(evidence: PolicyOpportunityEvidence) -> tuple[Any, ...]:
    timestamp = evidence.created_at.timestamp() if evidence.created_at is not None else 0.0
    return (timestamp, evidence.trace_digest, evidence.line_number)


def _optional_datetime(
    value: Any,
    *,
    path: Path,
    line_number: int,
) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PolicyDiscoveryError(
            "created_at must be an ISO-8601 string",
            path=path,
            line_number=line_number,
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyDiscoveryError(
            "created_at must be an ISO-8601 datetime",
            path=path,
            line_number=line_number,
        ) from exc
    if parsed.tzinfo is None:
        raise PolicyDiscoveryError(
            "created_at must include a timezone",
            path=path,
            line_number=line_number,
        )
    return parsed.astimezone(UTC)


def _required_object(
    value: Any,
    label: str,
    path: Path,
    line_number: int,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyDiscoveryError(
            f"{label} must be an object",
            path=path,
            line_number=line_number,
        )
    return value


def _required_string(
    value: Any,
    label: str,
    path: Path,
    line_number: int,
) -> str:
    if not isinstance(value, str) or not value:
        raise PolicyDiscoveryError(
            f"{label} must be a non-empty string",
            path=path,
            line_number=line_number,
        )
    return value


def _risk_level(value: Any, path: Path, line_number: int) -> RiskLevel:
    if value not in _RISK_LEVELS:
        raise PolicyDiscoveryError(
            "risk_level is invalid",
            path=path,
            line_number=line_number,
        )
    return cast(RiskLevel, value)


__all__ = ["discover_policy_opportunities"]
