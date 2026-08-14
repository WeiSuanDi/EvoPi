from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
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
    assert "203.0.113.1" not in payload
    ip_payload = next(tmp_path.glob("remote-client-ip-*.jsonl")).read_text(
        encoding="utf-8"
    )
    assert "203.0.113.1" in ip_payload


def test_remote_audit_prunes_raw_client_ip_sidecars_after_retention(tmp_path: Path) -> None:
    audit = RemoteAuditLog(tmp_path)
    old = tmp_path / "remote-client-ip-2020-01-01.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    os.utime(old, (1, 1))
    fresh = tmp_path / f"remote-client-ip-{datetime.now(UTC):%Y-%m-%d}.jsonl"
    fresh.write_text("{}\n", encoding="utf-8")

    removed = audit.prune_expired_client_ips(
        now=datetime.now(UTC) + timedelta(days=31), retention_days=30
    )

    assert old in removed
    assert fresh in removed
    assert not old.exists()


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


def test_remote_audit_serializes_gateway_and_admin_threads(tmp_path: Path) -> None:
    audit = RemoteAuditLog(tmp_path)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(
            executor.map(
                lambda index: audit.append(
                    action="runtime.operation",
                    outcome="allowed",
                    details={"index": index},
                ),
                range(20),
            )
        )

    assert verify_remote_audit_chain(audit.current_path) == 20
