from __future__ import annotations

import json
from pathlib import Path

import pytest

from evopi.remote import RemoteFrameCodec, RemoteProtocolError


FIXTURES = Path(__file__).parent / "remote_v1" / "frames.json"


def test_remote_v1_canonical_frames_match_python_codec() -> None:
    fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))

    for payload in fixtures["valid"]:
        frame = RemoteFrameCodec.decode(json.dumps(payload, separators=(",", ":")))
        assert json.loads(RemoteFrameCodec.encode(frame)) == payload

    for payload in fixtures["invalid"]:
        with pytest.raises(RemoteProtocolError):
            RemoteFrameCodec.decode(json.dumps(payload, separators=(",", ":")))
