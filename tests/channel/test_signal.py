from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect


def _url(session_id: str, role: str, token: str) -> str:
    return f"/ws/signal?session_id={session_id}&role={role}&token={token}"


def test_websocket_forwards_offer_answer_and_ice(client, create_pair, join_pair) -> None:
    created = create_pair("mobile", "iPhone")
    joined = join_pair(created["code"], "desktop", "MacBook")
    session_id = created["session_id"]

    with client.websocket_connect(_url(session_id, "mobile", created["token"])) as mobile:
        ready = mobile.receive_json()
        assert ready["type"] == "signal.ready"
        assert ready["session_id"] == session_id
        assert ready["payload"]["peer_online"] is False
        assert ready["payload"]["transport"] == "direct"

        with client.websocket_connect(_url(session_id, "desktop", joined["token"])) as desktop:
            ready_desktop = desktop.receive_json()
            assert ready_desktop["type"] == "signal.ready"
            assert ready_desktop["payload"]["peer_online"] is True
            assert ready_desktop["payload"]["peer_role"] == "mobile"

            peer_joined = mobile.receive_json()
            assert peer_joined["type"] == "signal.peer_joined"
            assert peer_joined["from"] == "desktop"

            offer_payload = {"type": "offer", "sdp": "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\n"}
            mobile.send_json({"type": "signal.offer", "payload": offer_payload})
            forwarded = desktop.receive_json()
            assert forwarded["type"] == "signal.offer"
            assert forwarded["from"] == "mobile"
            assert forwarded["session_id"] == session_id
            assert forwarded["seq"] == 1
            assert forwarded["payload"] == offer_payload

            answer_payload = {"type": "answer", "sdp": "v=0\r\n"}
            desktop.send_json({"type": "signal.answer", "payload": answer_payload})
            answer = mobile.receive_json()
            assert answer["type"] == "signal.answer"
            assert answer["from"] == "desktop"
            assert answer["seq"] == 2
            assert answer["payload"] == answer_payload

            ice_payload = {"candidate": "candidate:1 1 udp 2122260223 192.168.1.2 5000 typ host"}
            mobile.send_json({"type": "signal.ice", "payload": ice_payload})
            ice = desktop.receive_json()
            assert ice["type"] == "signal.ice"
            assert ice["seq"] == 3
            assert ice["payload"] == ice_payload

            # 服务端不解析 SDP：原有字段（含中文与嵌套结构）原样保留
            nested = {"type": "offer", "sdp": "测试", "meta": {"a": [1, 2, 3]}}
            mobile.send_json({"type": "signal.offer", "payload": nested})
            assert desktop.receive_json()["payload"] == nested


def test_offer_without_peer_returns_error_event_but_keeps_socket(
    client, create_pair
) -> None:
    created = create_pair("mobile")
    with client.websocket_connect(
        _url(created["session_id"], "mobile", created["token"])
    ) as websocket:
        assert websocket.receive_json()["type"] == "signal.ready"

        websocket.send_json({"type": "signal.offer", "payload": {"sdp": "v=0"}})
        error = websocket.receive_json()
        assert error["type"] == "signal.error"
        assert error["ok"] is False
        assert error["code"] == "peer_offline"
        assert "对端" in error["error"]

        # 连接没有被静默断开，心跳仍然可用
        websocket.send_json({"type": "ping"})
        assert websocket.receive_json()["type"] == "signal.pong"


def test_peer_disconnect_notifies_other_side(client, create_pair, join_pair) -> None:
    created = create_pair("mobile")
    joined = join_pair(created["code"], "desktop")
    session_id = created["session_id"]

    with client.websocket_connect(_url(session_id, "mobile", created["token"])) as mobile:
        assert mobile.receive_json()["type"] == "signal.ready"

        with client.websocket_connect(_url(session_id, "desktop", joined["token"])) as desktop:
            assert desktop.receive_json()["type"] == "signal.ready"
            assert mobile.receive_json()["type"] == "signal.peer_joined"

        left = mobile.receive_json()
        assert left["type"] == "signal.peer_left"
        assert left["from"] == "desktop"
        assert left["payload"]["role"] == "desktop"

        mobile.send_json({"type": "signal.offer", "payload": {"sdp": "v=0"}})
        error = mobile.receive_json()
        assert error["code"] == "peer_offline"


def test_unknown_event_returns_chinese_error(client, create_pair) -> None:
    created = create_pair()
    with client.websocket_connect(
        _url(created["session_id"], "mobile", created["token"])
    ) as websocket:
        assert websocket.receive_json()["type"] == "signal.ready"

        websocket.send_json({"type": "signal.unknown"})
        error = websocket.receive_json()
        assert error["code"] == "unknown_event"
        assert "不支持" in error["error"]

        websocket.send_text("this-is-not-json")
        assert websocket.receive_json()["code"] == "bad_json"

        websocket.send_json({"type": "signal.offer", "payload": 123})
        assert websocket.receive_json()["code"] == "bad_event"


def test_invalid_token_is_reported_then_closed(client, create_pair) -> None:
    created = create_pair()
    with client.websocket_connect(
        _url(created["session_id"], "mobile", "wrong-token")
    ) as websocket:
        error = websocket.receive_json()
        assert error["type"] == "signal.error"
        assert error["ok"] is False
        assert error["code"] == "token_invalid"
        assert error["error"] == "会话令牌无效"
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()


def test_missing_query_params_is_reported(client) -> None:
    with client.websocket_connect("/ws/signal") as websocket:
        error = websocket.receive_json()
        assert error["type"] == "signal.error"
        assert error["code"] == "token_required"
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()


def test_heartbeat_timeout_sends_error_event(client, create_pair, service) -> None:
    service.heartbeat_timeout = 0.2
    created = create_pair()
    with client.websocket_connect(
        _url(created["session_id"], "mobile", created["token"])
    ) as websocket:
        assert websocket.receive_json()["type"] == "signal.ready"
        error = websocket.receive_json()
        assert error["type"] == "signal.error"
        assert error["code"] == "heartbeat_timeout"
        assert error["error"] == "心跳超时，连接已断开"
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()


def test_revoke_notifies_open_socket(client, create_pair) -> None:
    created = create_pair()
    with client.websocket_connect(
        _url(created["session_id"], "mobile", created["token"])
    ) as websocket:
        assert websocket.receive_json()["type"] == "signal.ready"

        response = client.post(
            f"/channel/pair/{created['session_id']}/revoke",
            json={"token": created["token"]},
        )
        assert response.status_code == 200

        closed = websocket.receive_json()
        assert closed["type"] == "signal.session_closed"
        assert closed["payload"]["reason"] == "revoked_by_mobile"
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()


def test_session_status_reports_online_flags(client, create_pair, join_pair) -> None:
    created = create_pair()
    joined = join_pair(created["code"])
    session_id = created["session_id"]

    with client.websocket_connect(_url(session_id, "mobile", created["token"])) as mobile:
        assert mobile.receive_json()["type"] == "signal.ready"
        with client.websocket_connect(_url(session_id, "desktop", joined["token"])) as desktop:
            assert desktop.receive_json()["type"] == "signal.ready"
            assert mobile.receive_json()["type"] == "signal.peer_joined"

            status = client.get(f"/channel/pair/{session_id}").json()
            assert status["state"] == "connected"
            assert status["connection"]["initiator_online"] is True
            assert status["connection"]["joiner_online"] is True


def test_payload_too_large_is_rejected(client, create_pair, join_pair, service) -> None:
    service.max_signal_bytes = 64
    created = create_pair()
    joined = join_pair(created["code"])
    session_id = created["session_id"]

    with client.websocket_connect(_url(session_id, "mobile", created["token"])) as mobile:
        assert mobile.receive_json()["type"] == "signal.ready"
        with client.websocket_connect(_url(session_id, "desktop", joined["token"])) as desktop:
            assert desktop.receive_json()["type"] == "signal.ready"
            assert mobile.receive_json()["type"] == "signal.peer_joined"

            mobile.send_json({"type": "signal.offer", "payload": {"sdp": "x" * 200}})
            error = mobile.receive_json()
            assert error["code"] == "payload_too_large"
