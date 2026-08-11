from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]


def test_release_metadata_and_license_are_product_ready() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == "0.2.0"
    assert project["authors"] == [{"name": "WeiSuanDi"}]
    assert project["urls"]["Repository"] == "https://github.com/WeiSuanDi/EvoPi"
    assert "MIT License" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert (ROOT / "install.ps1").is_file()
    assert (ROOT / ".github" / "workflows" / "release.yml").is_file()


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
