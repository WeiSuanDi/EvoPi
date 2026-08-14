from __future__ import annotations

import hashlib
import json
import zipfile
from io import BytesIO
from pathlib import Path

import httpx
import pytest

from evopi.distribution import (
    DistributionError,
    GitHubReleaseClient,
    ManagedRuntime,
    ReleaseInfo,
    UpdateStatus,
)


def _wheel_bytes(version: str = "0.2.1") -> bytes:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            f"evopi-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: evopi\nVersion: {version}\n",
        )
    return stream.getvalue()


def test_release_client_accepts_only_stable_versioned_assets() -> None:
    wheel = _wheel_bytes()
    digest = hashlib.sha256(wheel).hexdigest()
    release_payload = {
        "html_url": "https://github.com/WeiSuanDi/EvoPi/releases/tag/v0.2.1",
        "tag_name": "v0.2.1",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": "evopi-0.2.1-py3-none-any.whl",
                "browser_download_url": (
                    "https://github.com/WeiSuanDi/EvoPi/releases/download/v0.2.1/"
                    "evopi-0.2.1-py3-none-any.whl"
                ),
            },
            {
                "name": "SHA256SUMS",
                "browser_download_url": (
                    "https://github.com/WeiSuanDi/EvoPi/releases/download/v0.2.1/SHA256SUMS"
                ),
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=release_payload)
        if request.url.path.endswith("SHA256SUMS"):
            return httpx.Response(200, text=f"{digest}  evopi-0.2.1-py3-none-any.whl\n")
        return httpx.Response(200, content=wheel)

    client = GitHubReleaseClient(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    info, downloaded = client.fetch_latest()

    assert info.version == "0.2.1"
    assert info.sha256 == digest
    assert downloaded == wheel


@pytest.mark.parametrize("bad_url", [
    "http://github.com/WeiSuanDi/EvoPi/releases/download/v0.2.1/x.whl",
    "https://evil.example/evopi-0.2.1-py3-none-any.whl",
])
def test_release_client_rejects_unsafe_asset_hosts(bad_url: str) -> None:
    payload = {
        "html_url": "https://github.com/WeiSuanDi/EvoPi/releases/tag/v0.2.1",
        "tag_name": "v0.2.1",
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": "evopi-0.2.1-py3-none-any.whl", "browser_download_url": bad_url},
            {
                "name": "SHA256SUMS",
                "browser_download_url": "https://github.com/x/SHA256SUMS",
            },
        ],
    }
    client = GitHubReleaseClient(
        client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        )
    )
    with pytest.raises(DistributionError, match="HTTPS GitHub"):
        client.fetch_latest()


def test_managed_runtime_switches_only_after_successful_install(tmp_path: Path) -> None:
    runtime = ManagedRuntime(tmp_path, installer=lambda info, wheel, target: target.mkdir())
    current = tmp_path / "runtime" / "current.txt"
    current.parent.mkdir(parents=True)
    current.write_text("0.2.0\n", encoding="utf-8")
    (tmp_path / "runtime" / "versions" / "0.2.0").mkdir(parents=True)
    wheel = _wheel_bytes()
    info = ReleaseInfo(
        version="0.2.1",
        release_url="https://github.com/WeiSuanDi/EvoPi/releases/tag/v0.2.1",
        wheel_name="evopi-0.2.1-py3-none-any.whl",
        wheel_url="https://github.com/WeiSuanDi/EvoPi/releases/download/v0.2.1/x.whl",
        sha256=hashlib.sha256(wheel).hexdigest(),
        checksum_url="https://github.com/WeiSuanDi/EvoPi/releases/download/v0.2.1/SHA256SUMS",
    )

    result = runtime.install(info, wheel)

    assert result.status is UpdateStatus.UPDATED
    assert current.read_text(encoding="utf-8").strip() == "0.2.1"


def test_managed_runtime_failed_install_keeps_current_pointer(tmp_path: Path) -> None:
    def fail(info: ReleaseInfo, wheel: bytes, target: Path) -> None:
        raise RuntimeError("smoke failed")

    runtime = ManagedRuntime(tmp_path, installer=fail)
    current = tmp_path / "runtime" / "current.txt"
    current.parent.mkdir(parents=True)
    current.write_text("0.2.0\n", encoding="utf-8")
    wheel = _wheel_bytes()
    info = ReleaseInfo(
        version="0.2.1",
        release_url="https://github.com/WeiSuanDi/EvoPi/releases/tag/v0.2.1",
        wheel_name="evopi-0.2.1-py3-none-any.whl",
        wheel_url="https://github.com/WeiSuanDi/EvoPi/releases/download/v0.2.1/x.whl",
        sha256=hashlib.sha256(wheel).hexdigest(),
        checksum_url="https://github.com/WeiSuanDi/EvoPi/releases/download/v0.2.1/SHA256SUMS",
    )

    result = runtime.install(info, wheel)

    assert result.status is UpdateStatus.FAILED
    assert current.read_text(encoding="utf-8").strip() == "0.2.0"
    assert json.dumps(result.to_dict())


def test_managed_runtime_rejects_mismatched_existing_marker_without_deleting_it(
    tmp_path: Path,
) -> None:
    runtime = ManagedRuntime(tmp_path, installer=lambda info, wheel, target: target.mkdir())
    current = tmp_path / "runtime" / "current.txt"
    current.parent.mkdir(parents=True)
    current.write_text("0.2.0\n", encoding="utf-8")
    target = tmp_path / "runtime" / "versions" / "0.2.1"
    target.mkdir(parents=True)
    marker = target / ".evopi-runtime.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "version": "0.2.1",
                "features": [],
                "sha256": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    wheel = _wheel_bytes()
    info = ReleaseInfo(
        version="0.2.1",
        release_url="https://github.com/WeiSuanDi/EvoPi/releases/tag/v0.2.1",
        wheel_name="evopi-0.2.1-py3-none-any.whl",
        wheel_url="https://github.com/WeiSuanDi/EvoPi/releases/download/v0.2.1/x.whl",
        sha256=hashlib.sha256(wheel).hexdigest(),
        checksum_url="https://github.com/WeiSuanDi/EvoPi/releases/download/v0.2.1/SHA256SUMS",
    )

    result = runtime.install(info, wheel)

    assert result.status is UpdateStatus.FAILED
    assert current.read_text(encoding="utf-8").strip() == "0.2.0"
    assert marker.exists()


def test_managed_runtime_rolls_back_only_to_verified_version(tmp_path: Path) -> None:
    runtime = ManagedRuntime(tmp_path)
    versions = tmp_path / "runtime" / "versions"
    for version in ("0.2.0", "0.2.1"):
        target = versions / version
        target.mkdir(parents=True)
        (target / ".evopi-runtime.json").write_text(
            json.dumps({"schema_version": 1, "version": version, "sha256": "a" * 64}),
            encoding="utf-8",
        )
    (versions / "0.1.9").mkdir()
    current = tmp_path / "runtime" / "current.txt"
    current.write_text("0.2.1\n", encoding="utf-8")

    result = runtime.rollback()

    assert result.status is UpdateStatus.ROLLED_BACK
    assert current.read_text(encoding="utf-8").strip() == "0.2.0"


def test_managed_runtime_identity_includes_and_preserves_remote_feature(
    tmp_path: Path,
) -> None:
    installed: list[Path] = []

    def install(info: ReleaseInfo, wheel: bytes, target: Path) -> None:
        del info, wheel
        target.mkdir(parents=True)
        installed.append(target)

    runtime = ManagedRuntime(tmp_path, installer=install)
    wheel = _wheel_bytes()
    info = ReleaseInfo(
        version="0.3.0",
        release_url="https://github.com/WeiSuanDi/EvoPi/releases/tag/v0.3.0",
        wheel_name="evopi-0.3.0-py3-none-any.whl",
        wheel_url="https://github.com/WeiSuanDi/EvoPi/releases/download/v0.3.0/x.whl",
        sha256=hashlib.sha256(wheel).hexdigest(),
        checksum_url="https://github.com/WeiSuanDi/EvoPi/releases/download/v0.3.0/SHA256SUMS",
    )

    result = runtime.install(info, wheel, features=("remote",))

    runtime_id = (tmp_path / "runtime" / "current.txt").read_text().strip()
    assert result.status is UpdateStatus.UPDATED
    assert runtime_id.startswith("0.3.0--remote-")
    assert installed == [tmp_path / "runtime" / "versions" / runtime_id]
    assert runtime.current_version == "0.3.0"
    assert runtime.current_features == ("remote",)

    next_info = ReleaseInfo(
        version="0.3.1",
        release_url="https://github.com/WeiSuanDi/EvoPi/releases/tag/v0.3.1",
        wheel_name="evopi-0.3.1-py3-none-any.whl",
        wheel_url="https://github.com/WeiSuanDi/EvoPi/releases/download/v0.3.1/x.whl",
        sha256=hashlib.sha256(wheel).hexdigest(),
        checksum_url="https://github.com/WeiSuanDi/EvoPi/releases/download/v0.3.1/SHA256SUMS",
    )
    runtime.install(next_info, wheel)

    assert runtime.current_version == "0.3.1"
    assert runtime.current_features == ("remote",)
