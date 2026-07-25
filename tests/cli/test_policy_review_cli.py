from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from importlib import import_module
from types import ModuleType

from evopi.cli import main as exported_main
from evopi.cli import policy_review as policy_review_cli
from evopi.cli.main import main
from evopi.core.context import AgentContext
from evopi.core.tool import ToolCall
from evopi.policy.decisions import PolicyAction, PolicyDecision
from evopi.policy.types import HookName, PolicyContext, RiskLevel

_MODULE_NAME = "evopi_test_policy_review_assets"
cli_main_module = import_module("evopi.cli.main")


@dataclass(slots=True)
class ReviewPolicy:
    name: str = "review_candidate"
    action: PolicyAction = "allow"
    version: str = "2"
    description: str = "CLI review candidate"
    hooks: tuple[HookName, ...] = ("before_tool_call",)
    priority: int = 10
    enabled: bool = True
    source: str = "project"
    risk_level: RiskLevel = "medium"
    metadata: dict = field(default_factory=dict)

    def run(self, context: PolicyContext) -> PolicyDecision:
        return PolicyDecision(action=self.action, reason="CLI candidate")


def install_asset_module(monkeypatch, *, action: PolicyAction = "allow") -> ModuleType:
    module = ModuleType(_MODULE_NAME)
    module.INSTANCE = ReviewPolicy(action=action)
    module.PolicyClass = ReviewPolicy
    module.policy_factory = lambda: ReviewPolicy(action=action)
    module.CASES = [
        PolicyContext(
            hook="before_tool_call",
            agent_context=AgentContext(),
            tool_call=ToolCall(
                id="case-call",
                name="shell_command",
                arguments={"command": "python -m pytest"},
            ),
            arguments={"command": "python -m pytest"},
        )
    ]
    module.case_factory = lambda: list(module.CASES)
    module.INVALID_CASES = [object()]
    monkeypatch.setitem(sys.modules, _MODULE_NAME, module)
    return module


def write_trace(path, *, action: PolicyAction = "allow") -> None:
    decision = {
        "action": action,
        "reason": "historical",
        "risk_level": "low",
        "rewritten_args": None,
        "replacement_result": None,
        "metadata": {},
        "policy_name": "review_candidate",
    }
    record = {
        "schema_version": 2,
        "type": "policy_evaluation",
        "run_id": "run",
        "data": {
            "hook": "before_tool_call",
            "input": {
                "tool_call": {
                    "id": "trace-call",
                    "name": "shell_command",
                    "arguments": {"command": "python -m pytest"},
                },
                "arguments": {"command": "python -m pytest"},
                "tool_result": None,
                "error": None,
                "aborted": False,
                "metadata": {},
            },
            "final": decision,
            "decisions": [decision],
        },
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def test_policy_and_case_references_support_instance_class_and_factory(
    monkeypatch,
) -> None:
    module = install_asset_module(monkeypatch)

    assert policy_review_cli.load_policy_reference(
        f"{_MODULE_NAME}:INSTANCE"
    ) is module.INSTANCE
    assert isinstance(
        policy_review_cli.load_policy_reference(f"{_MODULE_NAME}:PolicyClass"),
        ReviewPolicy,
    )
    assert isinstance(
        policy_review_cli.load_policy_reference(f"{_MODULE_NAME}:policy_factory"),
        ReviewPolicy,
    )
    assert len(
        policy_review_cli.load_dry_run_cases_reference(f"{_MODULE_NAME}:CASES")
    ) == 1
    assert len(
        policy_review_cli.load_dry_run_cases_reference(
            f"{_MODULE_NAME}:case_factory"
        )
    ) == 1


def test_cli_json_report_passes_with_complete_unchanged_evidence(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    install_asset_module(monkeypatch)
    trace_path = tmp_path / "trace.jsonl"
    write_trace(trace_path)

    exit_code = main(
        [
            "policy",
            "review",
            f"{_MODULE_NAME}:INSTANCE",
            "--dry-run-cases",
            f"{_MODULE_NAME}:CASES",
            "--trace",
            str(trace_path),
            "--json",
        ]
    )

    output = capsys.readouterr()
    report = json.loads(output.out)
    assert exit_code == 0
    assert report["status"] == "passed"
    assert [check["status"] for check in report["checks"]] == [
        "passed",
        "passed",
        "passed",
    ]
    assert output.err == ""


def test_cli_text_report_returns_review_required_for_changed_replay(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    install_asset_module(monkeypatch, action="block")
    trace_path = tmp_path / "trace.jsonl"
    write_trace(trace_path)

    exit_code = main(
        [
            "policy",
            "review",
            f"{_MODULE_NAME}:policy_factory",
            "--dry-run-cases",
            f"{_MODULE_NAME}:case_factory",
            "--trace",
            str(trace_path),
        ]
    )

    output = capsys.readouterr()
    assert exit_code == 2
    assert "Supervisor review: review_required" in output.out
    assert "trace_replay_changed" in output.out
    assert "not activation approval" in output.out


def test_cli_missing_evidence_returns_review_required(monkeypatch, capsys) -> None:
    install_asset_module(monkeypatch)

    exit_code = main(["policy", "review", f"{_MODULE_NAME}:INSTANCE", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert report["status"] == "review_required"
    assert {finding["code"] for finding in report["findings"]} == {
        "dry_run_missing",
        "trace_replay_missing",
    }


def test_cli_invalid_cases_and_trace_become_failed_reports(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    install_asset_module(monkeypatch)
    invalid_cases_exit = main(
        [
            "policy",
            "review",
            f"{_MODULE_NAME}:INSTANCE",
            "--dry-run-cases",
            f"{_MODULE_NAME}:INVALID_CASES",
            "--json",
        ]
    )
    invalid_cases = json.loads(capsys.readouterr().out)

    trace_path = tmp_path / "broken.jsonl"
    trace_path.write_text("{broken\n", encoding="utf-8")
    trace_exit = main(
        [
            "policy",
            "review",
            f"{_MODULE_NAME}:INSTANCE",
            "--dry-run-cases",
            f"{_MODULE_NAME}:CASES",
            "--trace",
            str(trace_path),
            "--json",
        ]
    )
    invalid_trace = json.loads(capsys.readouterr().out)

    assert invalid_cases_exit == 1
    assert invalid_cases["status"] == "failed"
    assert invalid_cases["checks"][1]["status"] == "failed"
    assert trace_exit == 1
    assert invalid_trace["status"] == "failed"
    assert invalid_trace["checks"][2]["status"] == "failed"


def test_cli_candidate_import_and_inapplicable_trace_are_command_errors(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    module = install_asset_module(monkeypatch)
    module.AFTER_TURN = ReviewPolicy(hooks=("after_turn",))
    trace_path = tmp_path / "trace.jsonl"
    write_trace(trace_path)

    import_exit = main(["policy", "review", "missing_module:policy"])
    import_output = capsys.readouterr()
    trace_exit = main(
        [
            "policy",
            "review",
            f"{_MODULE_NAME}:AFTER_TURN",
            "--trace",
            str(trace_path),
        ]
    )
    trace_output = capsys.readouterr()

    assert import_exit == 1
    assert "policy review error" in import_output.err
    assert trace_exit == 1
    assert "only valid" in trace_output.err


def test_subcommand_dispatch_preserves_legacy_prompt_path(monkeypatch) -> None:
    calls: list[object] = []

    class Parser:
        def parse_args(self, args):
            calls.append(args)
            return argparse.Namespace(prompt="policy", provider=None)

    async def fake_run(args):
        calls.append(args.prompt)
        return 0

    monkeypatch.setattr(cli_main_module, "build_parser", lambda: Parser())
    monkeypatch.setattr(cli_main_module, "_run_one_shot", fake_run)
    monkeypatch.setattr(
        cli_main_module,
        "policy_review_main",
        lambda args: calls.append("review") or 99,
    )

    assert main(["policy"]) == 0
    assert calls == [["policy"], "policy"]
    assert main(["policy", "review", "candidate:policy"]) == 99
    assert calls[-1] == "review"
    assert exported_main is main
