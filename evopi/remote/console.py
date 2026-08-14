"""Packaged, opt-in static Remote console with strict response headers."""

from __future__ import annotations

from importlib.resources import files

_CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
}

SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self' wss:; img-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "cache-control": "no-store",
}


def console_asset(name: str) -> tuple[bytes, str]:
    if name not in _CONTENT_TYPES:
        raise FileNotFoundError(name)
    resource = files("evopi.remote").joinpath("console", name)
    return resource.read_bytes(), _CONTENT_TYPES[name]


__all__ = ["SECURITY_HEADERS", "console_asset"]
