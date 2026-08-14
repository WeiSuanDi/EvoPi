from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]


def test_release_metadata_and_license_are_product_ready() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == "0.3.0"
    assert project["authors"] == [{"name": "WeiSuanDi"}]
    assert project["urls"]["Repository"] == "https://github.com/WeiSuanDi/EvoPi"
    assert "MIT License" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert (ROOT / "install.ps1").is_file()
    assert (ROOT / ".github" / "workflows" / "release.yml").is_file()
    assert (ROOT / ".github" / "workflows" / "remote-client-release.yml").is_file()


def test_pull_request_ci_covers_python_and_remote_client() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
    assert workflow_path.is_file()

    workflow = workflow_path.read_text(encoding="utf-8")
    assert "pull_request:" in workflow
    assert "push:" in workflow
    assert "branches: [main]" in workflow
    assert "ubuntu-latest" in workflow
    assert "windows-latest" in workflow
    for version in ('"3.11"', '"3.12"', '"3.13"'):
        assert version in workflow
    assert 'python -m pip install -e ".[dev]"' in workflow
    assert "python -m ruff check ." in workflow
    assert "python -m mypy" in workflow
    assert "python -m pytest -q" in workflow
    assert "working-directory: packages/remote-client" in workflow
    assert "npm ci" in workflow
    assert "npm run typecheck" in workflow
    assert "npm test" in workflow
    assert "npm pack --dry-run" in workflow
    assert "python -m build" in workflow
    assert "python -m twine check dist/*" in workflow
    assert "evopi remote serve --help" in workflow


def test_development_extra_covers_remote_test_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    optional = project["optional-dependencies"]
    remote = set(optional["remote"])
    development = set(optional["dev"])

    assert remote <= development
    assert "httpx2>=2.0.0" in development


def test_unreleased_remote_install_does_not_reference_a_missing_tag() -> None:
    for name in ("README.md", "README.zh-CN.md"):
        content = (ROOT / name).read_text(encoding="utf-8")
        assert "EvoPi.git@v0.3.0" not in content
        assert "EvoPi.git@main" in content


def test_install_script_parses_as_powershell() -> None:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    script = ROOT / "install.ps1"
    escaped_script = str(script).replace("'", "''")
    command = (
        "$errors=$null; [System.Management.Automation.Language.Parser]::ParseFile("
        f"'{escaped_script}', [ref]$null, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
    )
    subprocess.run([executable, "-NoProfile", "-Command", command], check=True)


def test_install_script_supports_and_smokes_remote_feature() -> None:
    script = (ROOT / "install.ps1").read_text(encoding="utf-8")

    assert '[ValidateSet("remote")]' in script
    assert "remote serve --help" in script
    assert "features = $selectedFeatures" in script


def test_release_workflow_supports_non_publishing_preflight() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    assert "workflow_dispatch:" in workflow
    assert "EVOPI_RELEASE_TAG:" in workflow
    assert "github.event_name == 'push'" in workflow
    assert 'python -m pip install -e ".[dev]"' in workflow


def test_remote_client_release_uses_independent_tag_and_provenance() -> None:
    workflow = (ROOT / ".github" / "workflows" / "remote-client-release.yml").read_text(
        encoding="utf-8"
    )

    assert '"remote-client-v*"' in workflow
    assert "npm publish --access public --provenance" in workflow
    assert "workflow_dispatch:" in workflow


def test_base_cli_help_does_not_import_remote_optional_dependencies() -> None:
    script = """
import builtins
original = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == 'cryptography' or name.startswith('cryptography.'):
        raise ImportError('remote optional dependency blocked by test')
    return original(name, *args, **kwargs)
builtins.__import__ = blocked
from evopi.cli.main import main
raise SystemExit(main(['--help']))
"""

    subprocess.run([sys.executable, "-c", script], cwd=ROOT, check=True)
