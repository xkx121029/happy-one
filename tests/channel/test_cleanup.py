from __future__ import annotations

import asyncio

import pytest

from happyone.channel.service import ChannelError, PairingService


def _pair(service: PairingService):
    created = service.create_pair("mobile", "iPhone")
    joined = service.join_pair(created.code, "desktop", "MacBook")
    return created, joined


def test_sweep_expires_unused_code_and_session(service, clock) -> None:
    created = service.create_pair("mobile")
    clock.advance(601)

    report = service.sweep()

    assert report.expired_codes == 1
    assert report.expired_sessions == 1
    with pytest.raises(ChannelError) as exc:
        service.join_pair(created.code, "desktop")
    assert exc.value.code == "code_not_found"
    # 会话在保留期内仍可查询，但状态已经是 expired
    assert service.get_status(created.session_id).state == "expired"

    # 再跑一次清理应当是幂等的
    again = service.sweep()
    assert again.as_dict() == {
        "expired_codes": 0,
        "expired_sessions": 0,
        "dropped_connections": 0,
        "purged_sessions": 0,
    }


def test_sweep_idle_paired_session_expires(service, clock) -> None:
    created, _ = _pair(service)
    clock.advance(1801)

    report = service.sweep()

    assert report.expired_sessions == 1
    status = service.get_status(created.session_id)
    assert status.state == "expired"
    assert status.close_reason == "ttl_expired"


def test_sweep_purges_closed_session_after_retention(service, clock) -> None:
    created, _ = _pair(service)
    service.revoke(created.session_id, created.token)
    clock.advance(601)

    report = service.sweep()

    assert report.purged_sessions == 1
    with pytest.raises(ChannelError):
        service.get_status(created.session_id)


def test_sweep_drops_stale_connections_and_notifies_peer(service, clock) -> None:
    created, joined = _pair(service)
    mobile_queue: asyncio.Queue = asyncio.Queue()
    desktop_queue: asyncio.Queue = asyncio.Queue()

    mobile = service.attach(created.session_id, "mobile", created.token, mobile_queue)
    desktop = service.attach(created.session_id, "desktop", joined.token, desktop_queue)
    assert mobile.ready_event["payload"]["peer_online"] is False
    assert desktop.ready_event["payload"]["peer_online"] is True
    assert desktop.session.state == "connected"

    clock.advance(service.heartbeat_timeout * 2 + 1)
    report = service.sweep()

    assert report.dropped_connections == 2
    first = mobile_queue.get_nowait()
    assert first["type"] == "signal.error"
    assert first["code"] == "heartbeat_timeout"
    assert mobile_queue.get_nowait() is None
    # 对端会收到 peer_left
    peer_events = [desktop_queue.get_nowait(), desktop_queue.get_nowait()]
    assert any(event["type"] == "signal.peer_left" for event in peer_events)


def test_online_session_is_not_swept(clock) -> None:
    # 心跳阈值放大，确保本用例只验证「在线会话不被 TTL 回收」
    service = PairingService(clock=clock, heartbeat_timeout=3600.0)
    created, _ = _pair(service)
    service.attach(created.session_id, "mobile", created.token, asyncio.Queue())

    clock.advance(1801)
    report = service.sweep()

    assert report.expired_sessions == 0
    assert service.get_status(created.session_id).state == "paired"


def test_heartbeat_touch_keeps_session_alive(service, clock) -> None:
    created, joined = _pair(service)
    service.attach(created.session_id, "mobile", created.token, asyncio.Queue())
    service.attach(created.session_id, "desktop", joined.token, asyncio.Queue())

    clock.advance(1500)
    service.touch(created.session_id, "mobile")
    clock.advance(400)  # 已超过原始 expires_at，但心跳滑动了 TTL
    assert service.get_status(created.session_id).state == "connected"


def test_max_sessions_guard(clock) -> None:
    service = PairingService(clock=clock, max_sessions=2)
    service.create_pair("mobile")
    service.create_pair("desktop")

    with pytest.raises(ChannelError) as exc:
        service.create_pair("mobile")
    assert exc.value.code == "too_many_sessions"


def test_route_signal_requires_dict_payload(service) -> None:
    created, joined = _pair(service)
    service.attach(created.session_id, "mobile", created.token, asyncio.Queue())
    service.attach(created.session_id, "desktop", joined.token, asyncio.Queue())

    with pytest.raises(ChannelError) as exc:
        service.route_signal(created.session_id, "mobile", "signal.offer", "not-a-dict")
    assert exc.value.code == "bad_event"


def test_route_signal_rejects_unknown_event_type(service) -> None:
    created, _ = _pair(service)
    with pytest.raises(ChannelError) as exc:
        service.route_signal(created.session_id, "mobile", "signal.unknown", {})
    assert exc.value.code == "unknown_event"


def test_reusing_same_role_connection_replaces_old_one(service) -> None:
    created, _ = _pair(service)
    first_queue: asyncio.Queue = asyncio.Queue()
    second_queue: asyncio.Queue = asyncio.Queue()

    first = service.attach(created.session_id, "mobile", created.token, first_queue)
    service.attach(created.session_id, "mobile", created.token, second_queue)

    replaced = first_queue.get_nowait()
    assert replaced["code"] == "connection_replaced"
    assert first_queue.get_nowait() is None
    assert second_queue.empty()

    # 旧连接迟到的 detach 不能把新连接标记为离线
    service.detach(
        created.session_id, "mobile", "disconnected", connection=first.connection
    )
    status = service.get_status(created.session_id)
    assert status.initiator.online is True
