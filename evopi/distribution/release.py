"""Strict GitHub Release discovery and artifact validation."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from email.parser import BytesParser
from io import BytesIO
from urllib.parse import urlsplit

import httpx

from evopi.distribution.models import DistributionError, ReleaseInfo

_LATEST_RELEASE_API = "https://api.github.com/repos/WeiSuanDi/EvoPi/releases/latest"
_SEMVER = re.compile(r"^v(?P<version>0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GITHUB_ASSET_HOSTS = {"github.com", "objects.githubusercontent.com"}
_GITHUB_DOWNLOAD_HOSTS = {
    *_GITHUB_ASSET_HOSTS,
    "release-assets.githubusercontent.com",
    "github-releases.githubusercontent.com",
}


def parse_stable_tag(tag: str) -> str:
    match = _SEMVER.fullmatch(tag)
    if match is None:
        raise DistributionError(f"release tag is not stable SemVer: {tag}")
    return tag[1:]


def version_key(version: str) -> tuple[int, int, int]:
    try:
        parts = tuple(int(part) for part in version.split("."))
    except ValueError as exc:
        raise DistributionError(f"invalid release version: {version}") from exc
    if len(parts) != 3 or any(part < 0 for part in parts):
        raise DistributionError(f"invalid release version: {version}")
    return parts  # type: ignore[return-value]


def _validate_asset_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in _GITHUB_ASSET_HOSTS:
        raise DistributionError("release assets must use an HTTPS GitHub host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DistributionError("release asset URL contains forbidden components")


def _validate_download_response_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in _GITHUB_DOWNLOAD_HOSTS:
        raise DistributionError("release download redirected outside HTTPS GitHub hosts")
    if parsed.username or parsed.password or parsed.fragment:
        raise DistributionError("release download URL contains forbidden components")


def _wheel_metadata_version(wheel: bytes) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(BytesIO(wheel)) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise DistributionError("wheel must contain exactly one METADATA file")
            metadata = BytesParser().parsebytes(archive.read(names[0]))
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise DistributionError("downloaded EvoPi wheel is invalid") from exc
    name = metadata.get("Name", "")
    version = metadata.get("Version", "")
    if name.lower() != "evopi" or not version:
        raise DistributionError("wheel metadata does not identify EvoPi")
    return name, version


class GitHubReleaseClient:
    def __init__(self, *, client: httpx.Client | None = None, timeout: float = 30.0) -> None:
        self._client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "EvoPi-Updater"},
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def latest_info(self) -> ReleaseInfo:
        try:
            response = self._client.get(_LATEST_RELEASE_API)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            raise DistributionError(f"unable to query the latest EvoPi release: {exc}") from exc
        if not isinstance(payload, dict):
            raise DistributionError("GitHub release response must be an object")
        if payload.get("draft") is not False or payload.get("prerelease") is not False:
            raise DistributionError("latest release is not a stable published release")
        tag = payload.get("tag_name")
        release_url = payload.get("html_url")
        assets = payload.get("assets")
        if not isinstance(tag, str) or not isinstance(release_url, str) or not isinstance(assets, list):
            raise DistributionError("GitHub release response is missing required fields")
        version = parse_stable_tag(tag)
        wheel_name = f"evopi-{version}-py3-none-any.whl"
        by_name: dict[str, str] = {}
        provenance_url: str | None = None
        for asset in assets:
            if not isinstance(asset, dict):
                raise DistributionError("GitHub release asset must be an object")
            name = asset.get("name")
            url = asset.get("browser_download_url")
            if not isinstance(name, str) or not isinstance(url, str):
                raise DistributionError("GitHub release asset is missing fields")
            if name in by_name:
                raise DistributionError(f"duplicate release asset: {name}")
            by_name[name] = url
            if name.endswith(".intoto.jsonl"):
                provenance_url = url
        if wheel_name not in by_name or "SHA256SUMS" not in by_name:
            raise DistributionError("release is missing the wheel or SHA256SUMS asset")
        for url in (release_url, by_name[wheel_name], by_name["SHA256SUMS"]):
            _validate_asset_url(url)
        if provenance_url is not None:
            _validate_asset_url(provenance_url)
        checksum = self._fetch_checksum(by_name["SHA256SUMS"], wheel_name)
        return ReleaseInfo(
            version=version,
            release_url=release_url,
            wheel_name=wheel_name,
            wheel_url=by_name[wheel_name],
            sha256=checksum,
            checksum_url=by_name["SHA256SUMS"],
            provenance_url=provenance_url,
        )

    def _fetch_checksum(self, url: str, wheel_name: str) -> str:
        try:
            response = self._client.get(url)
            response.raise_for_status()
            _validate_download_response_url(str(response.url))
        except httpx.HTTPError as exc:
            raise DistributionError(f"unable to download SHA256SUMS: {exc}") from exc
        matches: list[str] = []
        for line in response.text.splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and parts[1].lstrip("*") == wheel_name:
                matches.append(parts[0].lower())
        if len(matches) != 1 or _SHA256.fullmatch(matches[0]) is None:
            raise DistributionError("SHA256SUMS does not contain one valid wheel digest")
        return matches[0]

    def download(self, info: ReleaseInfo) -> bytes:
        _validate_asset_url(info.wheel_url)
        try:
            response = self._client.get(info.wheel_url)
            response.raise_for_status()
            _validate_download_response_url(str(response.url))
        except httpx.HTTPError as exc:
            raise DistributionError(f"unable to download EvoPi wheel: {exc}") from exc
        wheel = response.content
        digest = hashlib.sha256(wheel).hexdigest()
        if digest != info.sha256:
            raise DistributionError("EvoPi wheel SHA-256 does not match SHA256SUMS")
        _, metadata_version = _wheel_metadata_version(wheel)
        if metadata_version != info.version:
            raise DistributionError("wheel metadata version does not match the Release tag")
        return wheel

    def fetch_latest(self) -> tuple[ReleaseInfo, bytes]:
        info = self.latest_info()
        return info, self.download(info)


__all__ = ["GitHubReleaseClient", "parse_stable_tag", "version_key"]
