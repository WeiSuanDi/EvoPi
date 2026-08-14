from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from evopi.core.events import CoreEvent
from evopi.remote import (
    REMOTE_SUBPROTOCOL,
    RemoteAuditLog,
    RemoteFrame,
    RemoteFrameCodec,
    RemoteGateway,
    RemoteGatewayConfig,
    RemoteHostConfig,
    RemoteHostController,
    RemoteHostStore,
    challenge_from_dict,
    generate_device_key,
    public_jwk_from_private_key,
    sign_auth_challenge,
)
from evopi.rpc.codec_v2 import decode_v2_envelope, encode_v2_request
from evopi.rpc.event_stream import EventStream
from evopi.rpc.protocol_v2 import RpcV2Event, RpcV2Request, RpcV2Response


class _RpcHost:
    async def initialize(self, params: dict[str, object]) -> dict[str, object]:
        del params
        return {
            "protocol": "evopi.rpc.v2",
            "schema_version": 2,
            "host_id": "host-1",
            "session_id": "session-1",
            "stream": {
                "stream_id": "stream-1",
                "cursor": 0,
                "oldest_sequence": 0,
                "latest_sequence": 0,
                "capacity": 1000,
            },
            "active_tool_names": [],
            "policy_names": [],
            "capabilities": {
                "event_replay": True,
                "confirmation": True,
                "text_steering": True,
                "text_follow_up": True,
            },
            "steering_mode": "one-at-a-time",
            "follow_up_mode": "one-at-a-time",
        }

    async def runtime_status(self, params: dict[str, object]) -> dict[str, object]:
        del params
        return {
            "active_run_id": None,
            "lifecycle": "idle",
            "session_id": "session-1",
            "pending_confirmation_count": 0,
            "last_end_reason": None,
            "last_run_error": None,
            "steering_mode": "one-at-a-time",
            "follow_up_mode": "one-at-a-time",
            "pending_steering_count": 0,
            "pending_follow_up_count": 0,
        }

    async def run_start(self, params: dict[str, object]) -> dict[str, object]:
        del params
        return {"run_id": "run-1", "start_sequence": 1}

    def __getattr__(self, name: str) -> Any:
        async def empty(params: dict[str, object]) -> dict[str, object]:
            del params
            if name == "events_replay":
                return {
                    "stream_id": "stream-1",
                    "after_sequence": 0,
                    "oldest_sequence": 0,
                    "latest_sequence": 0,
                    "events": [],
                }
            if name == "confirmation_list":
                return {"pending": []}
            raise AssertionError(name)

        return empty


class _StreamingRpcHost(_RpcHost):
    def __init__(self) -> None:
        self.event_stream = EventStream(capacity=200)

    async def initialize(self, params: dict[str, object]) -> dict[str, object]:
        result = await super().initialize(params)
        latest = self.event_stream.latest_sequence
        result["stream"] = {
            "stream_id": self.event_stream.stream_id,
            "cursor": latest,
            "oldest_sequence": 1 if latest else 0,
            "latest_sequence": latest,
            "capacity": 200,
        }
        return result

    def project_event(self, event: Any) -> RpcV2Event:
        return RpcV2Event(
            event_id=event.event_id,
            stream_id=self.event_stream.stream_id,
            sequence=event.sequence,
            type=event.type,
            data=event.data,
            run_id=event.run_id,
            created_at=event.created_at,
        )


def _gateway(
    tmp_path: Path, *, rpc_host: Any | None = None
) -> tuple[RemoteGateway, object, str]:
    store = RemoteHostStore(tmp_path / "remote")
    config = store.initialize(
        RemoteHostConfig(name="test-host", workspace=tmp_path / "workspace")
    )
    controller = RemoteHostController(store, "test-host")
    private_key = generate_device_key()
    code = controller.issue_pairing_code()
    request = controller.submit_pairing(
        code=code.code,
        device_name="browser",
        public_jwk=public_jwk_from_private_key(private_key),
    )
    device = controller.approve(request.request_id, scopes=["control"])
    gateway = RemoteGateway(
        host_config=config,
        controller=controller,
        rpc_host=rpc_host or _RpcHost(),
        gateway_config=RemoteGatewayConfig(
            bind="127.0.0.1", allowed_hosts=("testserver",)
        ),
        audit=RemoteAuditLog(tmp_path / "audit"),
    )
    return gateway, private_key, device.device_id


def _send_frame(socket: Any, frame_type: str, request_id: str, data: dict[str, object]) -> None:
    socket.send_text(
        RemoteFrameCodec.encode(
            RemoteFrame(type=frame_type, request_id=request_id, data=data)
        )
    )


def _authenticate(socket: Any, private_key: object, device_id: str) -> None:
    _send_frame(socket, "auth.begin", "auth-1", {"device_id": device_id})
    response = RemoteFrameCodec.decode(socket.receive_text())
    challenge = challenge_from_dict(response.data["challenge"])
    signature = sign_auth_challenge(private_key, challenge)
    _send_frame(socket, "auth.complete", "auth-2", {"signature": signature})
    assert RemoteFrameCodec.decode(socket.receive_text()).type == "auth.ok"


def test_gateway_authenticates_then_requires_lease_for_run(tmp_path: Path) -> None:
    gateway, private_key, device_id = _gateway(tmp_path)
    client = TestClient(gateway.create_app(), client=("127.0.0.1", 50000))
    with client.websocket_connect(
        "/v1/connect", subprotocols=[REMOTE_SUBPROTOCOL]
    ) as socket:
        _authenticate(socket, private_key, device_id)
        socket.send_text(
            encode_v2_request(
                RpcV2Request(
                    request_id="initialize-1",
                    method="initialize",
                    params={"client_name": "tests", "client_version": "1.0"},
                )
            )
        )
        initialized = decode_v2_envelope(socket.receive_text())
        assert isinstance(initialized, RpcV2Response) and initialized.ok

        socket.send_text(
            encode_v2_request(
                RpcV2Request(
                    request_id="run-1",
                    method="run.start",
                    params={"prompt": "hello"},
                )
            )
        )
        denied = decode_v2_envelope(socket.receive_text())
        assert isinstance(denied, RpcV2Response)
        assert denied.error is not None and denied.error.code == "remote_lease_required"

        _send_frame(socket, "lease.acquire", "lease-1", {})
        assert RemoteFrameCodec.decode(socket.receive_text()).type == "lease.granted"
        socket.send_text(
            encode_v2_request(
                RpcV2Request(
                    request_id="run-2",
                    method="run.start",
                    params={"prompt": "hello"},
                )
            )
        )
        started = decode_v2_envelope(socket.receive_text())
        assert isinstance(started, RpcV2Response) and started.ok


def test_gateway_rejects_wrong_subprotocol(tmp_path: Path) -> None:
    gateway, _, _ = _gateway(tmp_path)
    client = TestClient(gateway.create_app(), client=("127.0.0.1", 50000))
    try:
        with client.websocket_connect("/v1/connect", subprotocols=["wrong"]):
            raise AssertionError("connection should not be accepted")
    except Exception as exc:
        assert "1008" in str(exc) or type(exc).__name__ == "WebSocketDisconnect"


def test_gateway_rejects_rpc_before_authentication(tmp_path: Path) -> None:
    gateway, _, _ = _gateway(tmp_path)
    client = TestClient(gateway.create_app(), client=("127.0.0.1", 50000))
    with client.websocket_connect(
        "/v1/connect", subprotocols=[REMOTE_SUBPROTOCOL]
    ) as socket:
        socket.send_text(
            encode_v2_request(
                RpcV2Request(
                    request_id="initialize-before-auth",
                    method="initialize",
                    params={"client_name": "tests", "client_version": "1.0"},
                )
            )
        )
        error = RemoteFrameCodec.decode(socket.receive_text())
        assert error.type == "error"
        assert error.data["code"] == "protocol"


def test_gateway_health_is_local_and_reports_audit_readiness(tmp_path: Path) -> None:
    gateway, _, _ = _gateway(tmp_path)
    client = TestClient(gateway.create_app(), client=("127.0.0.1", 50000))
    assert client.get("/health/live").json() == {"status": "live"}
    assert client.get("/health/ready").json() == {"status": "ready"}

    gateway.ready = False
    response = client.get("/health/ready")
    assert response.status_code == 503


def test_gateway_streams_live_events_after_rpc_initialize(tmp_path: Path) -> None:
    host = _StreamingRpcHost()
    gateway, private_key, device_id = _gateway(tmp_path, rpc_host=host)
    client = TestClient(gateway.create_app(), client=("127.0.0.1", 50000))
    with client.websocket_connect(
        "/v1/connect", subprotocols=[REMOTE_SUBPROTOCOL]
    ) as socket:
        _authenticate(socket, private_key, device_id)
        socket.send_text(
            encode_v2_request(
                RpcV2Request(
                    request_id="initialize-live",
                    method="initialize",
                    params={"client_name": "tests", "client_version": "1.0"},
                )
            )
        )
        assert isinstance(decode_v2_envelope(socket.receive_text()), RpcV2Response)
        host.event_stream.publish(CoreEvent(type="turn_start", data={"turn": 1}))
        event = decode_v2_envelope(socket.receive_text())
        assert isinstance(event, RpcV2Event)
        assert event.type == "turn_start"


def test_gateway_event_pages_are_bounded_to_one_hundred(tmp_path: Path) -> None:
    host = _StreamingRpcHost()
    for index in range(105):
        host.event_stream.publish(CoreEvent(type="turn_start", data={"turn": index}))
    gateway, private_key, device_id = _gateway(tmp_path, rpc_host=host)
    client = TestClient(gateway.create_app(), client=("127.0.0.1", 50000))
    with client.websocket_connect(
        "/v1/connect", subprotocols=[REMOTE_SUBPROTOCOL]
    ) as socket:
        _authenticate(socket, private_key, device_id)
        _send_frame(
            socket,
            "events.page",
            "page-1",
            {"stream_id": host.event_stream.stream_id, "after_sequence": 0},
        )
        page = RemoteFrameCodec.decode(socket.receive_text())
        assert len(page.data["events"]) == 100
        assert page.data["complete"] is False
        assert page.data["next_sequence"] == 100


def test_console_is_opt_in_and_has_strict_security_headers(tmp_path: Path) -> None:
    gateway, _, _ = _gateway(tmp_path)
    client = TestClient(gateway.create_app(), client=("127.0.0.1", 50000))
    assert client.get("/").status_code == 404

    gateway.config = RemoteGatewayConfig(
        bind="127.0.0.1",
        allowed_hosts=("testserver",),
        console_enabled=True,
    )
    client = TestClient(gateway.create_app(), client=("127.0.0.1", 50000))
    response = client.get("/")
    assert response.status_code == 200
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    script = client.get("/console/app.js").text
    assert "innerHTML" not in script
