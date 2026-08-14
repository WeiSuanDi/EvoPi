from __future__ import annotations

import json
from pathlib import Path

import pytest

from evopi.evolution import WorkspaceTrustStore


def test_workspace_trust_round_trip_preserves_verified_record(tmp_path: Path) -> None:
    path = tmp_path / "trust.json"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = WorkspaceTrustStore(path).trust(workspace, trusted_by="user")

    restored = WorkspaceTrustStore(path)

    assert restored.is_trusted(workspace) is True
    assert restored.records() == (record,)


@pytest.mark.parametrize(
    "payload",
    (
        '{"schema_version":true,"workspaces":[]}',
        '{"schema_version":1,"schema_version":1,"workspaces":[]}',
        '{"schema_version":1,"workspaces":[],"unexpected":true}',
    ),
)
def test_workspace_trust_rejects_noncanonical_store(
    tmp_path: Path, payload: str
) -> None:
    path = tmp_path / "trust.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match="workspace trust store"):
        WorkspaceTrustStore(path)


def test_workspace_trust_rejects_duplicate_records(tmp_path: Path) -> None:
    path = tmp_path / "trust.json"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = WorkspaceTrustStore(path)
    store.trust(workspace, trusted_by="user")
    raw = json.loads(path.read_text(encoding="utf-8"))
    duplicate = dict(raw["workspaces"][0])
    raw["workspaces"].append(duplicate)
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="workspace trust record"):
        WorkspaceTrustStore(path)


def test_workspace_trust_rejects_coerced_record_fields(tmp_path: Path) -> None:
    path = tmp_path / "trust.json"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    WorkspaceTrustStore(path).trust(workspace, trusted_by="user")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["workspaces"][0]["trusted_by"] = 7
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="workspace trust record"):
        WorkspaceTrustStore(path)


def test_workspace_trust_rejects_symbolic_link_store(tmp_path: Path) -> None:
    target = tmp_path / "real.json"
    target.write_text('{"schema_version":1,"workspaces":[]}', encoding="utf-8")
    link = tmp_path / "trust.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ValueError, match="symbolic link"):
        WorkspaceTrustStore(link)
