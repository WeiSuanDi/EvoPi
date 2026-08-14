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
