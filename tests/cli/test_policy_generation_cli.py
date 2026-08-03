"""CLI tests for ``evopi policy generate``.

All model stages use scripted fake models — no real Provider, network,
credentials, approval, activation, reload, commit, or push occurs.
"""

from __future__ import annotations

import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import pytest

from evopi.cli import policy_generation as gen
from evopi.cli.main import main
from evopi.core.messages import AssistantMessage
from evopi.core.stream import ModelComplete



def _make_trace_and_report(tmp_path: Path) -> tuple[Path, Path]:
    """Create a Trace and a stored Discovery report; return (trace, report)."""
    from datetime import UTC, datetime

    from evopi.evolution import (
        PolicyOpportunityStore,
        discover_policy_opportunities,
    )
    from tests.evolution.test_policy_pattern_discovery import (
        _confirmation_records,
        _write_trace,
    )

    trace = tmp_path / "trace.jsonl"
    records: list[dict[str, object]] = []
    for index in range(1, 4):
        records.extend(
            _confirmation_records(
                run_id=f"run-{index}",
                index=index,
                decision="deny",
                command=f"risky-command-{index}",
                created_at=datetime(2026, 1, index, tzinfo=UTC),
            )
        )
    _write_trace(trace, records)
    report = discover_policy_opportunities([trace])
    store = PolicyOpportunityStore(tmp_path / "home" / "opportunities" / "policies")
    stored = store.save(report)
    return trace, store.report_path(stored.report_id)


class _ScriptedModel:
    """One model that serves Proposal then Candidate stages."""

    name = "scripted"
    provider = "test"

    def __init__(self, proposal_payload: dict, candidate_payload: dict) -> None:
        self._proposal = proposal_payload
        self._candidate = candidate_payload
        self.calls: list[str] = []

    def stream(self, context):
        from evopi.core.messages import SystemMessage, UserMessage

        async def _stream():
            stage = "proposal"
            contract: dict = {}
            for message in context.messages:
                if isinstance(message, UserMessage) and "PROPOSAL" in message.content:
                    stage = "candidate"
                if isinstance(message, SystemMessage):
                    contract.update(_extract_contract(message.content))
            self.calls.append(stage)
            if stage == "candidate":
                payload = _candidate_payload(contract)
            else:
                payload = self._proposal
            yield ModelComplete(
                message=AssistantMessage(
                    content=json.dumps(payload),
                    stop_reason="stop",
                ),
            )

        return _stream()


def _inject_model(monkeypatch: pytest.MonkeyPatch, model: _ScriptedModel) -> None:
    def fake_builder(args):
        return model, None

    monkeypatch.setattr(gen, "_build_model_route", fake_builder)


def _proposal_payload() -> dict:
    return {
        "schema_version": 1,
        "strategy": "additive",
        "candidate_name": "block_risky_command",
        "description": "Block risky commands",
        "match_summary": "3/3 match",
        "rationale": "Users denied risky commands",
        "fallback_action": "allow",
        "replacement_target": None,
        "sample_decisions": [
            {"sample_id": f"sample-{i}", "action": "block"}
            for i in range(1, 4)
        ],
        "warnings": [],
    }


def _real_sample_ids(report_path: Path) -> list[str]:
    """Read the stored report and return the actual sample IDs.

    Evidence sample IDs are ``<digest8>:<line>`` strings derived from the
    Opportunity evidence references.
    """
    payload = json.loads(Path(report_path).read_text(encoding="utf-8"))
    opportunity = payload["opportunities"][0]
    return [
        f"{e['trace_digest'][:8]}:{e['line_number']}"
        for e in opportunity["evidence"]
    ]


def _real_signature(report_path: Path) -> str:
    payload = json.loads(Path(report_path).read_text(encoding="utf-8"))
    return payload["opportunities"][0]["semantic_signature"]


def _candidate_payload(
    metadata: dict | None = None,
    *,
    description: str = "Block risky commands",
) -> dict:
    metadata_literal = _metadata_literal(metadata or {})
    return {
        "schema_version": 1,
        "files": [
            {
                "path": "policy.py",
                "content": (
                    "from __future__ import annotations\n"
                    "from evopi.policy.decisions import PolicyDecision\n"
                    "from evopi.policy.types import PolicyContext\n\n"
                    "class GeneratedPolicy:\n"
                    "    name = 'block_risky_command'\n"
                    "    version = '0.1.0'\n"
                    f"    description = {json.dumps(description)}\n"
                    "    hooks = ('before_tool_call',)\n"
                    "    priority = 100\n"
                    "    enabled = True\n"
                    "    source = 'generated'\n"
                    "    risk_level = 'medium'\n"
                    f"    metadata = {metadata_literal}\n\n"
                    "    def run(self, context: PolicyContext) -> PolicyDecision:\n"
                    "        return PolicyDecision(action='block')\n\n"
                    "POLICY = GeneratedPolicy()\n"
                ),
            }
        ],
    }


def _metadata_literal(metadata: dict) -> str:
    """Render a dict as a Python literal (JSON null is invalid Python)."""
    if not metadata:
        return "{}"
    entries = []
    for key, value in metadata.items():
        if value is None:
            rendered = "None"
        elif isinstance(value, bool):
            rendered = "True" if value else "False"
        elif isinstance(value, (int, float)):
            rendered = str(value)
        else:
            rendered = json.dumps(value, ensure_ascii=False)
        entries.append(f"{json.dumps(key)}: {rendered}")
    return "{" + ", ".join(entries) + "}"


_CONTRACT_KEYS = (
    "generation_id",
    "report_id",
    "report_digest",
    "semantic_signature",
    "proposal_digest",
    "strategy",
    "evidence_digest",
    "replacement_target",
)


def _extract_contract(prompt_text: str) -> dict:
    """Extract the Host identity contract values from the Candidate prompt.

    Mirrors what a real model reads from the system prompt: the contract is
    rendered as ``'key': 'value'`` pairs inside the instruction text.
    """
    import re

    result: dict = {}
    for key in _CONTRACT_KEYS:
        pattern = re.compile(
            re.escape(key) + r"': '([^']*)'"
        )
        match = pattern.search(prompt_text)
        if match:
            result[key] = match.group(1)
    if "replacement_target" in result and result["replacement_target"] == "None":
        result["replacement_target"] = None
    return result


def _run_generate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra_args: list[str] | None = None,
    model: _ScriptedModel | None = None,
) -> tuple[int, str, str]:
    monkeypatch.setenv("EVOPI_HOME", str(tmp_path / "home"))
    trace, report_path = _make_trace_and_report(tmp_path)
    if model is None:
        ids = _real_sample_ids(report_path)
        proposal = _proposal_payload()
        proposal["sample_decisions"] = [
            {"sample_id": sid, "action": "block"} for sid in ids
        ]
        model = _ScriptedModel(proposal, _candidate_payload())
    _inject_model(monkeypatch, model)
    base = [
        "policy", "generate",
        str(report_path),
        "--opportunity", _real_signature(report_path)[:12],
        "--trace", str(trace),
        "--yes",
        "--workspace", str(tmp_path),
    ]
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(base + (extra_args or []))
    return code, out.getvalue(), err.getvalue()


def test_generate_success_creates_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, err = _run_generate(tmp_path, monkeypatch)
    assert code == 0
    assert "generated" in out.lower()
    assert "block_risky_command" in out
    # Candidate directory materialized
    target = tmp_path / ".evopi" / "policy-candidates" / "block_risky_command"
    assert (target / "policy.py").is_file()
    assert (target / "evopi-policy.json").is_file()
    assert (target / "cases.py").is_file()
    assert (target / "README.md").is_file()
    # Record stored
    home = tmp_path / "home"
    records = list((home / "generations" / "policies" / "records").glob("*.json"))
    assert len(records) == 1
    payload = json.loads(records[0].read_text(encoding="utf-8"))
    assert payload["outcome"] == "generated"
    # No raw evidence values in stdout, stderr, or record
    for stream in (out, err, records[0].read_text(encoding="utf-8")):
        assert "risky-command" not in stream


def test_generate_declined_when_no_yes_non_tty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --yes and non-TTY stdin, generation declines with exit 2."""
    from evopi.cli import policy_generation as gen_module

    # Force non-TTY behavior
    def fake_no_tty(args):
        return "declined"

    monkeypatch.setattr(gen_module, "_resolve_confirmation", fake_no_tty)
    monkeypatch.setenv("EVOPI_HOME", str(tmp_path / "home"))
    trace, report_path = _make_trace_and_report(tmp_path)
    proposal = _proposal_payload()
    proposal["sample_decisions"] = [
        {"sample_id": sid, "action": "block"}
        for sid in _real_sample_ids(report_path)
    ]
    model = _ScriptedModel(proposal, _candidate_payload())
    _inject_model(monkeypatch, model)
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main([
            "policy", "generate",
            str(report_path),
            "--opportunity", _real_signature(report_path)[:12],
            "--trace", str(trace),
            "--workspace", str(tmp_path),
        ])
    assert code == 2
    assert "declined" in out.getvalue().lower()
    # No candidate directory
    target = tmp_path / ".evopi" / "policy-candidates" / "block_risky_command"
    assert not target.exists()
    # Declined record stored
    home = tmp_path / "home"
    records = list((home / "generations" / "policies" / "records").glob("*.json"))
    assert any("declined" in r.read_text(encoding="utf-8") for r in records)


def test_generate_json_output_excludes_raw_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, err = _run_generate(tmp_path, monkeypatch, extra_args=["--json"])
    assert code == 0
    payload = json.loads(out)
    assert payload["schema_version"] == 1
    assert payload["outcome"] == "generated"
    assert "arguments" not in payload
    assert "risky-command" not in out


def test_generate_error_exit_code_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenModel:
        name = "broken"
        provider = "test"

        def stream(self, context):
            async def _stream():
                yield ModelComplete(
                    message=AssistantMessage(
                        content="not json at all",
                        stop_reason="stop",
                    ),
                )

            return _stream()

    monkeypatch.setenv("EVOPI_HOME", str(tmp_path / "home"))
    trace, report_path = _make_trace_and_report(tmp_path)
    _inject_model(monkeypatch, _BrokenModel())
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main([
            "policy", "generate",
            str(report_path),
            "--opportunity", _real_signature(report_path)[:12],
            "--trace", str(trace),
            "--yes",
            "--workspace", str(tmp_path),
        ])
    assert code == 1
    assert "error" in err.getvalue().lower()


def test_generate_requires_trace_consent_flag(tmp_path: Path) -> None:
    _, report_path = _make_trace_and_report(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main([
            "policy", "generate",
            str(report_path),
            "--opportunity", _real_signature(report_path)[:12],
        ])
    assert exc.value.code == 2  # argparse error


def test_main_routes_policy_generate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Main dispatch reaches the generate command."""
    monkeypatch.setenv("EVOPI_HOME", str(tmp_path / "home"))
    trace, report_path = _make_trace_and_report(tmp_path)
    proposal = _proposal_payload()
    proposal["sample_decisions"] = [
        {"sample_id": sid, "action": "block"}
        for sid in _real_sample_ids(report_path)
    ]
    model = _ScriptedModel(proposal, _candidate_payload())
    _inject_model(monkeypatch, model)
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main([
            "policy", "generate",
            str(report_path),
            "--opportunity", _real_signature(report_path)[:12],
            "--trace", str(trace),
            "--yes",
            "--workspace", str(tmp_path),
        ])
    assert code == 0
    assert "generated" in out.getvalue().lower()


# ---------------------------------------------------------------------------
# Revision 2: defer, lifecycle commands, interrupt propagation
# ---------------------------------------------------------------------------


def _defer_payload() -> dict:
    return {
        "schema_version": 1,
        "strategy": "defer",
        "candidate_name": "",
        "description": "",
        "match_summary": "",
        "rationale": "not now",
        "fallback_action": "allow",
        "replacement_target": None,
        "sample_decisions": [],
        "warnings": [],
    }


def test_generate_defer_exits_2_no_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVOPI_HOME", str(tmp_path / "home"))
    trace, report_path = _make_trace_and_report(tmp_path)
    model = _ScriptedModel(_defer_payload(), _candidate_payload())
    _inject_model(monkeypatch, model)
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main([
            "policy", "generate",
            str(report_path),
            "--opportunity", _real_signature(report_path)[:12],
            "--trace", str(trace),
            "--workspace", str(tmp_path),
        ])
    assert code == 2
    assert "deferred" in out.getvalue().lower()
    # No candidate directory and no confirmation requested
    target = tmp_path / ".evopi" / "policy-candidates" / "block_risky_command"
    assert not target.exists()
    # Deferred record persisted with a real evidence digest
    home = tmp_path / "home"
    records = list((home / "generations" / "policies" / "records").glob("*.json"))
    assert len(records) == 1
    payload = json.loads(records[0].read_text(encoding="utf-8"))
    assert payload["outcome"] == "deferred"
    assert len(payload["evidence_digest"]) == 64


def test_generate_success_lifecycle_commands_are_real(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, err = _run_generate(tmp_path, monkeypatch)
    assert code == 0
    assert "evopi policy review" in out
    assert "--trace" in out
    assert "evopi policy approve <REVIEW_ID>" in out
    assert "evopi policy activate <APPROVAL_ID>" in out
    assert "/reload" in out
    # No invented --snapshot flag
    assert "--snapshot" not in out


def test_generate_replacement_lifecycle_shows_replace_requirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    monkeypatch.setenv("EVOPI_HOME", str(tmp_path / "home"))
    trace, report_path = _make_trace_and_report(tmp_path)
    ids = _real_sample_ids(report_path)
    # Build a replacement-style scripted model
    class _ReplacementModel:
        name = "replacement"
        provider = "test"

        def __init__(self) -> None:
            self.stage = "proposal"

        def stream(self, context):
            from evopi.core.messages import UserMessage

            async def _stream():
                for m in context.messages:
                    if isinstance(m, UserMessage) and "PROPOSAL" in m.content:
                        self.stage = "candidate"
                if self.stage == "proposal":
                    payload = {
                        "schema_version": 1,
                        "strategy": "replacement",
                        "candidate_name": "tool_confirmation",
                        "description": "Replace tool confirmation",
                        "match_summary": "3/3",
                        "rationale": "tighter control",
                        "fallback_action": "require_confirmation",
                        "replacement_target": "tool_confirmation",
                        "sample_decisions": [
                            {"sample_id": sid, "action": "require_confirmation"}
                            for sid in ids
                        ],
                        "warnings": [],
                    }
                else:
                    from evopi.core.messages import SystemMessage

                    contract: dict = {}
                    for m in context.messages:
                        if isinstance(m, SystemMessage):
                            contract.update(_extract_contract(m.content))
                    payload = _candidate_payload(
                        contract,
                        description="Replace tool confirmation",
                    )
                    payload["files"][0]["content"] = payload["files"][0]["content"].replace(
                        "block_risky_command", "tool_confirmation"
                    ).replace(
                        "risk_level = 'medium'",
                        "risk_level = 'high'",
                    ).replace(
                        "return PolicyDecision(action='block')",
                        "return PolicyDecision(action='require_confirmation')",
                    )
                yield ModelComplete(
                    message=AssistantMessage(content=json.dumps(payload), stop_reason="stop"),
                )

            return _stream()

    model = _ReplacementModel()
    _inject_model(monkeypatch, model)
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main([
            "policy", "generate",
            str(report_path),
            "--opportunity", _real_signature(report_path)[:12],
            "--trace", str(trace),
            "--yes",
            "--workspace", str(tmp_path),
        ])
    assert code == 0, f"exit {code} stderr={err.getvalue()!r}"
    out_text = out.getvalue()
    assert "--replace tool_confirmation" in out_text, f"out={out_text}"
    assert "--expected-digest" in out_text


def test_generate_keyboard_interrupt_propagates_130(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KeyboardInterrupt during confirmation propagates to the 130 exit path."""
    from evopi.cli import policy_generation as gen_module

    monkeypatch.setenv("EVOPI_HOME", str(tmp_path / "home"))
    trace, report_path = _make_trace_and_report(tmp_path)
    proposal = _proposal_payload()
    proposal["sample_decisions"] = [
        {"sample_id": sid, "action": "block"}
        for sid in _real_sample_ids(report_path)
    ]
    model = _ScriptedModel(proposal, _candidate_payload())
    _inject_model(monkeypatch, model)

    def fake_interrupt(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(gen_module, "_resolve_confirmation", fake_interrupt)
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main([
            "policy", "generate",
            str(report_path),
            "--opportunity", _real_signature(report_path)[:12],
            "--trace", str(trace),
            "--workspace", str(tmp_path),
        ])
    assert code == 130
    assert "aborted" in err.getvalue().lower()


# ---------------------------------------------------------------------------
# Revision 4: lock-failure cleanup and store failure exit (N)
# ---------------------------------------------------------------------------


def test_lock_failure_cleans_generated_target_and_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock failure on record persistence removes only the created target."""
    monkeypatch.setenv("EVOPI_HOME", str(tmp_path / "home"))
    trace, report_path = _make_trace_and_report(tmp_path)
    proposal = _proposal_payload()
    proposal["sample_decisions"] = [
        {"sample_id": sid, "action": "block"}
        for sid in _real_sample_ids(report_path)
    ]
    model = _ScriptedModel(proposal, _candidate_payload())
    _inject_model(monkeypatch, model)

    from evopi.cli import policy_generation as gen_module
    from evopi.evolution import EvolutionStoreLockError

    def fake_store(record, **kwargs):
        raise EvolutionStoreLockError("locked by another process")

    monkeypatch.setattr(gen_module, "_store_record", fake_store)
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main([
            "policy", "generate",
            str(report_path),
            "--opportunity", _real_signature(report_path)[:12],
            "--trace", str(trace),
            "--yes",
            "--workspace", str(tmp_path),
        ])
    assert code == 1
    assert "persistence failed" in err.getvalue().lower()
    # The target created by this operation was removed (no orphan).
    target = tmp_path / ".evopi" / "policy-candidates" / "block_risky_command"
    assert not target.exists()
