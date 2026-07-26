from __future__ import annotations

from pathlib import Path

from evopi.evolution import ActivationDecision, ActivationStore
from evopi.plugins import (
    PluginArtifactStore,
    PluginCandidateStatus,
    PluginManager,
    review_plugin,
)


PLUGIN = """\
from evopi.plugins import Plugin, PluginMetadata

class DemoPlugin(Plugin):
    @property
    def meta(self):
        return PluginMetadata(name="demo", version="1.0.0")

    def register(self, api):
        return None
"""


def write_candidate(root: Path, *, side_effect: Path | None = None) -> Path:
    directory = root / "demo"
    directory.mkdir(parents=True)
    code = PLUGIN
    if side_effect is not None:
        code = (
            "from pathlib import Path\n"
            f"Path({str(side_effect)!r}).write_text('executed', encoding='utf-8')\n"
            + code
        )
    (directory / "plugin.py").write_text(code, encoding="utf-8")
    (directory / "evopi-plugin.json").write_text(
        '{"schema_version":1,"name":"demo","version":"1.0.0",'
        '"entrypoint":"plugin.py","description":"demo","dependencies":[]}',
        encoding="utf-8",
    )
    return directory


def test_review_plugin_never_imports_candidate_code(tmp_path: Path) -> None:
    marker = tmp_path / "executed.txt"
    path = write_candidate(tmp_path / "candidates", side_effect=marker)

    report = review_plugin(path)

    assert report.passed is True
    assert marker.exists() is False
    assert report.candidate.name == "demo"


def test_manager_marks_changed_approved_source_as_stale(tmp_path: Path) -> None:
    path = write_candidate(tmp_path / "plugins")
    store = ActivationStore(tmp_path / "activations.json")
    initial = review_plugin(path).candidate
    store.add(
        candidate=initial.artifact,
        decision=ActivationDecision.APPROVED,
        decided_by="tester",
    )
    (path / "plugin.py").write_text(PLUGIN + "\n# changed\n", encoding="utf-8")

    states = PluginManager(
        workspace=tmp_path,
        activation_store=store,
        candidate_paths=[path],
    ).states()

    assert states[0].status is PluginCandidateStatus.STALE


def test_approved_snapshot_is_immutable_when_source_changes(tmp_path: Path) -> None:
    path = write_candidate(tmp_path / "plugins")
    candidate = review_plugin(path).candidate
    store = PluginArtifactStore(tmp_path / "artifacts")

    snapshot = store.install(candidate)
    original = (snapshot / "plugin.py").read_text(encoding="utf-8")
    (path / "plugin.py").write_text(PLUGIN + "\n# changed\n", encoding="utf-8")

    assert (snapshot / "plugin.py").read_text(encoding="utf-8") == original
    assert store.entrypoint_for(candidate.artifact) == snapshot / "plugin.py"
