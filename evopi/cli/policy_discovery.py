"""CLI entrypoint for deterministic Policy Pattern Discovery."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from evopi.evolution import (
    EvolutionStoreLockError,
    PolicyDiscoveryError,
    PolicyDiscoveryReport,
    PolicyDiscoverySettings,
    PolicyOpportunityStore,
    discover_policy_opportunities,
    resolve_evolution_home,
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_policy_discover_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evopi policy discover",
        description="Discover recurring Policy governance opportunities from JSONL Trace evidence",
    )
    parser.add_argument(
        "traces",
        nargs="+",
        type=Path,
        metavar="TRACE",
        help="Trace JSONL file or directory containing conventional Trace filenames",
    )
    parser.add_argument(
        "--min-occurrences",
        type=_positive_int,
        default=3,
        metavar="N",
        help="Minimum matching human decisions (default: 3)",
    )
    parser.add_argument(
        "--min-runs",
        type=_positive_int,
        default=2,
        metavar="N",
        help="Minimum distinct Run IDs (default: 2)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Write the stable JSON report to stdout",
    )
    return parser


def policy_discover_main(argv: Sequence[str]) -> int:
    args = build_policy_discover_parser().parse_args(list(argv))
    try:
        report = discover_policy_opportunities(
            args.traces,
            settings=PolicyDiscoverySettings(
                min_occurrences=args.min_occurrences,
                min_runs=args.min_runs,
            ),
        )
        store = PolicyOpportunityStore(
            resolve_evolution_home() / "opportunities" / "policies"
        )
        stored = store.save(report)
    except (OSError, ValueError, PolicyDiscoveryError, EvolutionStoreLockError) as exc:
        print(f"EvoPi policy discover error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nEvoPi policy discovery aborted.", file=sys.stderr)
        return 130

    if args.json_output:
        print(
            json.dumps(
                stored.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        print(render_policy_discovery(stored))
    return 0


def render_policy_discovery(report: PolicyDiscoveryReport) -> str:
    lines = [
        "Policy opportunity discovery: complete",
        f"Report: {report.report_id}",
        (
            f"Evidence: {report.stats.trace_count} trace(s), "
            f"{report.stats.eligible_human_decisions} human decision(s)"
        ),
        (
            f"Excluded: automatic={report.stats.excluded_automatic}, "
            f"cancelled={report.stats.excluded_cancelled}, "
            f"other_hooks={report.stats.excluded_other_hooks}"
        ),
        f"Opportunities: {len(report.opportunities)}",
    ]
    for index, opportunity in enumerate(report.opportunities, start=1):
        policies = ", ".join(opportunity.policy_names) or "unknown"
        lines.append(
            f"  {index}. {opportunity.theme} "
            f"tool={opportunity.tool_name} policies={policies} "
            f"risk={opportunity.risk_level} occurrences={opportunity.occurrence_count} "
            f"runs={opportunity.run_count}"
        )
        for evidence in opportunity.evidence[:3]:
            lines.append(
                f"     trace={evidence.trace_digest[:12]} "
                f"line={evidence.line_number} run={evidence.run_id} "
                f"decision={evidence.decision}"
            )
        hidden = len(opportunity.evidence) - min(3, len(opportunity.evidence))
        hidden += opportunity.omitted_evidence_count
        if hidden:
            lines.append(f"     ... {hidden} additional evidence reference(s)")
    lines.extend(f"Warning: {warning}" for warning in report.warnings)
    lines.append(
        "This report is review evidence; it does not create or activate a Policy."
    )
    return "\n".join(lines)


__all__ = [
    "build_policy_discover_parser",
    "policy_discover_main",
    "render_policy_discovery",
]
