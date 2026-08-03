"""CLI entrypoint for governed Policy candidate generation.

``evopi policy generate`` is the user-confirmed bridge between an immutable
Opportunity report and an inactive generated candidate.  Generation never
reviews, approves, activates, reloads, registers, or executes the candidate.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from evopi.ai import (
    ModelRoute,
)
from evopi.cli.runtime import build_model_runtime
from evopi.core.model import Model
from evopi.core.model_errors import ModelRetryConfig
from evopi.evolution import (
    EvolutionStoreLockError,
    PolicyDiscoveryError,
    PolicyDiscoveryReport,
    PolicyGenerationError,
    PolicyGenerationProposal,
    PolicyGenerationRecord,
    PolicyGenerationResult,
    PolicyGenerationSettings,
    PolicyGenerationStore,
    PolicyOpportunityStore,
    PolicyCandidateGenerationService,
    check_evidence_byte_budget,
    load_discovery_report,
    load_policy_generation_evidence,
    resolve_evolution_home,
    resolve_policy_opportunity,
)
from evopi.evolution.policy_generation_protocol import (
    PolicyGenerationConfirmation,
)

ModelBuilder: Callable[[object], tuple[Model, ModelRoute | None]]


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_policy_generate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evopi policy generate",
        description=(
            "Generate an inactive Policy candidate from an Opportunity report "
            "and explicit Trace evidence, with human confirmation."
        ),
    )
    parser.add_argument("report", metavar="REPORT", help="Stored 32-char report ID or JSON path")
    parser.add_argument(
        "--opportunity",
        required=True,
        metavar="SIGNATURE_PREFIX",
        help="Unique Opportunity semantic-signature prefix (>= 8 hex chars)",
    )
    parser.add_argument(
        "--trace",
        action="append",
        required=True,
        metavar="TRACE",
        help="Trace JSONL file or directory (repeatable; consent for model transmission)",
    )
    parser.add_argument("--intent", metavar="TEXT", help="Optional user intent for the Proposal")
    parser.add_argument("--name", metavar="NAME", help="Hard candidate name constraint")
    parser.add_argument("--path", metavar="PATH", help="Target candidate directory")
    parser.add_argument(
        "--workspace",
        type=Path,
        metavar="PATH",
        help="Workspace for default candidate target resolution",
    )
    parser.add_argument("--max-evidence", type=_positive_int, default=12, metavar="N")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Explicit preauthorization for scripts (no interactive confirmation)",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")

    # Model / failover / retry options (mirror the product runtime)
    parser.add_argument("--provider", help="Provider name")
    parser.add_argument("--model", help="Model name")
    parser.add_argument("--fallback", action="append", metavar="P:M")
    parser.add_argument("--no-failover", action="store_true")
    parser.add_argument("--max-retries", type=_non_negative_int, default=3)
    parser.add_argument("--no-retry", action="store_true")
    parser.add_argument("--model-timeout", type=_positive_float, default=120.0)
    parser.add_argument("--max-output-tokens", type=_positive_int, default=4096)
    parser.add_argument("--context-window", type=int, default=0)
    parser.add_argument("--circuit-failure-threshold", type=_positive_int, default=2)
    parser.add_argument("--circuit-recovery-timeout", type=_positive_float, default=30.0)
    parser.add_argument("--generation-timeout", type=_positive_float, default=120.0)
    return parser


def policy_generate_main(argv: Sequence[str]) -> int:
    args = build_policy_generate_parser().parse_args(list(argv))
    try:
        return _run_generation(args)
    except (OSError, ValueError, PolicyGenerationError, PolicyDiscoveryError,
            EvolutionStoreLockError) as exc:
        print(f"EvoPi policy generate error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nEvoPi policy generation aborted.", file=sys.stderr)
        return 130


def _run_generation(args: argparse.Namespace) -> int:
    import asyncio

    # --- load and revalidate the Discovery report -------------------------
    report = _load_report(args.report)
    opportunity = resolve_policy_opportunity(report, args.opportunity)

    # --- reconstruct evidence (explicit --trace consent) -------------------
    print(
        "EvoPi policy generate: Trace arguments selected below will be sent "
        "to the configured model provider for evidence-grounded generation.",
        file=sys.stderr,
    )
    settings = PolicyGenerationSettings(
        max_evidence=args.max_evidence,
        stage_timeout=args.generation_timeout,
    )
    evidence = load_policy_generation_evidence(
        report,
        opportunity,
        args.trace,
        settings,
    )
    check_evidence_byte_budget(evidence, settings)
    evidence_digest = _evidence_digest_for(evidence)
    # One attempt ID for the entire attempt (all post-evidence outcomes).
    generation_id = _new_generation_id()

    # --- model route (test-injectable) --------------------------------------
    model, model_route = _build_model_route(args)

    service = PolicyCandidateGenerationService(
        model,
        model_route=model_route,
        retry_config=ModelRetryConfig(
            enabled=not args.no_retry,
            max_retries=args.max_retries,
        ),
        settings=settings,
    )

    # --- stage 1: proposal ---------------------------------------------------
    try:
        proposal = asyncio.run(
            service.propose(
                report,
                opportunity,
                evidence,
                intent=args.intent,
                name=args.name,
            )
        )
    except PolicyGenerationError as exc:
        return _record_and_fail(
            exc,
            report=report,
            opportunity=opportunity,
            confirmation="none",
            generation_id=generation_id,
            evidence_digest=evidence_digest,
            model_runs=service.model_runs,
            message="model Proposal failed",
        )
    model_runs = service.model_runs

    # --- render proposal to stderr (redacted) --------------------------------
    _render_proposal(proposal, opportunity=opportunity)

    # --- defer is a first-class successful outcome ---------------------------
    if proposal.strategy == "defer":
        return _record_deferred(
            report=report,
            opportunity=opportunity,
            proposal=proposal,
            generation_id=generation_id,
            evidence_digest=evidence_digest,
            model_runs=model_runs,
            json_output=args.json_output,
        )

    # --- confirmation ---------------------------------------------------------
    confirmation = _resolve_confirmation(args)
    if confirmation in {"declined", "none"}:
        return _record_declined(
            report=report,
            opportunity=opportunity,
            proposal=proposal,
            generation_id=generation_id,
            evidence_digest=evidence_digest,
            model_runs=model_runs,
            json_output=args.json_output,
        )

    # --- stage 2: materialize (after confirmation) ----------------------------
    default_target = _default_target(args, proposal.candidate_name)
    target_path = Path(args.path or default_target).expanduser().resolve()
    target_pre_existed = target_path.exists()
    try:
        result = asyncio.run(
            service.materialize(
                proposal,
                report,
                opportunity,
                evidence,
                generation_id=generation_id,
                path=target_path,
            )
        )
    except PolicyGenerationError as exc:
        return _record_and_fail(
            exc,
            report=report,
            opportunity=opportunity,
            confirmation=confirmation,
            generation_id=generation_id,
            evidence_digest=evidence_digest,
            model_runs=service.model_runs,
            proposal=proposal,
            message="candidate materialization failed",
        )

    # --- store the immutable record ------------------------------------------
    try:
        record = _store_record(
            result.record,
            confirmation=confirmation,
            proposal=proposal,
        )
    except (PolicyGenerationError, EvolutionStoreLockError) as exc:
        # Record persistence failed: remove only the target created by this
        # operation and report failure; never delete a pre-existing path.
        if not target_pre_existed:
            cleanup_error = _cleanup_created_target(target_path)
            detail = f"; {cleanup_error}" if cleanup_error else ""
        else:
            detail = (
                f"; target existed before generation, left in place: "
                f"{target_path}"
            )
        return _fail(f"generation record persistence failed: {exc}{detail}")
    _render_success(record, result, proposal, args)

    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_report(spec: str) -> PolicyDiscoveryReport:
    """Load and revalidate a stored Discovery report by ID or path."""

    candidate = Path(spec).expanduser()
    if candidate.is_file():
        return load_discovery_report(candidate)
    return load_discovery_report(
        PolicyOpportunityStore(
            resolve_evolution_home() / "opportunities" / "policies"
        ).report_path(spec)
    )


def _build_model_route(args: argparse.Namespace) -> tuple[Model, ModelRoute | None]:
    if hasattr(args, "_model_builder") and args._model_builder is not None:
        return args._model_builder(args)  # type: ignore[return-value]
    primary, route, _configs = build_model_runtime(args)
    return primary, route


def _resolve_confirmation(args: argparse.Namespace) -> PolicyGenerationConfirmation:
    if args.yes:
        return "preauthorized"
    if not sys.stdin.isatty():
        return "declined"
    try:
        answer = input("Generate this Policy candidate? [y/N] ").strip().lower()
    except EOFError:
        return "declined"
    except KeyboardInterrupt:
        # Propagate to the outer 130 exit path.
        raise
    if answer in {"y", "yes"}:
        return "interactive"
    return "declined"


def _evidence_digest_for(evidence: Sequence[object]) -> str:
    """Stable digest over the selected evidence samples."""
    import hashlib

    payload = [
        {
            "sample_id": sample.sample_id,  # type: ignore[attr-defined]
            "trace_digest": sample.trace_digest,  # type: ignore[attr-defined]
            "line_number": sample.line_number,  # type: ignore[attr-defined]
            "run_id": sample.run_id,  # type: ignore[attr-defined]
            "human_decision": sample.human_decision,  # type: ignore[attr-defined]
            "tool_name": sample.tool_name,  # type: ignore[attr-defined]
            "arguments": sample.arguments,  # type: ignore[attr-defined]
        }
        for sample in evidence
    ]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _persist_record(record: PolicyGenerationRecord) -> None:
    """Persist an immutable record; storage failures are never swallowed."""
    PolicyGenerationStore(
        resolve_evolution_home() / "generations" / "policies"
    ).save(record)


def _record_declined(
    *,
    report: object,
    opportunity: object,
    proposal: PolicyGenerationProposal,
    generation_id: str,
    evidence_digest: str,
    model_runs: tuple[object, ...],
    json_output: bool,
) -> int:
    from datetime import UTC, datetime

    record = PolicyGenerationRecord(
        generation_id=generation_id,
        created_at=datetime.now(UTC),
        outcome="declined",
        report_id=report.report_id,  # type: ignore[attr-defined]
        report_digest=report.report_digest,  # type: ignore[attr-defined]
        semantic_signature=opportunity.semantic_signature,  # type: ignore[attr-defined]
        evidence_digest=evidence_digest,
        proposal=proposal,
        confirmation="declined",
        model_runs=tuple(model_runs),  # type: ignore[arg-type]
    )
    try:
        _persist_record(record)
    except (PolicyGenerationError, EvolutionStoreLockError) as exc:
        return _fail(f"declined record persistence failed: {exc}")
    if json_output:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "outcome": "declined",
                    "generation_id": generation_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        print(f"Policy generation declined (generation {generation_id[:8]}).")
    return 2


def _record_deferred(
    *,
    report: object,
    opportunity: object,
    proposal: PolicyGenerationProposal,
    generation_id: str,
    evidence_digest: str,
    model_runs: tuple[object, ...],
    json_output: bool,
) -> int:
    from datetime import UTC, datetime

    record = PolicyGenerationRecord(
        generation_id=generation_id,
        created_at=datetime.now(UTC),
        outcome="deferred",
        report_id=report.report_id,  # type: ignore[attr-defined]
        report_digest=report.report_digest,  # type: ignore[attr-defined]
        semantic_signature=opportunity.semantic_signature,  # type: ignore[attr-defined]
        evidence_digest=evidence_digest,
        proposal=proposal,
        confirmation="none",
        model_runs=tuple(model_runs),  # type: ignore[arg-type]
    )
    try:
        _persist_record(record)
    except (PolicyGenerationError, EvolutionStoreLockError) as exc:
        return _fail(f"deferred record persistence failed: {exc}")
    if json_output:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "outcome": "deferred",
                    "generation_id": generation_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        print(
            f"Policy generation deferred (generation {generation_id[:8]}); "
            "no candidate was created."
        )
    return 2


def _record_and_fail(
    exc: PolicyGenerationError,
    *,
    report: object,
    opportunity: object,
    confirmation: PolicyGenerationConfirmation,
    generation_id: str,
    evidence_digest: str,
    model_runs: tuple[object, ...],
    message: str,
    proposal: PolicyGenerationProposal | None = None,
) -> int:
    from datetime import UTC, datetime

    record = PolicyGenerationRecord(
        generation_id=generation_id,
        created_at=datetime.now(UTC),
        outcome="failed",
        report_id=report.report_id,  # type: ignore[attr-defined]
        report_digest=report.report_digest,  # type: ignore[attr-defined]
        semantic_signature=opportunity.semantic_signature,  # type: ignore[attr-defined]
        evidence_digest=evidence_digest,
        proposal=proposal,
        confirmation=confirmation,
        model_runs=tuple(model_runs),  # type: ignore[arg-type]
        error_code=exc.code,
        error_message=(exc.args[0][:200] if exc.args else ""),
    )
    try:
        _persist_record(record)
    except PolicyGenerationError as store_exc:
        return _fail(f"failed record persistence failed: {store_exc}")
    return _fail(f"{message}: {exc}")


def _cleanup_created_target(path: str | Path) -> str | None:
    """Remove only the candidate target created by this operation.

    Never deletes a pre-existing path; the caller only invokes this after
    a materialization that created the target.  Returns an error message
    when cleanup fails, so the failure is surfaced for safe manual recovery.
    """
    target = Path(path).expanduser().resolve()
    if not target.exists():
        return None
    if not target.is_dir():
        try:
            target.unlink()
        except OSError as exc:
            return f"could not remove candidate file {target}: {exc}"
        return None
    import shutil

    try:
        shutil.rmtree(target)
    except OSError as exc:
        return (
            f"could not remove candidate directory {target}: {exc}; "
            f"remove it manually before retrying"
        )
    return None


def _store_record(
    record: PolicyGenerationRecord,
    *,
    confirmation: PolicyGenerationConfirmation,
    proposal: PolicyGenerationProposal,
) -> PolicyGenerationRecord:
    from dataclasses import replace

    stored = replace(
        record,
        confirmation=confirmation,
        proposal=proposal,
    )
    return PolicyGenerationStore(
        resolve_evolution_home() / "generations" / "policies"
    ).save(stored)


def _render_proposal(
    proposal: PolicyGenerationProposal,
    *,
    opportunity: object | None = None,
) -> None:
    from collections import Counter

    counts = Counter(d.action for d in proposal.sample_decisions)
    action_summary = ", ".join(
        f"{action}={count}" for action, count in sorted(counts.items())
    ) or "none"
    lines = [
        "Proposed Policy candidate:",
        f"  strategy: {proposal.strategy}",
        f"  name: {proposal.candidate_name or '(defer)'}",
        f"  description: {proposal.description}",
        f"  match_summary: {proposal.match_summary}",
        f"  rationale: {proposal.rationale}",
        f"  fallback_action: {proposal.fallback_action}",
        f"  per-action sample decisions: {action_summary}",
    ]
    if opportunity is not None:
        risk = getattr(opportunity, "risk_level", None)
        if risk:
            lines.append(f"  opportunity risk: {risk}")
    if proposal.replacement_target:
        lines.append(f"  replacement_target: {proposal.replacement_target}")
    if proposal.warnings:
        lines.append("  warnings: " + "; ".join(proposal.warnings))
    print("\n".join(lines), file=sys.stderr)


def _render_success(
    record: PolicyGenerationRecord,
    result: PolicyGenerationResult,
    proposal: PolicyGenerationProposal,
    args: argparse.Namespace,
) -> None:
    candidate_path = str(result.candidate)
    if args.json_output:
        payload = {
            "schema_version": 1,
            "generation_id": record.generation_id,
            "outcome": record.outcome,
            "candidate_name": record.candidate_name,
            "candidate_digest": record.candidate_digest,
            "strategy": proposal.strategy,
            "path": candidate_path,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        lines = [
            f"Policy candidate generated: {proposal.candidate_name}",
            f"  Generation ID: {record.generation_id}",
            f"  Candidate path: {candidate_path}",
            "",
            "Next steps (inactive lifecycle):",
            f'  evopi policy review "{candidate_path}" --trace "<trace>"',
            "  evopi policy approve <REVIEW_ID>",
            "  evopi policy activate <APPROVAL_ID>",
            "  then runtime /reload",
        ]
        if proposal.strategy == "replacement" and proposal.replacement_target:
            lines.append(
                "  Activation additionally requires: "
                f"evopi policy activate <APPROVAL_ID> "
                f"--replace {proposal.replacement_target} "
                "--expected-digest <digest>"
            )
        print("\n".join(lines))


def _fail(message: str) -> int:
    print(f"EvoPi policy generate error: {message}", file=sys.stderr)
    return 1


def _default_target(args: argparse.Namespace, candidate_name: str) -> Path:
    workspace = getattr(args, "workspace", None) or Path.cwd()
    return Path(workspace).resolve() / ".evopi" / "policy-candidates" / candidate_name


def _new_generation_id() -> str:
    import uuid

    return uuid.uuid4().hex


__all__ = [
    "build_policy_generate_parser",
    "policy_generate_main",
]
