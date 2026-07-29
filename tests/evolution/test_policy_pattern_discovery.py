from __future__ import annotations

import json
import importlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evopi.evolution import (
    EvolutionFileLock,
    EvolutionStoreLockError,
    PolicyDiscoveryError,
    PolicyDiscoverySettings,
    PolicyOpportunityStore,
    discover_policy_opportunities,
)


def _decision(policy_name: str, risk_level: str) -> dict[str, object]:
    return {
        "action": "require_confirmation",
        "reason": "test confirmation",
        "risk_level": risk_level,
        "rewritten_args": None,
        "replacement_result": None,
        "metadata": {},
        "policy_name": policy_name,
    }


def _confirmation_records(
    *,
    run_id: str,
    index: int,
    decision: str,
    command: str,
    created_at: datetime,
    policy_name: str = "tool_confirmation",
    risk_level: str = "medium",
    automatic: bool = False,
) -> list[dict[str, object]]:
    call_id = f"call-{index}"
    request_id = f"request-{index}"
    tool_call = {
        "id": call_id,
        "name": "shell_command",
        "arguments": {"command": command},
    }
    policy_decision = _decision(policy_name, risk_level)
    timestamp = created_at.isoformat()
    return [
        {
            "schema_version": 2,
            "type": "policy_evaluation",
            "run_id": run_id,
            "created_at": timestamp,
            "data": {
                "hook": "before_tool_call",
                "input": {
                    "tool_call": tool_call,
                    "arguments": {"command": command},
                },
                "final": policy_decision,
                "decisions": [policy_decision],
            },
        },
        {
            "schema_version": 2,
            "type": "confirmation_request",
            "run_id": run_id,
            "created_at": timestamp,
            "data": {
                "request": {
                    "id": request_id,
                    "hook": "before_tool_call",
                    "reason": "test confirmation",
                    "risk_level": risk_level,
                    "policy_names": [policy_name],
                    "tool_call": tool_call,
                    "arguments": {"command": command},
                    "metadata": {},
                }
            },
        },
        {
            "schema_version": 2,
            "type": "confirmation_response",
            "run_id": run_id,
            "created_at": timestamp,
            "data": {
                "response": {
                    "request_id": request_id,
                    "decision": decision,
                    "reason": "test response",
                    "metadata": {"automatic": automatic} if automatic else {},
                }
            },
        },
    ]


def _write_trace(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def test_repeated_human_denials_form_one_value_free_semantic_opportunity(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    records: list[dict[str, object]] = []
    records.extend(
        _confirmation_records(
            run_id="run-one",
            index=1,
            decision="deny",
            command="secret-command-one",
            created_at=start,
        )
    )
    records.extend(
        _confirmation_records(
            run_id="run-two",
            index=2,
            decision="deny",
            command="secret-command-two",
            created_at=start + timedelta(minutes=1),
        )
    )
    records.extend(
        _confirmation_records(
            run_id="run-two",
            index=3,
            decision="deny",
            command="secret-command-three",
            created_at=start + timedelta(minutes=2),
        )
    )
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, records)

    report = discover_policy_opportunities([trace])

    assert report.stats.eligible_human_decisions == 3
    assert len(report.opportunities) == 1
    opportunity = report.opportunities[0]
    assert opportunity.theme == "repeated_denial"
    assert opportunity.tool_name == "shell_command"
    assert opportunity.policy_names == ("tool_confirmation",)
    assert opportunity.risk_level == "medium"
    assert opportunity.argument_fields == ("command",)
    assert opportunity.occurrence_count == 3
    assert opportunity.run_count == 2
    assert opportunity.approve_count == 0
    assert opportunity.deny_count == 3
    payload = report.to_dict()
    assert len(report.input_digest) == 64
    assert payload["input_digest"] == report.input_digest
    assert payload["opportunities"][0]["semantic_signature"] == (
        opportunity.semantic_signature
    )
    assert "opportunity_id" not in payload["opportunities"][0]
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "secret-command" not in serialized
    assert str(tmp_path) not in serialized


def test_directory_discovery_and_legacy_confirmation_pairs_share_one_cluster(
    tmp_path: Path,
) -> None:
    traces = tmp_path / "traces"
    nested = traces / "nested"
    nested.mkdir(parents=True)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first = _confirmation_records(
        run_id="run-one",
        index=1,
        decision="approve",
        command="first-value",
        created_at=start,
    )
    first.extend(
        _confirmation_records(
            run_id="run-one",
            index=2,
            decision="approve",
            command="second-value",
            created_at=start + timedelta(minutes=1),
        )
    )
    legacy = _confirmation_records(
        run_id="run-two",
        index=3,
        decision="approve",
        command="third-value",
        created_at=start + timedelta(minutes=2),
    )[1:]
    for record in legacy:
        record.pop("schema_version")
    _write_trace(traces / "trace.jsonl", first)
    _write_trace(nested / "worker.trace.jsonl", legacy)
    _write_trace(nested / "session.jsonl", [{"type": "not-a-trace", "data": {}}])

    report = discover_policy_opportunities([traces])

    assert report.stats.trace_count == 2
    assert report.stats.eligible_human_decisions == 3
    assert report.opportunities[0].theme == "repeated_approval"
    assert report.opportunities[0].run_count == 2


def test_non_human_and_non_tool_confirmations_are_diagnostic_only(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    records: list[dict[str, object]] = []
    for index, run_id in enumerate(("run-one", "run-two", "run-two"), start=1):
        records.extend(
            _confirmation_records(
                run_id=run_id,
                index=index,
                decision="approve",
                command=f"approved-{index}",
                created_at=start + timedelta(minutes=index),
            )
        )
    records.extend(
        _confirmation_records(
            run_id="run-three",
            index=4,
            decision="deny",
            command="automatic-denial",
            created_at=start + timedelta(minutes=4),
            automatic=True,
        )
    )
    records.extend(
        _confirmation_records(
            run_id="run-three",
            index=5,
            decision="cancelled",
            command="cancelled",
            created_at=start + timedelta(minutes=5),
        )
    )
    request_id = "merge-request"
    timestamp = (start + timedelta(minutes=6)).isoformat()
    records.extend(
        [
            {
                "schema_version": 2,
                "type": "confirmation_request",
                "run_id": "run-four",
                "created_at": timestamp,
                "data": {
                    "request": {
                        "id": request_id,
                        "hook": "before_session_merge",
                        "reason": "merge",
                        "risk_level": "medium",
                        "policy_names": ["merge_policy"],
                        "tool_call": None,
                        "arguments": None,
                        "metadata": {},
                    }
                },
            },
            {
                "schema_version": 2,
                "type": "confirmation_response",
                "run_id": "run-four",
                "created_at": timestamp,
                "data": {
                    "response": {
                        "request_id": request_id,
                        "decision": "approve",
                        "reason": "approved",
                        "metadata": {},
                    }
                },
            },
        ]
    )
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, records)

    report = discover_policy_opportunities([trace])

    assert report.stats.matched_confirmations == 6
    assert report.stats.eligible_human_decisions == 3
    assert report.stats.excluded_automatic == 1
    assert report.stats.excluded_cancelled == 1
    assert report.stats.excluded_other_hooks == 1
    assert report.opportunities[0].approve_count == 3
    assert len(report.warnings) == 3


def test_safety_theme_precedes_risk_and_frequency_in_stable_ranking(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    records: list[dict[str, object]] = []
    groups = [
        ("deny_policy", "low", ("deny", "deny", "deny")),
        ("mixed_policy", "critical", ("approve", "deny", "approve")),
        ("approve_policy", "critical", ("approve", "approve", "approve")),
    ]
    index = 0
    for policy_name, risk_level, decisions in groups:
        for offset, decision in enumerate(decisions):
            index += 1
            records.extend(
                _confirmation_records(
                    run_id=f"{policy_name}-run-{offset % 2}",
                    index=index,
                    decision=decision,
                    command=f"value-{index}",
                    created_at=start + timedelta(minutes=index),
                    policy_name=policy_name,
                    risk_level=risk_level,
                )
            )
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, records)

    report = discover_policy_opportunities([trace])

    assert [item.theme for item in report.opportunities] == [
        "repeated_denial",
        "mixed_decisions",
        "repeated_approval",
    ]


def test_configurable_threshold_and_evidence_cap_keep_full_counts(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    records: list[dict[str, object]] = []
    for index in range(1, 5):
        records.extend(
            _confirmation_records(
                run_id="one-run",
                index=index,
                decision="deny",
                command=f"value-{index}",
                created_at=start + timedelta(minutes=index),
            )
        )
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, records)

    default_report = discover_policy_opportunities([trace])
    configured_report = discover_policy_opportunities(
        [trace],
        settings=PolicyDiscoverySettings(
            min_occurrences=2,
            min_runs=1,
            max_evidence_refs=2,
        ),
    )

    assert default_report.opportunities == ()
    opportunity = configured_report.opportunities[0]
    assert opportunity.occurrence_count == 4
    assert len(opportunity.evidence) == 2
    assert opportunity.omitted_evidence_count == 2


def test_dangling_confirmation_evaluation_fails_with_source_line(
    tmp_path: Path,
) -> None:
    record = _confirmation_records(
        run_id="run-one",
        index=1,
        decision="deny",
        command="never-confirmed",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )[0]
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, [record])

    with pytest.raises(PolicyDiscoveryError) as captured:
        discover_policy_opportunities([trace])

    assert captured.value.path == trace.resolve()
    assert captured.value.line_number == 1
    assert "no confirmation request" in str(captured.value)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("{not-json}\n", "invalid JSONL"),
        (
            json.dumps(
                {
                    "schema_version": 3,
                    "type": "confirmation_request",
                    "run_id": "run",
                    "data": {},
                }
            )
            + "\n",
            "unsupported Trace schema",
        ),
    ],
)
def test_invalid_trace_fails_closed_with_line_number(
    tmp_path: Path,
    payload: str,
    message: str,
) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_text(payload, encoding="utf-8")

    with pytest.raises(PolicyDiscoveryError, match=message) as captured:
        discover_policy_opportunities([trace])

    assert captured.value.line_number == 1


def test_opportunity_store_is_immutable_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "trace.jsonl"
    records: list[dict[str, object]] = []
    for index, run_id in enumerate(("run-one", "run-two", "run-two"), start=1):
        records.extend(
            _confirmation_records(
                run_id=run_id,
                index=index,
                decision="deny",
                command=f"value-{index}",
                created_at=datetime(2026, 1, index, tzinfo=UTC),
            )
        )
    _write_trace(trace, records)
    store = PolicyOpportunityStore(tmp_path / "opportunities")

    saved = store.save(discover_policy_opportunities([trace]))

    assert saved.report_digest
    assert store.load(saved.report_id) == saved
    report_path = store.report_path(saved.report_id)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["stats"]["record_count"] += 1
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PolicyDiscoveryError, match="digest"):
        store.load(saved.report_id)


def test_opportunity_store_uses_a_nonblocking_cross_process_lock(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, [{"schema_version": 2, "type": "agent_start", "data": {}}])
    store = PolicyOpportunityStore(tmp_path / "opportunities")
    report = replace(
        discover_policy_opportunities([trace]),
        report_id="a" * 32,
    )

    with EvolutionFileLock(store.lock_path):
        with pytest.raises(EvolutionStoreLockError):
            store.save(report)

    assert store.report_path(report.report_id).exists() is False


def test_duplicate_paths_and_duplicate_trace_content_do_not_inflate_counts(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "trace.jsonl"
    copy = tmp_path / "copied.jsonl"
    records: list[dict[str, object]] = []
    for index, run_id in enumerate(("run-one", "run-two", "run-two"), start=1):
        records.extend(
            _confirmation_records(
                run_id=run_id,
                index=index,
                decision="approve",
                command=f"value-{index}",
                created_at=datetime(2026, 1, index, tzinfo=UTC),
            )
        )
    _write_trace(trace, records)
    copy.write_bytes(trace.read_bytes())

    report = discover_policy_opportunities([trace, trace, copy])

    assert report.stats.trace_count == 1
    assert report.stats.eligible_human_decisions == 3
    assert report.opportunities[0].occurrence_count == 3


def test_argument_shape_changes_create_separate_semantic_opportunities(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    records: list[dict[str, object]] = []
    index = 0
    for include_cwd in (False, True):
        for run_offset in (0, 1, 1):
            index += 1
            group = _confirmation_records(
                run_id=f"shape-{include_cwd}-{run_offset}",
                index=index,
                decision="deny",
                command=f"value-{index}",
                created_at=start + timedelta(minutes=index),
            )
            if include_cwd:
                evaluation = group[0]["data"]["input"]
                request = group[1]["data"]["request"]
                evaluation["arguments"]["cwd"] = "private-directory"
                request["arguments"]["cwd"] = "private-directory"
            records.extend(group)
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, records)

    report = discover_policy_opportunities([trace])

    assert len(report.opportunities) == 2
    assert {item.argument_fields for item in report.opportunities} == {
        ("command",),
        ("command", "cwd"),
    }
    assert "private-directory" not in json.dumps(report.to_dict())


def test_malformed_tool_call_arguments_fail_closed(
    tmp_path: Path,
) -> None:
    records = _confirmation_records(
        run_id="run-one",
        index=1,
        decision="deny",
        command="value",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    records[0]["data"]["input"]["tool_call"]["arguments"] = "not-an-object"
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, records)

    with pytest.raises(PolicyDiscoveryError, match="tool_call.arguments") as captured:
        discover_policy_opportunities([trace])

    assert captured.value.line_number == 1


def test_malformed_policy_decision_item_fails_closed(
    tmp_path: Path,
) -> None:
    records = _confirmation_records(
        run_id="run-one",
        index=1,
        decision="deny",
        command="value",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    records[0]["data"]["decisions"] = ["not-an-object"]
    records[1]["data"]["request"]["policy_names"] = []
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, records)

    with pytest.raises(PolicyDiscoveryError, match="decisions item") as captured:
        discover_policy_opportunities([trace])

    assert captured.value.line_number == 1


def test_opportunity_store_atomic_failure_leaves_no_partial_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_trace(trace, [{"schema_version": 2, "type": "agent_start", "data": {}}])
    store = PolicyOpportunityStore(tmp_path / "opportunities")
    report = discover_policy_opportunities([trace])
    module = importlib.import_module("evopi.evolution.policy_opportunity_store")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(module.os, "replace", fail_replace)

    with pytest.raises(PolicyDiscoveryError, match="could not persist"):
        store.save(report)

    assert store.report_path(report.report_id).exists() is False
    assert list((store.root / "reports").glob("*.tmp")) == []
