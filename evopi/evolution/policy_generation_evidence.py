"""Evidence reconstruction and deterministic selection for generation.

Raw Trace values are untrusted data.  This module revalidates the exact
correlation the Discovery report bound (digest, line, Run, human decision,
Tool, Policy names, risk, argument fields, argument-shape digest) and then
selects a bounded, balanced evidence set for model transmission.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from evopi.evolution.policy_discovery_protocol import (
    PolicyDiscoveryError,
    PolicyDiscoveryReport,
    PolicyOpportunity,
    policy_discovery_report_from_dict,
)
from evopi.evolution.policy_generation_protocol import (
    PolicyGenerationError,
    PolicyGenerationEvidenceSample,
    PolicyGenerationSettings,
)


class PolicyGenerationEvidenceError(PolicyGenerationError):
    """Raised when evidence cannot be reconstructed without weakening binds."""


# ---------------------------------------------------------------------------
# Opportunity resolution
# ---------------------------------------------------------------------------


def resolve_policy_opportunity(
    report: PolicyDiscoveryReport,
    prefix: str,
) -> PolicyOpportunity:
    """Resolve *prefix* to exactly one Opportunity.

    Accepts an exact 64-character signature or a unique hexadecimal prefix
    of at least 8 characters.  Short, missing, and ambiguous input is
    rejected.
    """
    signature = prefix.lower()
    if len(signature) < 8:
        raise PolicyGenerationEvidenceError(
            "opportunity signature prefix must be at least 8 characters",
            code="ambiguous_opportunity",
        )
    matches = [
        item
        for item in report.opportunities
        if item.semantic_signature.startswith(signature)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise PolicyGenerationEvidenceError(
            "no Opportunity matches the signature prefix",
            code="missing_opportunity",
        )
    raise PolicyGenerationEvidenceError(
        "opportunity signature prefix is ambiguous",
        code="ambiguous_opportunity",
    )


# ---------------------------------------------------------------------------
# Trace path resolution (same conventional-name rules as Discovery)
# ---------------------------------------------------------------------------


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
                raise PolicyGenerationEvidenceError(
                    "Trace directory contains no conventional Trace files",
                    code="missing_trace",
                    path=str(path),
                )
            for match in matches:
                resolved[str(match).casefold()] = match
            continue
        raise PolicyGenerationEvidenceError(
            "Trace path does not exist",
            code="missing_trace",
            path=str(path),
        )
    if not resolved:
        raise PolicyGenerationEvidenceError(
            "at least one Trace path is required",
            code="missing_trace",
        )
    return tuple(resolved[key] for key in sorted(resolved))


def _is_conventional_trace_name(name: str) -> bool:
    normalized = name.casefold()
    return (
        normalized == "trace.jsonl"
        or normalized.startswith("trace-")
        or normalized.endswith(".trace.jsonl")
    )


# ---------------------------------------------------------------------------
# Evidence loading
# ---------------------------------------------------------------------------


def load_policy_generation_evidence(
    report: PolicyDiscoveryReport,
    opportunity: PolicyOpportunity,
    paths: Iterable[str | Path],
    settings: PolicyGenerationSettings | None = None,
) -> tuple[PolicyGenerationEvidenceSample, ...]:
    """Reconstruct raw evidence for one Opportunity.

    Returns a deterministic, balanced selection of at most
    ``settings.max_evidence`` samples.  Selection balances approve/deny,
    then distinct Runs, then chronological/reference order.
    """
    settings = settings or PolicyGenerationSettings()
    referenced_digests = {evidence.trace_digest for evidence in opportunity.evidence}
    if not referenced_digests:
        raise PolicyGenerationEvidenceError(
            "Opportunity has no evidence references",
            code="missing_evidence",
        )

    samples: list[PolicyGenerationEvidenceSample] = []
    seen_keys: set[tuple[str, int]] = set()

    for path in _resolve_trace_paths(paths):
        digest = _file_digest(path)
        if digest not in referenced_digests:
            continue  # digest not referenced by the selected Opportunity
        raw = _parse_trace_evidence(
            path,
            digest,
            opportunity,
        )
        for sample in raw:
            key = (sample.trace_digest, sample.line_number)
            if key in seen_keys:
                raise PolicyGenerationEvidenceError(
                    "duplicate evidence sample after Trace merge",
                    code="duplicate_evidence",
                )
            seen_keys.add(key)
            samples.append(sample)

    if not samples:
        raise PolicyGenerationEvidenceError(
            "no evidence samples matched the referenced Traces",
            code="missing_evidence",
        )

    selected = _select_balanced(samples, settings.max_evidence)
    return tuple(selected)


def _parse_trace_evidence(
    path: Path,
    trace_digest: str,
    opportunity: PolicyOpportunity,
) -> list[PolicyGenerationEvidenceSample]:
    """Parse one Trace file and return samples for the referenced lines.

    The Trace must contain the exact correlated request/response records
    for each referenced line.  Automatic/cancelled evidence and any drift
    (line, Run, decision, Tool, Policy names, risk, argument shape) is
    rejected.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        raise PolicyGenerationEvidenceError(
            f"Trace is not readable UTF-8: {exc}",
            code="invalid_trace",
            path=str(path),
        ) from exc

    # Generation consumes the same Trace protocol as Discovery.  Reuse its
    # strict stream validator before reconstructing raw arguments so ordering,
    # duplicate IDs, unmatched evaluations, and v1/v2 correlation rules cannot
    # drift between the two stages.  Discovery is deterministic and performs
    # no model, tool, confirmation, or persistence work here.
    from evopi.evolution.policy_discovery import discover_policy_opportunities

    try:
        discover_policy_opportunities([path])
    except PolicyDiscoveryError as exc:
        reason = str(exc)
        error_path = exc.path or path
        location = str(error_path)
        if exc.line_number is not None:
            location += f":{exc.line_number}"
        prefix = location + ": "
        if reason.startswith(prefix):
            reason = reason[len(prefix) :]
        raise PolicyGenerationEvidenceError(
            f"Trace failed strict stream validation: {reason}",
            code="invalid_trace",
            path=str(error_path),
            line_number=exc.line_number,
        ) from exc

    # Collect evaluation/request/response correlation per line
    evaluations: dict[int, _Evaluation] = {}
    evaluations_by_key: dict[tuple[str, str], _Evaluation] = {}
    requests: dict[str, _Request] = {}
    responses: dict[str, _Response] = {}

    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        record = _load_record(line)
        event_type = record["type"]
        schema_version = record.get("schema_version")
        # Missing schema_version means v1 (Discovery-compatible); explicit
        # unsupported versions fail closed.
        if schema_version is None:
            schema_version = 1
        if schema_version not in _SUPPORTED_SCHEMAS:
            raise PolicyGenerationEvidenceError(
                f"unsupported Trace schema version: {schema_version}",
                code="invalid_trace",
                path=str(path),
                line_number=line_number,
            )
        run_id = record.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise PolicyGenerationEvidenceError(
                "Trace record run_id must be a non-empty string",
                code="invalid_trace",
                path=str(path),
                line_number=line_number,
            )
        data = record["data"]
        if event_type == "policy_evaluation":
            evaluation = _parse_evaluation(run_id, data)
            if evaluation is not None:
                evaluation.line_number = line_number
                key = (run_id, evaluation.tool_call_id)
                if key in evaluations_by_key:
                    raise PolicyGenerationEvidenceError(
                        f"duplicate Policy evaluation for ToolCall {key}",
                        code="invalid_trace",
                        path=str(path),
                        line_number=line_number,
                    )
                evaluations[line_number] = evaluation
                evaluations_by_key[key] = evaluation
        elif event_type == "confirmation_request":
            request = _parse_request(run_id, data)
            if request is not None:
                request.line_number = line_number
                if request.request_id in requests:
                    raise PolicyGenerationEvidenceError(
                        f"duplicate confirmation request ID {request.request_id}",
                        code="invalid_trace",
                        path=str(path),
                        line_number=line_number,
                    )
                requests[request.request_id] = request
        elif event_type == "confirmation_response":
            response = _parse_response(run_id, data)
            if response is not None:
                if response.request_id in responses:
                    raise PolicyGenerationEvidenceError(
                        f"duplicate confirmation response for {response.request_id}",
                        code="invalid_trace",
                        path=str(path),
                        line_number=line_number,
                    )
                responses[response.request_id] = response

    samples: list[PolicyGenerationEvidenceSample] = []
    for evidence in opportunity.evidence:
        if evidence.trace_digest != trace_digest:
            continue
        # The Opportunity evidence line_number references the confirmation
        # request record; correlation is exact by request ID, then by
        # ToolCall ID and Run ID — never the nearest prior evaluation.
        request = _find_request_by_line(requests, evidence.line_number)
        if request is None:
            raise PolicyGenerationEvidenceError(
                "referenced line has no confirmation request",
                code="correlation_drift",
                path=str(path),
                line_number=evidence.line_number,
            )
        if request.run_id != evidence.run_id:
            raise PolicyGenerationEvidenceError(
                "confirmation request run_id drifted from Opportunity evidence",
                code="correlation_drift",
                path=str(path),
                line_number=evidence.line_number,
            )
        # Exact ToolCall correlation: the request and its Policy evaluation
        # must share the same ToolCall ID in the same Run.
        evaluation = evaluations_by_key.get(
            (request.run_id, request.tool_call_id)
        )
        if evaluation is None:
            raise PolicyGenerationEvidenceError(
                "confirmation request has no matching Policy evaluation "
                f"(ToolCall {request.tool_call_id!r} in run {request.run_id})",
                code="correlation_drift",
                path=str(path),
                line_number=evidence.line_number,
            )
        if evaluation.tool_name != opportunity.tool_name:
            raise PolicyGenerationEvidenceError(
                "evaluation tool drifted from Opportunity",
                code="correlation_drift",
                path=str(path),
                line_number=evidence.line_number,
            )
        if evaluation.policy_names != opportunity.policy_names:
            raise PolicyGenerationEvidenceError(
                "evaluation Policy names drifted from Opportunity",
                code="correlation_drift",
                path=str(path),
                line_number=evidence.line_number,
            )
        if evaluation.risk_level != opportunity.risk_level:
            raise PolicyGenerationEvidenceError(
                "evaluation risk drifted from Opportunity",
                code="correlation_drift",
                path=str(path),
                line_number=evidence.line_number,
            )
        if _argument_shape_digest(
            request.arguments or evaluation.arguments
        ) != opportunity.argument_shape_digest:
            raise PolicyGenerationEvidenceError(
                "argument shape drifted from Opportunity",
                code="correlation_drift",
                path=str(path),
                line_number=evidence.line_number,
            )

        response = responses.get(request.request_id)
        if response is None:
            raise PolicyGenerationEvidenceError(
                "confirmation request has no response",
                code="correlation_drift",
                path=str(path),
                line_number=evidence.line_number,
            )
        if response.run_id != request.run_id:
            raise PolicyGenerationEvidenceError(
                "confirmation response run_id does not match its request",
                code="correlation_drift",
                path=str(path),
                line_number=evidence.line_number,
            )
        if response.decision != evidence.decision:
            raise PolicyGenerationEvidenceError(
                "human decision drifted from Opportunity",
                code="correlation_drift",
                path=str(path),
                line_number=evidence.line_number,
            )
        if response.automatic or response.decision == "cancelled":
            raise PolicyGenerationEvidenceError(
                "automatic or cancelled evidence is not eligible",
                code="correlation_drift",
                path=str(path),
                line_number=evidence.line_number,
            )
        samples.append(
            PolicyGenerationEvidenceSample(
                sample_id=f"{trace_digest[:8]}:{evidence.line_number}",
                trace_digest=trace_digest,
                line_number=evidence.line_number,
                run_id=evidence.run_id,
                human_decision=evidence.decision,
                tool_name=evaluation.tool_name,
                arguments=evaluation.arguments,
            )
        )
    for sample in samples:
        _validate_evidence_sample(sample, path=path)
    return samples


def _validate_evidence_sample(
    sample: PolicyGenerationEvidenceSample,
    *,
    path: Path | None = None,
) -> None:
    """Runtime validation for one reconstructed evidence sample."""
    if not sample.sample_id or not sample.sample_id.strip():
        raise PolicyGenerationEvidenceError(
            "evidence sample_id must be non-empty",
            code="invalid_evidence",
            path=str(path) if path else None,
        )
    if not sample.tool_name or not sample.run_id:
        raise PolicyGenerationEvidenceError(
            "evidence tool_name and run_id must be non-empty",
            code="invalid_evidence",
            path=str(path) if path else None,
        )
    if len(sample.trace_digest) != 64 or any(
        char not in "0123456789abcdef" for char in sample.trace_digest.lower()
    ):
        raise PolicyGenerationEvidenceError(
            "evidence trace_digest must be 64 hex characters",
            code="invalid_evidence",
            path=str(path) if path else None,
        )
    if sample.line_number < 1:
        raise PolicyGenerationEvidenceError(
            "evidence line_number must be positive",
            code="invalid_evidence",
            path=str(path) if path else None,
        )
    if sample.human_decision not in {"approve", "deny"}:
        raise PolicyGenerationEvidenceError(
            f"evidence human_decision must be approve|deny, got "
            f"{sample.human_decision!r}",
            code="invalid_evidence",
            path=str(path) if path else None,
        )
    if not isinstance(sample.arguments, dict):
        raise PolicyGenerationEvidenceError(
            "evidence arguments must be an object",
            code="invalid_evidence",
            path=str(path) if path else None,
        )
    try:
        json.dumps(sample.arguments, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PolicyGenerationEvidenceError(
            f"evidence arguments must be strictly JSON-safe: {exc}",
            code="invalid_evidence",
            path=str(path) if path else None,
        ) from exc


def _find_request_by_line(
    requests: dict[str, _Request],
    line_number: int,
) -> _Request | None:
    for request in requests.values():
        if request.line_number == line_number:
            return request
    return None


# ---------------------------------------------------------------------------
# Deterministic balanced selection
# ---------------------------------------------------------------------------


def _select_balanced(
    samples: list[PolicyGenerationEvidenceSample],
    max_evidence: int,
) -> list[PolicyGenerationEvidenceSample]:
    """Deterministic selection: balance approve/deny, maximize distinct Runs
    within each decision group, then chronological/reference order."""
    if len(samples) <= max_evidence:
        return list(samples)

    approves = [s for s in samples if s.human_decision == "approve"]
    denies = [s for s in samples if s.human_decision == "deny"]

    # Within each decision group, prefer distinct Runs before reusing a Run.
    grouped_approves = _group_by_distinct_run(approves)
    grouped_denies = _group_by_distinct_run(denies)

    selected: list[PolicyGenerationEvidenceSample] = []
    a_i = d_i = 0
    while len(selected) < max_evidence and (
        a_i < len(grouped_approves) or d_i < len(grouped_denies)
    ):
        if d_i < len(grouped_denies):
            selected.append(grouped_denies[d_i])
            d_i += 1
        if len(selected) < max_evidence and a_i < len(grouped_approves):
            selected.append(grouped_approves[a_i])
            a_i += 1

    # Fill remaining slots from the leftover groups in reference order.
    leftover = grouped_approves[a_i:] + grouped_denies[d_i:]
    for sample in leftover:
        if len(selected) >= max_evidence:
            break
        selected.append(sample)

    # Final: keep reference order for determinism.
    ordered_keys = {id(s): i for i, s in enumerate(samples)}
    return sorted(selected, key=lambda s: ordered_keys[id(s)])


def _group_by_distinct_run(
    samples: list[PolicyGenerationEvidenceSample],
) -> list[PolicyGenerationEvidenceSample]:
    """Return one sample per distinct Run first (reference order), then the
    remaining samples in reference order.  Later evidence from a new Run is
    preferred over an earlier sample from an already represented Run."""
    first_per_run: list[PolicyGenerationEvidenceSample] = []
    seen_runs: set[str] = set()
    repeats: list[PolicyGenerationEvidenceSample] = []
    for sample in samples:
        if sample.run_id in seen_runs:
            repeats.append(sample)
        else:
            seen_runs.add(sample.run_id)
            first_per_run.append(sample)
    return first_per_run + repeats


# ---------------------------------------------------------------------------
# Byte budget
# ---------------------------------------------------------------------------


def evidence_byte_size(samples: Iterable[PolicyGenerationEvidenceSample]) -> int:
    """Full UTF-8 byte size of the canonical JSON evidence (never truncates)."""
    payload = [
        {
            "sample_id": s.sample_id,
            "trace_digest": s.trace_digest,
            "line_number": s.line_number,
            "run_id": s.run_id,
            "human_decision": s.human_decision,
            "tool_name": s.tool_name,
            "arguments": s.arguments,
        }
        for s in samples
    ]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return len(canonical.encode("utf-8"))


def check_evidence_byte_budget(
    samples: Iterable[PolicyGenerationEvidenceSample],
    settings: PolicyGenerationSettings,
) -> None:
    """Fail when the selected evidence exceeds the configured byte budget."""
    size = evidence_byte_size(samples)
    if size > settings.max_evidence_bytes:
        raise PolicyGenerationEvidenceError(
            f"selected evidence exceeds byte budget: {size} > "
            f"{settings.max_evidence_bytes}",
            code="evidence_too_large",
        )


# ---------------------------------------------------------------------------
# Shared parsing helpers (Discovery-compatible)
# ---------------------------------------------------------------------------


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_record(line: str) -> dict[str, Any]:
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        raise PolicyGenerationEvidenceError(
            f"invalid JSONL record: {exc}",
            code="invalid_trace",
        ) from exc
    if not isinstance(record, dict):
        raise PolicyGenerationEvidenceError(
            "Trace record is not an object",
            code="invalid_trace",
        )
    return record


def _argument_shape_digest(arguments: dict[str, Any]) -> str:
    shape = _argument_shape(arguments)
    payload = json.dumps(shape, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
        shapes = {
            json.dumps(_argument_shape(item), sort_keys=True, separators=(",", ":"))
            for item in value
        }
        return {"array": [json.loads(item) for item in sorted(shapes)]}
    if isinstance(value, dict):
        return {
            "object": {
                str(key): _argument_shape(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        }
    return "unknown"


class _Evaluation:
    __slots__ = (
        "run_id",
        "tool_call_id",
        "tool_name",
        "policy_names",
        "risk_level",
        "arguments",
        "line_number",
    )

    def __init__(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        policy_names: tuple[str, ...],
        risk_level: str,
        arguments: dict[str, Any],
    ) -> None:
        self.run_id = run_id
        self.tool_call_id = tool_call_id
        self.tool_name = tool_name
        self.policy_names = policy_names
        self.risk_level = risk_level
        self.arguments = arguments
        self.line_number = 0


class _Request:
    __slots__ = (
        "request_id",
        "run_id",
        "tool_call_id",
        "line_number",
        "arguments",
        "tool_name",
    )

    def __init__(
        self,
        *,
        request_id: str,
        run_id: str,
        tool_call_id: str,
        line_number: int,
        arguments: dict[str, Any],
        tool_name: str,
    ) -> None:
        self.request_id = request_id
        self.run_id = run_id
        self.tool_call_id = tool_call_id
        self.line_number = line_number
        self.arguments = arguments
        self.tool_name = tool_name


class _Response:
    __slots__ = ("request_id", "decision", "automatic", "run_id")

    def __init__(
        self,
        *,
        request_id: str,
        decision: str,
        automatic: bool,
        run_id: str,
    ) -> None:
        self.request_id = request_id
        self.decision = decision
        self.automatic = automatic
        self.run_id = run_id


_SUPPORTED_SCHEMAS = {1, 2}


def _parse_evaluation(run_id: str, data: dict[str, Any]) -> _Evaluation | None:
    if data.get("hook") != "before_tool_call":
        return None
    # A before_tool_call Policy Evaluation with malformed fields is a
    # relevant-event defect — reject it, never silently ignore it.
    input_data = data.get("input")
    if not isinstance(input_data, dict):
        raise PolicyGenerationEvidenceError(
            "policy_evaluation.input must be an object",
            code="invalid_trace",
        )
    tool_call = input_data.get("tool_call")
    if not isinstance(tool_call, dict):
        raise PolicyGenerationEvidenceError(
            "policy_evaluation.input.tool_call must be an object",
            code="invalid_trace",
        )
    tool_call_id = tool_call.get("id")
    tool_name = tool_call.get("name")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise PolicyGenerationEvidenceError(
            "policy_evaluation tool_call id must be a non-empty string",
            code="invalid_trace",
        )
    if not isinstance(tool_name, str) or not tool_name:
        raise PolicyGenerationEvidenceError(
            "policy_evaluation tool_call name must be a non-empty string",
            code="invalid_trace",
        )
    arguments = tool_call.get("arguments")
    if not isinstance(arguments, dict):
        raise PolicyGenerationEvidenceError(
            "policy_evaluation tool_call arguments must be an object",
            code="invalid_trace",
        )
    final = data.get("final")
    if not isinstance(final, dict) or final.get("action") != "require_confirmation":
        # An evaluation that does not require confirmation is not relevant
        # to generation evidence; skip it like Discovery does.
        return None
    risk_level = final.get("risk_level")
    if not isinstance(risk_level, str) or not risk_level:
        raise PolicyGenerationEvidenceError(
            "policy_evaluation.final.risk_level must be a non-empty string",
            code="invalid_trace",
        )
    decisions = data.get("decisions")
    names: set[str] = set()
    if isinstance(decisions, list):
        for item in decisions:
            if (
                isinstance(item, dict)
                and item.get("action") == "require_confirmation"
                and isinstance(item.get("policy_name"), str)
            ):
                names.add(item["policy_name"])
    return _Evaluation(
        run_id=run_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        policy_names=tuple(sorted(names)),
        risk_level=risk_level,
        arguments=arguments,
    )


def _parse_request(run_id: str, data: dict[str, Any]) -> _Request | None:
    request = data.get("request")
    if not isinstance(request, dict):
        return None
    request_id = request.get("id")
    if not isinstance(request_id, str):
        return None
    hook = request.get("hook")
    if hook != "before_tool_call":
        return None
    arguments = request.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    tool_call = request.get("tool_call")
    tool_call_id = ""
    tool_name = ""
    if isinstance(tool_call, dict):
        candidate_id = tool_call.get("id")
        candidate_name = tool_call.get("name")
        if isinstance(candidate_id, str):
            tool_call_id = candidate_id
        if isinstance(candidate_name, str):
            tool_name = candidate_name
    return _Request(
        request_id=request_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        line_number=0,
        arguments=arguments,
        tool_name=tool_name,
    )


def _parse_response(run_id: str, data: dict[str, Any]) -> _Response | None:
    response = data.get("response")
    if not isinstance(response, dict):
        return None
    request_id = response.get("request_id")
    decision = response.get("decision")
    if not isinstance(request_id, str) or not isinstance(decision, str):
        return None
    metadata = response.get("metadata")
    automatic = False
    if isinstance(metadata, dict) and "automatic" in metadata:
        if not isinstance(metadata["automatic"], bool):
            raise PolicyGenerationEvidenceError(
                "confirmation response automatic flag must be a boolean",
                code="invalid_trace",
            )
        automatic = metadata["automatic"]
    return _Response(
        request_id=request_id,
        decision=decision,
        automatic=automatic,
        run_id=run_id,
    )


def load_discovery_report(path: str | Path) -> PolicyDiscoveryReport:
    """Load and revalidate a stored Discovery report."""
    resolved = Path(path).expanduser().resolve()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyDiscoveryError(f"could not load Discovery report: {exc}") from exc
    return policy_discovery_report_from_dict(payload)


__all__ = [
    "PolicyGenerationEvidenceError",
    "check_evidence_byte_budget",
    "evidence_byte_size",
    "load_discovery_report",
    "load_policy_generation_evidence",
    "resolve_policy_opportunity",
]
