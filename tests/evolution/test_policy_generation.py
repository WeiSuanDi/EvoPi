"""Tests for Proposal validation, redaction, bundles, and materialization.

No real Provider or network is contacted: all model stages use scripted
fake models.  Generated Python is never imported or executed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from evopi.core.messages import AssistantMessage
from evopi.core.stream import ModelComplete
from evopi.evolution import (
    PolicyGenerationRuntimeError,
    PolicyGenerationProposal,
    PolicyGenerationSampleDecision,
    PolicyGenerationSettings,
    PolicyCandidateGenerationService,
)
from evopi.evolution.policy_discovery_protocol import (
    PolicyDiscoveryReport,
    PolicyDiscoverySettings,
    PolicyDiscoverySource,
    PolicyDiscoveryStats,
    PolicyOpportunity,
    PolicyOpportunityEvidence,
)
from evopi.evolution.policy_generation import (
    build_host_files,
    redact_proposal_text,
    validate_candidate_bundle,
    validate_proposal,
)
from evopi.evolution.policy_generation_protocol import (
    PolicyGenerationEvidenceSample,
    PolicyGenerationModelRun,
)


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
    """Extract Host identity contract values from the Candidate prompt."""
    import re

    result: dict = {}
    for key in _CONTRACT_KEYS:
        match = re.search(re.escape(key) + r"': '([^']*)'", prompt_text)
        if match:
            result[key] = match.group(1)
    if "replacement_target" in result and result["replacement_target"] == "None":
        result["replacement_target"] = None
    return result


def _candidate_payload(metadata: dict | None = None) -> dict:
    """Render a policy.py bundle that declares the extracted identity."""
    if metadata:
        entries = []
        for key, value in metadata.items():
            if value is None:
                rendered = "None"
            elif isinstance(value, bool):
                rendered = "True" if value else "False"
            else:
                rendered = json.dumps(value, ensure_ascii=False)
            entries.append(f"{json.dumps(key)}: {rendered}")
        metadata_literal = "{" + ", ".join(entries) + "}"
    else:
        metadata_literal = "{}"
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
                    "    description = 'Block risky commands'\n"
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _sample(sample_id: str, decision: str = "deny", command: str = "rm -rf /") -> PolicyGenerationEvidenceSample:
    return PolicyGenerationEvidenceSample(
        sample_id=sample_id,
        trace_digest="a" * 64,
        line_number=1,
        run_id="run-1",
        human_decision=decision,
        tool_name="shell_command",
        arguments={"command": command},
    )


def _opportunity() -> PolicyOpportunity:
    evidence = PolicyOpportunityEvidence(
        trace_digest="a" * 64,
        line_number=1,
        run_id="run-1",
        decision="deny",
    )
    return PolicyOpportunity(
        semantic_signature="b" * 64,
        theme="repeated_denial",
        hook="before_tool_call",
        tool_name="shell_command",
        policy_names=("tool_confirmation",),
        risk_level="medium",
        argument_fields=("command",),
        argument_shape_digest="c" * 64,
        occurrence_count=1,
        run_count=1,
        approve_count=0,
        deny_count=1,
        first_seen=None,
        last_seen=None,
        evidence=(evidence,),
    )


def _report() -> PolicyDiscoveryReport:
    source = PolicyDiscoverySource(name="trace.jsonl", trace_digest="a" * 64, record_count=1)
    return PolicyDiscoveryReport(
        input_digest="d" * 64,
        settings=PolicyDiscoverySettings(),
        sources=(source,),
        stats=PolicyDiscoveryStats(trace_count=1, record_count=1),
        opportunities=(_opportunity(),),
    )


def _proposal(strategy: str = "additive", **overrides: Any) -> PolicyGenerationProposal:
    base: dict[str, Any] = dict(
        strategy=strategy,
        candidate_name="block_risky_rm",
        description="Block risky rm",
        match_summary="1/1 match",
        rationale="Users denied risky rm",
        fallback_action="allow" if strategy == "additive" else "require_confirmation",
        replacement_target="tool_confirmation" if strategy == "replacement" else None,
        sample_decisions=(
            PolicyGenerationSampleDecision(sample_id="s1", action="block"),
        ),
    )
    base.update(overrides)
    proposal = PolicyGenerationProposal(**base)  # type: ignore[arg-type]
    # fill digest
    proposal.to_dict()
    return PolicyGenerationProposal(
        **{**base, "proposal_digest": proposal.to_dict()["proposal_digest"]}  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# validate_proposal
# ---------------------------------------------------------------------------

def test_proposal_additive_valid() -> None:
    samples = [_sample("s1")]
    proposal = _proposal("additive")
    assert validate_proposal(proposal, evidence=samples, opportunity=_opportunity()) == []


def test_proposal_additive_rejects_noop() -> None:
    samples = [_sample("s1")]
    proposal = _proposal(
        "additive",
        sample_decisions=(PolicyGenerationSampleDecision(sample_id="s1", action="allow"),),
    )
    errors = validate_proposal(proposal, evidence=samples, opportunity=_opportunity())
    assert any("no-op" in e for e in errors)


def test_proposal_additive_rejects_allow_action() -> None:
    samples = [_sample("s1")]
    proposal = _proposal(
        "additive",
        sample_decisions=(PolicyGenerationSampleDecision(sample_id="s1", action="allow"),),
    )
    errors = validate_proposal(proposal, evidence=samples, opportunity=_opportunity())
    assert errors


def test_proposal_replacement_valid() -> None:
    samples = [_sample("s1")]
    proposal = _proposal(
        "replacement",
        candidate_name="tool_confirmation",
        replacement_target="tool_confirmation",
    )
    assert validate_proposal(proposal, evidence=samples, opportunity=_opportunity()) == []


def test_proposal_replacement_rejects_unknown_target() -> None:
    samples = [_sample("s1")]
    proposal = _proposal(
        "replacement",
        candidate_name="wrong_target",
        replacement_target="wrong_target",
    )
    errors = validate_proposal(proposal, evidence=samples, opportunity=_opportunity())
    assert any("target" in e for e in errors)


def test_proposal_defer_rejects_fields() -> None:
    proposal = _proposal(
        "defer",
        candidate_name="some_policy",  # defer must not carry a candidate name
        replacement_target="tool_confirmation",
        fallback_action="require_confirmation",
    )
    errors = validate_proposal(proposal, evidence=[_sample("s1")], opportunity=_opportunity())
    assert errors  # defer must not carry automation fields


def test_proposal_explicit_name_is_hard_constraint() -> None:
    samples = [_sample("s1")]
    proposal = _proposal("additive", candidate_name="other_name")
    errors = validate_proposal(
        proposal,
        evidence=samples,
        opportunity=_opportunity(),
        explicit_name="block_risky_rm",
    )
    assert any("exactly" in e for e in errors)


def test_proposal_missing_sample_rejected() -> None:
    samples = [_sample("s1"), _sample("s2")]
    proposal = _proposal("additive")  # only decides s1
    errors = validate_proposal(proposal, evidence=samples, opportunity=_opportunity())
    assert any("misses" in e for e in errors)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def test_redact_proposal_text_replaces_string_values() -> None:
    samples = [_sample("s1", command="super-secret-command")]
    proposal = _proposal("additive", rationale="we saw super-secret-command in evidence")
    redacted = redact_proposal_text(proposal, samples)
    assert "super-secret-command" not in redacted.rationale
    assert "[redacted]" in redacted.rationale
    # structured decisions preserved
    assert redacted.sample_decisions == proposal.sample_decisions


def test_redact_keeps_short_values() -> None:
    samples = [_sample("s1", command="ls")]
    proposal = _proposal("additive", rationale="command was ls")
    redacted = redact_proposal_text(proposal, samples)
    assert "ls" in redacted.rationale  # length < 4 not redacted


def _identity_policy_py(
    *,
    candidate_name: str,
    description: str,
    risk_level: str,
    generation_id: str,
    report_id: str,
    report_digest: str,
    semantic_signature: str,
    proposal_digest: str,
    strategy: str,
    evidence_digest: str,
    replacement_target: str | None,
    action: str = "block",
) -> str:
    """Render a policy.py that declares exactly the Host identity contract."""
    metadata_items = [
        f"        {json.dumps(k)}: {json.dumps(v) if v is not None else 'None'}"
        for k, v in [
            ("generation_id", generation_id),
            ("report_id", report_id),
            ("report_digest", report_digest),
            ("semantic_signature", semantic_signature),
            ("proposal_digest", proposal_digest),
            ("strategy", strategy),
            ("evidence_digest", evidence_digest),
            ("replacement_target", replacement_target),
        ]
    ]
    metadata_block = (
        "    metadata = {\n"
        + ",\n".join(metadata_items)
        + "\n    }\n\n"
    )
    return (
        "from __future__ import annotations\n"
        "from evopi.policy.decisions import PolicyDecision\n"
        "from evopi.policy.types import PolicyContext\n\n"
        f"class GeneratedPolicy:\n"
        f"    name = {json.dumps(candidate_name)}\n"
        "    version = '0.1.0'\n"
        f"    description = {json.dumps(description)}\n"
        "    hooks = ('before_tool_call',)\n"
        "    priority = 100\n"
        "    enabled = True\n"
        "    source = 'generated'\n"
        f"    risk_level = {json.dumps(risk_level)}\n"
        + metadata_block
        + "    def run(self, context: PolicyContext) -> PolicyDecision:\n"
        "        if context.tool_call is not None and "
        "'risky' in str(context.tool_call.arguments):\n"
        f"            return PolicyDecision(action={json.dumps(action)})\n"
        "        return PolicyDecision(action='allow')\n\n"
        "POLICY = GeneratedPolicy()\n"
    )


def _identity_metadata_for(
    *,
    proposal: PolicyGenerationProposal,
    report: object,
    opportunity: object,
    evidence: list[PolicyGenerationEvidenceSample],
    generation_id: str,
    risk_level: str,
) -> dict:
    """Precompute the exact Host identity values for a scripted candidate."""
    from evopi.evolution.policy_generation import _evidence_digest

    return {
        "candidate_name": proposal.candidate_name,
        "description": proposal.description,
        "risk_level": risk_level,
        "generation_id": generation_id,
        "report_id": report.report_id,  # type: ignore[attr-defined]
        "report_digest": report.report_digest,  # type: ignore[attr-defined]
        "semantic_signature": opportunity.semantic_signature,  # type: ignore[attr-defined]
        "proposal_digest": (
            proposal.proposal_digest or proposal.to_dict()["proposal_digest"]
        ),
        "strategy": proposal.strategy,
        "evidence_digest": _evidence_digest(evidence),
        "replacement_target": proposal.replacement_target,
    }


# ---------------------------------------------------------------------------
# validate_candidate_bundle
# ---------------------------------------------------------------------------

def _bundle(policy_py: str = "POLICY = object()\n", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1,
        "files": [{"path": "policy.py", "content": policy_py}],
    }
    base.update(overrides)
    return base


def test_bundle_valid() -> None:
    errors = validate_candidate_bundle(
        _bundle(),
        settings=PolicyGenerationSettings(),
        protected_files=frozenset({"evopi-policy.json", "cases.py", "cases.json", "README.md", "test_policy.py"}),
    )
    assert errors == []


def test_bundle_rejects_protected_file() -> None:
    errors = validate_candidate_bundle(
        _bundle(files=[
            {"path": "policy.py", "content": "x = 1\n"},
            {"path": "evopi-policy.json", "content": "{}"},
        ]),
        settings=PolicyGenerationSettings(),
        protected_files=frozenset({"evopi-policy.json"}),
    )
    assert any("protected" in e for e in errors)


def test_bundle_rejects_path_traversal() -> None:
    errors = validate_candidate_bundle(
        _bundle(files=[
            {"path": "policy.py", "content": "x = 1\n"},
            {"path": "../evil.py", "content": "x = 1\n"},
        ]),
        settings=PolicyGenerationSettings(),
        protected_files=set(),
    )
    assert any("traversal" in e or "dot segment" in e for e in errors)


def test_bundle_rejects_duplicate() -> None:
    errors = validate_candidate_bundle(
        _bundle(files=[
            {"path": "policy.py", "content": "x = 1\n"},
            {"path": "policy.py", "content": "y = 2\n"},
        ]),
        settings=PolicyGenerationSettings(),
        protected_files=set(),
    )
    assert any("duplicate" in e for e in errors)


def test_bundle_rejects_invalid_python() -> None:
    errors = validate_candidate_bundle(
        _bundle(policy_py="def broken(:\n"),
        settings=PolicyGenerationSettings(),
        protected_files=set(),
    )
    assert any("not valid Python" in e for e in errors)


def test_bundle_rejects_missing_policy() -> None:
    errors = validate_candidate_bundle(
        _bundle(files=[]),
        settings=PolicyGenerationSettings(),
        protected_files=set(),
    )
    assert any("policy.py" in e for e in errors)


def test_bundle_rejects_nul_bytes() -> None:
    errors = validate_candidate_bundle(
        _bundle(policy_py="x = 1\x00\n"),
        settings=PolicyGenerationSettings(),
        protected_files=set(),
    )
    assert any("NUL" in e for e in errors)


def test_bundle_rejects_oversize() -> None:
    errors = validate_candidate_bundle(
        _bundle(files=[{"path": "policy.py", "content": "x" * 300}], schema_version=1),
        settings=PolicyGenerationSettings(max_file_bytes=100, max_total_file_bytes=200),
        protected_files=set(),
    )
    assert any("exceeds" in e for e in errors)


# ---------------------------------------------------------------------------
# build_host_files
# ---------------------------------------------------------------------------

def test_host_files_include_manifest_and_cases() -> None:
    files = build_host_files(
        candidate_name="block_risky_rm",
        description="desc",
        opportunity=_opportunity(),
        proposal=_proposal("additive"),
        evidence=[_sample("s1")],
        generation_id="g" * 32,
        report=_report(),
        semantic_signature="b" * 64,
        proposal_digest="e" * 64,
        evidence_digest="f" * 64,
    )
    assert "evopi-policy.json" in files
    assert "cases.py" in files
    assert "cases.json" in files
    assert "test_policy.py" in files
    assert "README.md" in files
    manifest = json.loads(files["evopi-policy.json"])
    assert manifest["source"] == "generated"
    assert manifest["hooks"] == ["before_tool_call"]
    assert manifest["priority"] == 100
    assert manifest["metadata"]["generation_id"] == "g" * 32
    assert manifest["metadata"]["strategy"] == "additive"


def test_host_files_risk_level_for_replacement() -> None:
    files = build_host_files(
        candidate_name="tool_confirmation",
        description="desc",
        opportunity=_opportunity(),  # medium risk
        proposal=_proposal("replacement", candidate_name="tool_confirmation"),
        evidence=[_sample("s1")],
        generation_id="g" * 32,
        report=_report(),
        semantic_signature="b" * 64,
        proposal_digest="e" * 64,
        evidence_digest="f" * 64,
    )
    manifest = json.loads(files["evopi-policy.json"])
    assert manifest["risk_level"] == "high"  # replacement risk is at least high


# ---------------------------------------------------------------------------
# Materialization with a scripted model (never executes candidate code)
# ---------------------------------------------------------------------------

class _ScriptedModel:
    """Returns a fixed JSON response; never contacts a Provider."""

    name = "scripted"
    provider = "test"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def stream(self, context):
        from evopi.core.messages import AssistantMessage

        async def _stream():
            yield ModelComplete(
                message=AssistantMessage(
                    content=json.dumps(self._payload),
                    stop_reason="stop",
                ),
            )

        return _stream()


def test_materialize_creates_candidate_directory(tmp_path: Path) -> None:
    proposal = _proposal("additive")
    report = _report()
    opportunity = _opportunity()
    samples = [_sample("s1")]
    meta = _identity_metadata_for(
        proposal=proposal,
        report=report,
        opportunity=opportunity,
        evidence=samples,
        generation_id="g" * 32,
        risk_level="medium",
    )
    policy_py = _identity_policy_py(**meta)
    bundle = _bundle(policy_py=policy_py)
    service = PolicyCandidateGenerationService(_ScriptedModel(bundle))
    target = tmp_path / "candidate"

    result = asyncio.run(
        service.materialize(
            proposal,
            report,
            opportunity,
            samples,
            generation_id="g" * 32,
            path=target,
        )
    )
    assert target.is_dir()
    assert (target / "policy.py").exists()
    assert (target / "evopi-policy.json").exists()
    assert (target / "cases.py").exists()
    assert result.record.outcome == "generated"
    assert result.record.candidate_digest is not None
    # Generated Python must never be executed — only static inspection ran
    assert result.candidate is not None


def test_materialize_rejects_conflicting_target(tmp_path: Path) -> None:
    proposal = _proposal("additive")
    service = PolicyCandidateGenerationService(_ScriptedModel(_bundle()))
    target = tmp_path / "candidate"
    target.mkdir()
    (target / "existing.txt").write_text("keep me")

    with pytest.raises(Exception, match="not empty"):
        asyncio.run(
            service.materialize(
                proposal,
                _report(),
                _opportunity(),
                [_sample("s1")],
                generation_id="g" * 32,
                path=target,
            )
        )
    # Existing file untouched
    assert (target / "existing.txt").read_text() == "keep me"


def test_materialize_cleans_staging_on_bad_bundle(tmp_path: Path) -> None:
    proposal = _proposal("additive")
    bad_bundle = _bundle(policy_py="def broken(:\n")  # invalid Python
    service = PolicyCandidateGenerationService(_ScriptedModel(bad_bundle))
    target = tmp_path / "candidate"

    with pytest.raises(Exception):
        asyncio.run(
            service.materialize(
                proposal,
                _report(),
                _opportunity(),
                [_sample("s1")],
                generation_id="g" * 32,
                path=target,
            )
        )
    assert not target.exists()
    # No leftover staging directories
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".candidate.")]
    assert leftovers == []


def test_materialize_model_tool_call_is_protocol_failure(tmp_path: Path) -> None:
    class _ToolCallModel:
        name = "toolcall"
        provider = "test"

        def stream(self, context):
            from evopi.core.tool import ToolCall

            async def _stream():
                yield ModelComplete(
                    message=AssistantMessage(
                        content="",
                        tool_calls=[
                            ToolCall(id="t1", name="x", arguments={})
                        ],
                        stop_reason="tool_use",
                    ),
                )

            return _stream()

    service = PolicyCandidateGenerationService(_ToolCallModel())
    with pytest.raises(Exception, match="ToolCall|turns|failed"):
        asyncio.run(
            service.materialize(
                _proposal("additive"),
                _report(),
                _opportunity(),
                [_sample("s1")],
                generation_id="g" * 32,
                path=tmp_path / "candidate",
            )
        )


def test_materialize_invalid_json_is_protocol_failure(tmp_path: Path) -> None:
    class _BadJsonModel:
        name = "badjson"
        provider = "test"

        def stream(self, context):
            async def _stream():
                yield ModelComplete(
                    message=AssistantMessage(
                        content="this is not json",
                        stop_reason="stop",
                    ),
                )

            return _stream()

    service = PolicyCandidateGenerationService(_BadJsonModel())
    with pytest.raises(Exception, match="JSON|json"):
        asyncio.run(
            service.materialize(
                _proposal("additive"),
                _report(),
                _opportunity(),
                [_sample("s1")],
                generation_id="g" * 32,
                path=tmp_path / "candidate",
            )
        )


# ---------------------------------------------------------------------------
# Revision 2: Windows paths, strict JSON, defer, nested redaction, repair
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_path",
    [
        "C:/outside.py",
        "c:\\outside.py",
        "D:/evil.py",
        "\\\\server\\share\\x.py",
        "\\\\server\\share\\x.py",
        "/absolute.py",
        "../escape.py",
        "sub/../escape.py",
        "helpers/",
        "helpers/empty/.hidden.py",
        "policy.txt",
        "cases.json",
        "EVOPI-POLICY.JSON",
    ],
)
def test_bundle_rejects_unsafe_paths(bad_path: str) -> None:
    errors = validate_candidate_bundle(
        _bundle(files=[
            {"path": "policy.py", "content": "x = 1\n"},
            {"path": bad_path, "content": "y = 2\n"},
        ]),
        settings=PolicyGenerationSettings(),
        protected_files=frozenset({"evopi-policy.json", "cases.py", "cases.json", "README.md", "test_policy.py"}),
    )
    assert errors, f"path {bad_path!r} should be rejected"


def test_bundle_rejects_case_collision() -> None:
    errors = validate_candidate_bundle(
        _bundle(files=[
            {"path": "policy.py", "content": "x = 1\n"},
            {"path": "helper.py", "content": "y = 2\n"},
            {"path": "HELPER.PY", "content": "z = 3\n"},
        ]),
        settings=PolicyGenerationSettings(),
        protected_files=set(),
    )
    assert any("case-colliding" in e or "duplicate" in e for e in errors)


def test_bundle_rejects_non_py_file() -> None:
    errors = validate_candidate_bundle(
        _bundle(files=[
            {"path": "policy.py", "content": "x = 1\n"},
            {"path": "notes.md", "content": "# notes"},
        ]),
        settings=PolicyGenerationSettings(),
        protected_files=set(),
    )
    assert any(".py" in e for e in errors)


def test_extract_json_rejects_prose() -> None:
    from evopi.evolution.policy_generation import _extract_json

    with pytest.raises(ValueError):
        _extract_json("Here is the JSON: {\"a\": 1}")


def test_extract_json_rejects_multiple_objects() -> None:
    from evopi.evolution.policy_generation import _extract_json

    with pytest.raises(ValueError):
        _extract_json('{"a": 1} trailing {"b": 2}')


def test_extract_json_rejects_partial_fence() -> None:
    from evopi.evolution.policy_generation import _extract_json

    with pytest.raises(ValueError):
        _extract_json('```json\n{"a": 1}')


def test_extract_json_accepts_complete_fence() -> None:
    from evopi.evolution.policy_generation import _extract_json

    result = _extract_json('```json\n{"a": 1}\n```')
    assert '"a"' in result


def test_defer_proposal_is_valid_without_name() -> None:
    from evopi.evolution.policy_generation import validate_proposal

    defer = PolicyGenerationProposal(
        strategy="defer",
        candidate_name="",
        description="",
        match_summary="",
        rationale="not now",
        fallback_action="allow",
        replacement_target=None,
        sample_decisions=(),
    )
    errors = validate_proposal(defer, evidence=[_sample("s1")], opportunity=_opportunity())
    assert errors == []


def test_defer_proposal_rejects_candidate_name() -> None:
    from evopi.evolution.policy_generation import validate_proposal

    defer = PolicyGenerationProposal(
        strategy="defer",
        candidate_name="some_policy",  # must be empty for defer
        description="",
        match_summary="",
        rationale="not now",
        fallback_action="allow",
    )
    errors = validate_proposal(defer, evidence=[_sample("s1")], opportunity=_opportunity())
    assert any("defer" in e for e in errors)


def test_nested_redaction_and_digest_round_trip() -> None:
    from evopi.evolution.policy_generation import redact_proposal_text
    from evopi.evolution.policy_generation_protocol import (
        policy_generation_proposal_from_dict,
    )

    nested_sample = PolicyGenerationEvidenceSample(
        sample_id="s1",
        trace_digest="a" * 64,
        line_number=1,
        run_id="run-1",
        human_decision="deny",
        tool_name="shell_command",
        arguments={"payload": {"command": "super-secret-value", "nested": ["deep-secret-2"]}},
    )
    proposal = _proposal("additive", rationale="saw super-secret-value and deep-secret-2")
    redacted = redact_proposal_text(proposal, [nested_sample])
    assert "super-secret-value" not in redacted.rationale
    assert "deep-secret-2" not in redacted.rationale
    assert "[redacted]" in redacted.rationale
    # Digest round-trips through the strict codec after redaction
    restored = policy_generation_proposal_from_dict(redacted.to_dict())
    assert restored.proposal_digest == redacted.proposal_digest


def test_one_schema_repair_succeeds() -> None:
    """A bad first response followed by a good one succeeds with one repair."""
    proposal = _proposal("additive")
    report = _report()
    opportunity = _opportunity()
    samples = [_sample("s1")]
    meta = _identity_metadata_for(
        proposal=proposal,
        report=report,
        opportunity=opportunity,
        evidence=samples,
        generation_id="g" * 32,
        risk_level="medium",
    )
    good_bundle = _bundle(policy_py=_identity_policy_py(**meta))

    class _RepairModel:
        name = "repair"
        provider = "test"

        def __init__(self) -> None:
            self.calls = 0

        def stream(self, context):
            async def _stream():
                self.calls += 1
                if self.calls == 1:
                    payload = {"not": "a proposal"}  # invalid
                else:
                    payload = good_bundle  # valid candidate bundle
                yield ModelComplete(
                    message=AssistantMessage(content=json.dumps(payload), stop_reason="stop"),
                )

            return _stream()

    model = _RepairModel()
    service = PolicyCandidateGenerationService(model)
    import tempfile

    target = Path(tempfile.gettempdir()) / f"candidate-repair-{__import__('uuid').uuid4().hex[:8]}"
    result = asyncio.run(
        service.materialize(
            proposal,
            report,
            opportunity,
            samples,
            generation_id="g" * 32,
            path=target,
        )
    )
    assert model.calls == 2  # initial + one repair
    assert result.record.outcome == "generated"
    import shutil

    shutil.rmtree(target, ignore_errors=True)


def test_repair_exhaustion_fails_closed() -> None:
    class _BadModel:
        name = "bad"
        provider = "test"

        def stream(self, context):
            async def _stream():
                yield ModelComplete(
                    message=AssistantMessage(
                        content=json.dumps({"not": "valid"}),
                        stop_reason="stop",
                    ),
                )

            return _stream()

    service = PolicyCandidateGenerationService(
        _BadModel(),
        settings=PolicyGenerationSettings(max_schema_repairs=1),
    )
    import tempfile

    target = Path(tempfile.gettempdir()) / f"candidate-exhaust-{__import__('uuid').uuid4().hex[:8]}"
    with pytest.raises(Exception, match="repair|failed"):
        asyncio.run(
            service.materialize(
                _proposal("additive"),
                _report(),
                _opportunity(),
                [_sample("s1")],
                generation_id="g" * 32,
                path=target,
            )
        )


# ---------------------------------------------------------------------------
# Revision 3: adversarial regressions (G/I)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_path",
    [
        "Z:/outside.py",
        "z:/x.py",
        "Y:relative.py",
        "C:foo.py",
        "Q:sub/bar.py",
        "\\\\server\\share\\x.py",
        "\\\\server\\share\\x.py",
        "/abs.py",
    ],
)
def test_bundle_rejects_arbitrary_windows_drive(bad_path: str) -> None:
    errors = validate_candidate_bundle(
        _bundle(files=[
            {"path": "policy.py", "content": "x = 1\n"},
            {"path": bad_path, "content": "y = 2\n"},
        ]),
        settings=PolicyGenerationSettings(),
        protected_files=set(),
    )
    assert errors, f"path {bad_path!r} must be rejected"


def test_bundle_requires_exact_lowercase_policy_py() -> None:
    # POLICY.PY (uppercase) must be rejected: exact root filename policy.py.
    errors = validate_candidate_bundle(
        _bundle(files=[
            {"path": "POLICY.PY", "content": "x = 1\n"},
        ]),
        settings=PolicyGenerationSettings(),
        protected_files=set(),
    )
    assert errors, "POLICY.PY must not be accepted as the policy entrypoint"


def test_invalid_json_receives_one_repair() -> None:
    """Invalid JSON followed by valid JSON uses exactly two model calls."""
    proposal = _proposal("additive")
    report = _report()
    opportunity = _opportunity()
    samples = [_sample("s1")]
    meta = _identity_metadata_for(
        proposal=proposal,
        report=report,
        opportunity=opportunity,
        evidence=samples,
        generation_id="g" * 32,
        risk_level="medium",
    )
    good_bundle = _bundle(policy_py=_identity_policy_py(**meta))

    class _JsonRepairModel:
        name = "json-repair"
        provider = "test"

        def __init__(self) -> None:
            self.calls = 0

        def stream(self, context):
            async def _stream():
                self.calls += 1
                if self.calls == 1:
                    content = "this is not json at all"  # invalid JSON
                else:
                    content = json.dumps(good_bundle)
                yield ModelComplete(
                    message=AssistantMessage(content=content, stop_reason="stop"),
                )

            return _stream()

    model = _JsonRepairModel()
    service = PolicyCandidateGenerationService(model)
    import tempfile

    target = Path(tempfile.gettempdir()) / f"candidate-json-repair-{__import__('uuid').uuid4().hex[:8]}"
    result = asyncio.run(
        service.materialize(
            proposal,
            report,
            opportunity,
            samples,
            generation_id="g" * 32,
            path=target,
        )
    )
    assert model.calls == 2, f"expected 2 calls (initial + repair), got {model.calls}"
    assert result.record.outcome == "generated"
    import shutil

    shutil.rmtree(target, ignore_errors=True)


def test_model_runs_accumulate_across_phases() -> None:
    """Proposal and Candidate model runs accumulate in call order."""

    proposal = _proposal("additive")
    report = _report()
    opportunity = _opportunity()
    samples = [_sample("s1")]
    meta = _identity_metadata_for(
        proposal=proposal,
        report=report,
        opportunity=opportunity,
        evidence=samples,
        generation_id="g" * 32,
        risk_level="medium",
    )
    good_bundle = _bundle(policy_py=_identity_policy_py(**meta))
    proposal_payload = {
        "schema_version": 1,
        "strategy": "additive",
        "candidate_name": proposal.candidate_name,
        "description": proposal.description,
        "match_summary": "1/1",
        "rationale": "x",
        "fallback_action": "allow",
        "replacement_target": None,
        "sample_decisions": [
            {"sample_id": d.sample_id, "action": d.action}
            for d in proposal.sample_decisions
        ],
        "warnings": [],
    }

    class _TwoPhaseModel:
        name = "two-phase"
        provider = "test"

        def __init__(self) -> None:
            self.calls = 0

        def stream(self, context):
            from evopi.core.messages import UserMessage

            async def _stream():
                self.calls += 1
                is_candidate = any(
                    isinstance(m, UserMessage) and "PROPOSAL" in m.content
                    for m in context.messages
                )
                content = (
                    json.dumps(good_bundle)
                    if is_candidate
                    else json.dumps(proposal_payload)
                )
                yield ModelComplete(
                    message=AssistantMessage(content=content, stop_reason="stop"),
                )

            return _stream()

    model = _TwoPhaseModel()
    service = PolicyCandidateGenerationService(model)
    import tempfile

    # Run the Proposal phase first.
    proposed = asyncio.run(
        service.propose(
            report,
            opportunity,
            samples,
        )
    )
    assert proposed.strategy == "additive"
    # Now compute the identity contract from the redacted proposal digest and
    # construct a second scripted model for the Candidate phase.
    meta = _identity_metadata_for(
        proposal=proposed,
        report=report,
        opportunity=opportunity,
        evidence=samples,
        generation_id="g" * 32,
        risk_level="medium",
    )
    good_bundle = _bundle(policy_py=_identity_policy_py(**meta))

    class _CandidateOnlyModel:
        name = "candidate-only"
        provider = "test"

        def stream(self, context):
            async def _stream():
                yield ModelComplete(
                    message=AssistantMessage(
                        content=json.dumps(good_bundle),
                        stop_reason="stop",
                    ),
                )

            return _stream()

    service2 = PolicyCandidateGenerationService(_CandidateOnlyModel())
    target = Path(tempfile.gettempdir()) / f"candidate-accum-{__import__('uuid').uuid4().hex[:8]}"
    result = asyncio.run(
        service2.materialize(
            proposed,
            report,
            opportunity,
            samples,
            generation_id="g" * 32,
            path=target,
        )
    )
    assert result.record.outcome == "generated"
    stages = [r.stage for r in service.model_runs] + [
        r.stage for r in service2.model_runs
    ]
    assert stages == ["proposal", "candidate"], f"expected proposal,candidate got {stages}"
    import shutil

    shutil.rmtree(target, ignore_errors=True)


def test_decoy_policy_class_rejected() -> None:
    """POLICY must be constructed from the verified class, not a decoy."""
    proposal = _proposal("additive")
    meta = _identity_metadata_for(
        proposal=proposal,
        report=_report(),
        opportunity=_opportunity(),
        evidence=[_sample("s1")],
        generation_id="g" * 32,
        risk_level="medium",
    )
    # A class with the right fields but POLICY points at a different class.
    decoy_py = _identity_policy_py(**meta).replace(
        "POLICY = GeneratedPolicy()",
        "class DecoyPolicy:\n    pass\nPOLICY = DecoyPolicy()",
    )
    from evopi.evolution.policy_generation import _verify_candidate_identity

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        policy_path = Path(td) / "policy.py"
        policy_path.write_text(decoy_py, encoding="utf-8")
        errors = _verify_candidate_identity(
            policy_path,
            candidate_name=proposal.candidate_name,
            description=proposal.description,
            risk_level="medium",
            generation_id="g" * 32,
            report_id=_report().report_id,
            report_digest=_report().report_digest,
            semantic_signature=_opportunity().semantic_signature,
            proposal_digest=meta["proposal_digest"],
            strategy="additive",
            evidence_digest=meta["evidence_digest"],
            replacement_target=None,
        )
        assert any("POLICY" in e for e in errors), f"decoy not caught: {errors}"


def test_host_derived_replacement_warning() -> None:
    """Proposal warnings include host-derived multi-Policy replacement note."""
    proposal = _proposal(
        "replacement",
        candidate_name="tool_confirmation",
        replacement_target="tool_confirmation",
    )
    from evopi.evolution.policy_generation import proposal_warnings

    warnings = proposal_warnings(proposal, _opportunity())
    # The fixture opportunity has only tool_confirmation, so no remaining
    # confirming Policies — the warning list is empty.  Build an opportunity
    # with an extra confirming Policy to exercise the warning.
    from dataclasses import replace as _replace

    multi = _replace(
        _opportunity(),
        policy_names=("tool_confirmation", "shell_safety"),
    )
    warnings = proposal_warnings(proposal, multi)
    assert any("remaining" in w or "leaves" in w for w in warnings)


def test_single_service_model_runs_in_record() -> None:
    """One Service owns the whole attempt; the Record stores its full history."""
    from evopi.core.messages import UserMessage

    proposal = _proposal(
        "additive",
        candidate_name="block_risky_command",
        description="Block risky commands",
    )
    report = _report()
    opportunity = _opportunity()
    samples = [_sample("s1", command="ls")]  # short value, no redaction

    proposal_payload = {
        "schema_version": 1,
        "strategy": "additive",
        "candidate_name": proposal.candidate_name,
        "description": proposal.description,
        "match_summary": "1/1",
        "rationale": "x",
        "fallback_action": "allow",
        "replacement_target": None,
        "sample_decisions": [
            {"sample_id": d.sample_id, "action": d.action}
            for d in proposal.sample_decisions
        ],
        "warnings": [],
    }

    class _SequentialModel:
        name = "sequential"
        provider = "test"

        def __init__(self) -> None:
            self.calls = 0

        def stream(self, context):
            async def _stream():
                self.calls += 1
                is_candidate = any(
                    isinstance(m, UserMessage) and "PROPOSAL" in m.content
                    for m in context.messages
                )
                if is_candidate:
                    # Candidate stage: build identity from the prompt contract.
                    from evopi.core.messages import SystemMessage

                    contract: dict = {}
                    for m in context.messages:
                        if isinstance(m, SystemMessage):
                            contract.update(_extract_contract(m.content))
                    payload = _candidate_payload(contract)
                else:
                    payload = proposal_payload
                yield ModelComplete(
                    message=AssistantMessage(
                        content=json.dumps(payload),
                        stop_reason="stop",
                    ),
                )

            return _stream()

    model = _SequentialModel()
    service = PolicyCandidateGenerationService(model)
    import tempfile

    proposed = asyncio.run(service.propose(report, opportunity, samples))
    target = Path(tempfile.gettempdir()) / f"candidate-single-{__import__('uuid').uuid4().hex[:8]}"
    result = asyncio.run(
        service.materialize(
            proposed,
            report,
            opportunity,
            samples,
            generation_id="g" * 32,
            path=target,
        )
    )
    assert result.record.outcome == "generated"
    # One Service, one history: proposal + candidate, stored in the Record.
    assert [r.stage for r in service.model_runs] == ["proposal", "candidate"]
    assert result.record.model_runs == service.model_runs
    import shutil

    shutil.rmtree(target, ignore_errors=True)


def test_defer_with_explicit_name_is_not_rejected() -> None:
    """--name constrains only materializable proposals; defer bypasses it."""
    from evopi.evolution.policy_generation import validate_proposal

    defer = PolicyGenerationProposal(
        strategy="defer",
        candidate_name="",
        description="",
        match_summary="",
        rationale="not now",
        fallback_action="allow",
    )
    errors = validate_proposal(
        defer,
        evidence=[_sample("s1")],
        opportunity=_opportunity(),
        explicit_name="some_other_name",  # would fail for additive/replacement
    )
    assert errors == []


def test_proposal_wall_clock_timeout_is_distinct_from_abort_and_audited() -> None:
    """A stage deadline is reported as timeout and retains its failed run audit."""

    class _BlockingModel:
        name = "blocking"
        provider = "test"

        def stream(self, context):
            async def _stream():
                await asyncio.Event().wait()
                yield ModelComplete(
                    message=AssistantMessage(content="unreachable", stop_reason="stop")
                )

            return _stream()

    service = PolicyCandidateGenerationService(
        _BlockingModel(),
        settings=PolicyGenerationSettings(stage_timeout=0.01),
    )

    with pytest.raises(PolicyGenerationRuntimeError) as captured:
        asyncio.run(service.propose(_report(), _opportunity(), [_sample("s1")]))

    assert captured.value.code == "model_timeout"
    assert len(service.model_runs) == 1
    run = service.model_runs[0]
    assert run.stage == "proposal"
    assert run.timed_out is True
    assert run.aborted is False
    assert run.failed is True
    assert run.error_code == "model_timeout"


def test_proposal_external_cancellation_is_aborted_and_audited() -> None:
    """A caller cancellation remains abort semantics and retains its run audit."""

    async def _scenario() -> tuple[
        PolicyGenerationRuntimeError,
        tuple[PolicyGenerationModelRun, ...],
    ]:
        class _BlockingModel:
            name = "blocking"
            provider = "test"

            def __init__(self) -> None:
                self.started = asyncio.Event()

            def stream(self, context):
                async def _stream():
                    self.started.set()
                    await asyncio.Event().wait()
                    yield ModelComplete(
                        message=AssistantMessage(
                            content="unreachable",
                            stop_reason="stop",
                        )
                    )

                return _stream()

        model = _BlockingModel()
        service = PolicyCandidateGenerationService(model)
        task = asyncio.create_task(
            service.propose(_report(), _opportunity(), [_sample("s1")])
        )
        await model.started.wait()
        task.cancel()
        with pytest.raises(PolicyGenerationRuntimeError) as captured:
            await task
        return captured.value, service.model_runs

    error, runs = asyncio.run(_scenario())
    assert error.code == "model_aborted"
    assert len(runs) == 1
    run = runs[0]
    assert run.timed_out is False
    assert run.aborted is True
    assert run.error_code == ""
