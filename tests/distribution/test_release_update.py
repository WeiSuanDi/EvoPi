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


@pytest.mark.parametrize(
    "marker_payload",
    (
        {
            "schema_version": True,
            "version": "0.2.1",
            "sha256": hashlib.sha256(_wheel_bytes()).hexdigest(),
        },
        (
            '{"schema_version":1,"version":"0.2.1","sha256":"bad",'
            f'"sha256":"{hashlib.sha256(_wheel_bytes()).hexdigest()}"}}'
        ),
    ),
)
def test_managed_runtime_rejects_noncanonical_existing_marker(
    tmp_path: Path, marker_payload: object
) -> None:
    runtime = ManagedRuntime(tmp_path, installer=lambda info, wheel, target: target.mkdir())
    target = tmp_path / "runtime" / "versions" / "0.2.1"
    target.mkdir(parents=True)
    marker = target / ".evopi-runtime.json"
    marker.write_text(
        marker_payload
        if isinstance(marker_payload, str)
        else json.dumps(marker_payload),
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
    assert not (tmp_path / "runtime" / "current.txt").exists()


def test_managed_runtime_rollback_skips_malformed_marker(tmp_path: Path) -> None:
    runtime = ManagedRuntime(tmp_path)
    versions = tmp_path / "runtime" / "versions"
    valid = versions / "0.2.0"
    valid.mkdir(parents=True)
    (valid / ".evopi-runtime.json").write_text(
        json.dumps({"schema_version": 1, "version": "0.2.0", "sha256": "a" * 64}),
        encoding="utf-8",
    )
    malformed = versions / "9.9.9"
    malformed.mkdir()
    (malformed / ".evopi-runtime.json").write_text(
        json.dumps({"schema_version": True, "version": "9.9.9", "sha256": "b" * 64}),
        encoding="utf-8",
    )
    (tmp_path / "runtime" / "current.txt").write_text("0.3.0\n", encoding="utf-8")

    result = runtime.rollback()

    assert result.status is UpdateStatus.ROLLED_BACK
    assert runtime.current_runtime_id == "0.2.0"


def test_managed_runtime_rejects_unsafe_current_pointer(tmp_path: Path) -> None:
    runtime = ManagedRuntime(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    (outside / ".evopi-runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "version": "0.3.0",
                "features": ["remote"],
                "sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    runtime.current_path.parent.mkdir(parents=True)
    runtime.current_path.write_text("../../outside\n", encoding="utf-8")

    assert runtime.current_runtime_id is None
    assert runtime.current_version is None
    assert runtime.current_features == ()


@pytest.mark.parametrize("version", ("../escape", "01.2.3"))
def test_managed_runtime_rejects_unsafe_release_version_before_install(
    tmp_path: Path, version: str
) -> None:
    called = False

    def installer(info: ReleaseInfo, wheel: bytes, target: Path) -> None:
        nonlocal called
        called = True

    wheel = _wheel_bytes()
    runtime = ManagedRuntime(tmp_path, installer=installer)
    result = runtime.install(
        ReleaseInfo(
            version=version,
            release_url="https://github.com/WeiSuanDi/EvoPi/releases/latest",
            wheel_name=f"evopi-{version}-py3-none-any.whl",
            wheel_url="https://github.com/WeiSuanDi/EvoPi/releases/download/x.whl",
            sha256=hashlib.sha256(wheel).hexdigest(),
            checksum_url="https://github.com/WeiSuanDi/EvoPi/releases/download/SHA256SUMS",
        ),
        wheel,
    )

    assert result.status is UpdateStatus.FAILED
    assert called is False
    assert not (tmp_path / "runtime" / "escape").exists()


def test_managed_runtime_recomputes_wheel_digest_before_install(tmp_path: Path) -> None:
    called = False

    def installer(info: ReleaseInfo, wheel: bytes, target: Path) -> None:
        nonlocal called
        called = True

    runtime = ManagedRuntime(tmp_path, installer=installer)
    result = runtime.install(
        ReleaseInfo(
            version="0.2.1",
            release_url="https://github.com/WeiSuanDi/EvoPi/releases/tag/v0.2.1",
            wheel_name="evopi-0.2.1-py3-none-any.whl",
            wheel_url="https://github.com/WeiSuanDi/EvoPi/releases/download/v0.2.1/x.whl",
            sha256="0" * 64,
            checksum_url="https://github.com/WeiSuanDi/EvoPi/releases/download/v0.2.1/SHA256SUMS",
        ),
        _wheel_bytes(),
    )

    assert result.status is UpdateStatus.FAILED
    assert called is False
