"""Isolated subprocess entrypoint for formal Policy review."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evopi.evolution.policy_candidates import (  # noqa: E402
    PolicyCandidate,
    inspect_policy_candidate,
    resolve_policy_entrypoint,
)
from evopi.policy.types import Policy, PolicyContext  # noqa: E402
from evopi.validators import (  # noqa: E402
    PolicyDryRunCase,
    PolicySchemaValidator,
    ReplayReport,
    ValidationResult,
    build_policy_review_report,
    dry_run_policy,
    load_before_tool_replay_cases,
    replay_policy,
)


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict) or request.get("protocol_version") != 1:
            raise ValueError("unsupported review worker protocol")
        snapshot = Path(str(request["snapshot"])).resolve()
        inspection = inspect_policy_candidate(snapshot)
        if inspection.candidate.artifact.digest != request.get("manifest_digest"):
            raise ValueError("snapshot digest does not match review request")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            report = asyncio.run(
                _review(
                    inspection.candidate,
                    trace_path=request.get("trace"),
                )
            )
        sys.stdout.write(
            json.dumps(
                {"ok": True, "report": report.to_dict()},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 0
    except BaseException as exc:
        sys.stdout.write(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 1


async def _review(
    candidate: PolicyCandidate,
    *,
    trace_path: object,
):
    policy = cast(Policy, _load_reference(candidate.path, candidate.manifest.entrypoint))
    schema = PolicySchemaValidator().validate(policy)
    schema.errors.extend(_contract_errors(candidate, policy))
    schema.passed = not schema.errors
    dry_run: ValidationResult | None = None
    replay: ReplayReport | None = None
    if schema.passed and candidate.manifest.dry_run_entrypoint is not None:
        try:
            raw_cases = _load_reference(
                candidate.path,
                candidate.manifest.dry_run_entrypoint,
            )
            if callable(raw_cases) and not isinstance(raw_cases, Iterable):
                raw_cases = raw_cases()
            if isinstance(raw_cases, (str, bytes)) or not isinstance(raw_cases, Iterable):
                raise TypeError("Dry-run entrypoint must return an iterable")
            cases = list(raw_cases)
            if any(
                not isinstance(case, (PolicyContext, PolicyDryRunCase))
                for case in cases
            ):
                raise TypeError(
                    "Dry-run entrypoint must contain PolicyContext or PolicyDryRunCase objects"
                )
        except Exception as exc:
            dry_run = ValidationResult(
                passed=False,
                errors=[f"Dry-run cases could not be loaded: {type(exc).__name__}: {exc}"],
            )
        else:
            dry_run = await dry_run_policy(policy, cases)
    if schema.passed and trace_path is not None:
        try:
            cases = load_before_tool_replay_cases(
                Path(str(trace_path)),
                policy_name=policy.name,
            )
        except Exception as exc:
            replay = ReplayReport(
                policy_name=policy.name,
                errors=[f"Trace replay could not be loaded: {type(exc).__name__}: {exc}"],
            )
        else:
            replay = await replay_policy(policy, cases)
    return build_policy_review_report(
        policy,
        schema_result=schema,
        dry_run_result=dry_run,
        replay_report=replay,
    )


def _load_reference(root: Path, spec: str) -> Any:
    path, attribute = resolve_policy_entrypoint(root, spec)
    module_name = f"_evopi_policy_candidate_{uuid4().hex}"
    module_spec = importlib.util.spec_from_file_location(module_name, path)
    if module_spec is None or module_spec.loader is None:
        raise ImportError(f"could not load candidate module: {path}")
    module = importlib.util.module_from_spec(module_spec)
    sys.path.insert(0, str(root))
    try:
        module_spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(root))
    value: Any = module
    for part in attribute.split("."):
        value = getattr(value, part)
    if isinstance(value, type):
        value = value()
    elif not callable(getattr(value, "run", None)) and callable(value):
        value = value()
    return value


def _contract_errors(candidate: PolicyCandidate, policy: Policy) -> list[str]:
    manifest = candidate.manifest
    expected = {
        "name": manifest.name,
        "version": manifest.version,
        "description": manifest.description,
        "hooks": manifest.hooks,
        "priority": manifest.priority,
        "source": manifest.source,
        "risk_level": manifest.risk_level,
        "metadata": manifest.metadata,
        "enabled": True,
    }
    return [
        f"Policy field '{name}' does not match its manifest"
        for name, value in expected.items()
        if getattr(policy, name, None) != value
    ]


if __name__ == "__main__":
    raise SystemExit(main())
