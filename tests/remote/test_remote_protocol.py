from __future__ import annotations

import pytest

from evopi.remote import RemoteFrameCodec, RemoteProtocolError


def test_remote_frame_codec_is_strict_and_rejects_duplicate_keys() -> None:
    frame = RemoteFrameCodec.decode(
        '{"schema_version":1,"type":"auth.begin","request_id":"r1",'
        '"data":{"device_id":"d1"}}'
    )
    assert frame.type == "auth.begin"
    assert frame.data == {"device_id": "d1"}

    with pytest.raises(RemoteProtocolError, match="duplicate"):
        RemoteFrameCodec.decode(
            '{"schema_version":1,"type":"auth.begin","type":"auth.complete",'
            '"request_id":"r1","data":{}}'
        )


def test_remote_frame_codec_rejects_large_frames_and_non_json_values() -> None:
    with pytest.raises(RemoteProtocolError, match="128 KiB"):
        RemoteFrameCodec.decode("x" * (128 * 1024 + 1))
    with pytest.raises(RemoteProtocolError, match="JSON"):
        RemoteFrameCodec.decode(
            '{"schema_version":1,"type":"auth.begin","request_id":"r1",'
            '"data":{"value":NaN}}'
        )
