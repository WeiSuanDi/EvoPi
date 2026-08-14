from __future__ import annotations

import json
from pathlib import Path

import pytest

from evopi.remote import RemoteAuditError, RemoteAuditLog, verify_remote_audit_chain


def test_remote_audit_is_append_only_hash_chained_and_redacted(tmp_path: Path) -> None:
    audit = RemoteAuditLog(tmp_path)
    first = audit.append(
        action="auth.verify",
        outcome="allowed",
        device_id="device-1",
        client_ip="203.0.113.1",
        details={"run_id": "run-1"},
    )
    second = audit.append(
        action="run.start",
        outcome="allowed",
        device_id="device-1",
        client_ip="203.0.113.1",
        details={"run_id": "run-1"},
    )

    assert first.entry_hash == second.previous_hash
    assert verify_remote_audit_chain(audit.current_path) == 2
    payload = audit.current_path.read_text(encoding="utf-8")
    assert "prompt" not in payload.lower()


def test_remote_audit_rejects_sensitive_detail_keys(tmp_path: Path) -> None:
    audit = RemoteAuditLog(tmp_path)
    with pytest.raises(RemoteAuditError, match="sensitive"):
        audit.append(
            action="run.start",
            outcome="denied",
            details={"prompt": "secret"},
        )


def test_remote_audit_detects_tampering(tmp_path: Path) -> None:
    audit = RemoteAuditLog(tmp_path)
    audit.append(action="auth.verify", outcome="allowed")
    lines = audit.current_path.read_text(encoding="utf-8").splitlines()
    value = json.loads(lines[0])
    value["outcome"] = "denied"
    audit.current_path.write_text(json.dumps(value) + "\n", encoding="utf-8")

    with pytest.raises(RemoteAuditError, match="digest"):
        verify_remote_audit_chain(audit.current_path)
