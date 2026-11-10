"""通道层核心逻辑：取件码配对、会话状态机、信令路由。

设计约束（与项目整体约定一致）：
- 不 import fastapi，HTTP / WebSocket 适配全部在 router.py；
- 会话与连接状态只放进程内存（单进程，≤20 人），不写 SQLite，不引入 Redis；
- 所有异常都转成 ChannelError，由 router 统一输出中文结构化错误；
- 中继（TURN）只保留标记位与接口占位，本版本不实现转发。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .models import (
    ERROR_MESSAGES,
    FORWARDABLE_EVENTS,
    ConnectionView,
    PairCreateResponse,
    PairJoinResponse,
    PairStatusResponse,
    PeerView,
    RevokeResponse,
    ServerEvent,
)

#: 去掉 0/O/1/I/L 等易混字符，31 个字符 × 6 位，足够 20 人场景。
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 6
#: 取件码有效期 10 分钟。
PAIR_CODE_TTL = 600.0
#: 配对成功后会话空闲 TTL 30 分钟（有信令或心跳会滑动续期）。
SESSION_TTL = 1800.0
#: 单条 WebSocket 消息的等待上限，超时即判定半开连接。
HEARTBEAT_TIMEOUT = 30.0
#: 内存清理任务间隔。
SWEEP_INTERVAL = 30.0
MAX_SESSIONS = 64
MAX_SIGNAL_BYTES = 64 * 1024
#: 已结束会话保留一段时间，方便客户端补查状态。
CLOSED_RETENTION = 600.0
#: 中继兜底占位：对称 NAT 下直连必然失败，未来接 TURN 时置 True 并接 relay 实现。
RELAY_SUPPORTED = False

ROLES: tuple[str, ...] = ("mobile", "desktop")
TERMINAL_STATES: tuple[str, ...] = ("closed", "expired")


class ChannelError(Exception):
    """通道层内部统一异常，router 负责转成结构化响应。"""

    def __init__(self, code: str, message: str | None = None, status: int = 400) -> None:
        self.code = code
        self.message = message or ERROR_MESSAGES.get(code, ERROR_MESSAGES["internal"])
        self.status = status
        super().__init__(self.message)


@dataclass
class PeerSlot:
    role: str
    device_name: str | None
    token: str
    online: bool = False
    connected_at: float | None = None


@dataclass
class PairCode:
    code: str
    session_id: str
    created_at: float
    expires_at: float
    used_at: float | None = None


@dataclass
class Session:
    session_id: str
    direction: str
    state: str
    created_at: float
    expires_at: float
    initiator: PeerSlot
    joiner: PeerSlot | None = None
    transport: str = "direct"
    seq: int = 0
    paired_at: float | None = None
    closed_at: float | None = None
    close_reason: str | None = None

    def slot(self, role: str) -> PeerSlot | None:
        if role == self.initiator.role:
            return self.initiator
        if self.joiner is not None and role == self.joiner.role:
            return self.joiner
        return None

    def slots(self) -> list[PeerSlot]:
        return [slot for slot in (self.initiator, self.joiner) if slot is not None]


@dataclass
class Connection:
    """一条已注册的 WebSocket 连接。

    queue 由 router 创建，service 只负责投递事件（put_nowait）；
    真正的发送与关流由 router 的 sender 任务串行完成：
    队列收到 None 时先把已排队事件发完，再关闭连接。
    """

    session_id: str
    role: str
    queue: "asyncio.Queue[dict[str, Any] | None]"
    connected_at: float
    last_seen: float
    closed: bool = False


@dataclass
class AttachResult:
    session: Session
    connection: Connection
    peer_conn: Connection | None
    ready_event: dict[str, Any]
    peer_notice: dict[str, Any] | None


@dataclass
class DetachResult:
    connection: Connection | None
    peer_conn: Connection | None
    peer_event: dict[str, Any] | None


@dataclass
class SweepReport:
    expired_codes: int = 0
    expired_sessions: int = 0
    dropped_connections: int = 0
    purged_sessions: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "expired_codes": self.expired_codes,
            "expired_sessions": self.expired_sessions,
            "dropped_connections": self.dropped_connections,
            "purged_sessions": self.purged_sessions,
        }


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _clean_device_name(value: str | None) -> str | None:
    if value is None:
        return None
    name = value.strip()
    return name[:64] if name else None


def _direction_for(role: str) -> str:
    return "mobile_to_desktop" if role == "mobile" else "desktop_to_mobile"


def _normalize_code(raw: str | None) -> str:
    if raw is None:
        return ""
    return raw.strip().upper().replace("-", "").replace(" ", "")


class PairingService:
    """配对与信令的纯逻辑实现（同步方法，便于直接单测）。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        pair_ttl: float = PAIR_CODE_TTL,
        session_ttl: float = SESSION_TTL,
        heartbeat_timeout: float = HEARTBEAT_TIMEOUT,
        sweep_interval: float = SWEEP_INTERVAL,
        max_sessions: int = MAX_SESSIONS,
        max_signal_bytes: int = MAX_SIGNAL_BYTES,
        code_length: int = CODE_LENGTH,
        audit: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._clock = clock
        self.pair_ttl = float(pair_ttl)
        self.session_ttl = float(session_ttl)
        self.heartbeat_timeout = float(heartbeat_timeout)
        self.sweep_interval = float(sweep_interval)
        self.max_sessions = int(max_sessions)
        self.max_signal_bytes = int(max_signal_bytes)
        self.code_length = int(code_length)
        self.audit = audit
        self._sessions: dict[str, Session] = {}
        self._codes: dict[str, PairCode] = {}
        self._conns: dict[tuple[str, str], Connection] = {}
        #: 由 router 懒启动的 TTL 清理任务句柄。
        self.reaper_task: "asyncio.Task[None] | None" = None

    # ------------------------------------------------------------------ 配对

    def create_pair(self, role: str, device_name: str | None = None) -> PairCreateResponse:
        role = self._validate_role(role)
        now = self._now()
        self.sweep(now)
        if self._active_session_count() >= self.max_sessions:
            raise ChannelError("too_many_sessions", status=503)
        code = self._new_code()
        session_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(24)
        expires_at = now + self.pair_ttl
        session = Session(
            session_id=session_id,
            direction=_direction_for(role),
            state="waiting_peer",
            created_at=now,
            expires_at=expires_at,
            initiator=PeerSlot(role=role, device_name=_clean_device_name(device_name), token=token),
        )
        self._sessions[session_id] = session
        self._codes[code] = PairCode(
            code=code, session_id=session_id, created_at=now, expires_at=expires_at
        )
        self._audit("pair_created", session, {"code": code})
        return PairCreateResponse(
            code=code,
            session_id=session_id,
            token=token,
            role=role,  # type: ignore[arg-type]
            direction=session.direction,  # type: ignore[arg-type]
            state=session.state,  # type: ignore[arg-type]
            transport=session.transport,  # type: ignore[arg-type]
            relay_available=RELAY_SUPPORTED,
            expires_at=_iso(expires_at),
            expires_in=max(0, int(expires_at - now)),
        )

    def join_pair(
        self, code: str, role: str, device_name: str | None = None
    ) -> PairJoinResponse:
        role = self._validate_role(role)
        normalized = _normalize_code(code)
        if len(normalized) != self.code_length:
            raise ChannelError("code_format")
        now = self._now()
        record = self._codes.get(normalized)
        if record is None:
            raise ChannelError("code_not_found", status=404)
        if record.used_at is not None:
            raise ChannelError("code_used", status=409)
        if now >= record.expires_at:
            session = self._sessions.get(record.session_id)
            if session is not None:
                self._terminate(session, now, "expired", "code_expired")
            raise ChannelError("code_expired", status=410)

        session = self._require_session(record.session_id)
        self._maybe_expire(session, now)
        if session.state == "expired":
            raise ChannelError("code_expired", status=410)
        if session.state == "closed":
            raise ChannelError("session_closed", status=409)
        if session.joiner is not None or session.state != "waiting_peer":
            raise ChannelError("code_used", status=409)
        if role == session.initiator.role:
            raise ChannelError("role_conflict", status=409)

        token = secrets.token_urlsafe(24)
        session.joiner = PeerSlot(
            role=role, device_name=_clean_device_name(device_name), token=token
        )
        session.paired_at = now
        # 只有双方都建立 WebSocket 才算 connected，这里先进入 paired
        session.state = "paired"
        session.expires_at = max(session.expires_at, now + self.session_ttl)
        record.used_at = now
        self._audit("pair_joined", session, {"code": normalized})
        return PairJoinResponse(
            session_id=session.session_id,
            token=token,
            role=role,  # type: ignore[arg-type]
            direction=session.direction,  # type: ignore[arg-type]
            state=session.state,  # type: ignore[arg-type]
            transport=session.transport,  # type: ignore[arg-type]
            relay_available=RELAY_SUPPORTED,
            peer=self._peer_view(session.initiator),
            expires_at=_iso(session.expires_at),
            expires_in=max(0, int(session.expires_at - now)),
        )

    def get_status(self, session_id: str) -> PairStatusResponse:
        session = self._require_session(session_id)
        now = self._now()
        self._maybe_expire(session, now)
        initiator_online = self._peer_online(session, session.initiator.role)
        joiner_online = bool(session.joiner and self._peer_online(session, session.joiner.role))
        return PairStatusResponse(
            session_id=session.session_id,
            state=session.state,  # type: ignore[arg-type]
            direction=session.direction,  # type: ignore[arg-type]
            transport=session.transport,  # type: ignore[arg-type]
            relay_available=RELAY_SUPPORTED,
            initiator=self._peer_view(session.initiator),
            joiner=self._peer_view(session.joiner) if session.joiner else None,
            connection=ConnectionView(
                initiator_online=initiator_online,
                joiner_online=joiner_online,
                transport=session.transport,  # type: ignore[arg-type]
                relay_available=RELAY_SUPPORTED,
            ),
            created_at=_iso(session.created_at),
            expires_at=_iso(session.expires_at),
            expires_in=max(0, int(session.expires_at - now)),
            close_reason=session.close_reason,
        )

    def revoke(self, session_id: str, token: str | None = None) -> RevokeResponse:
        session = self._require_session(session_id)
        if not token:
            raise ChannelError("token_required", status=403)
        actor = next(
            (slot.role for slot in session.slots() if hmac.compare_digest(slot.token, token)),
            None,
        )
        if actor is None:
            raise ChannelError("token_invalid", status=403)
        now = self._now()
        if session.state in TERMINAL_STATES:
            return RevokeResponse(
                session_id=session.session_id,
                state=session.state,  # type: ignore[arg-type]
                notified=[],
            )
        notified = self._terminate(session, now, "closed", f"revoked_by_{actor}")
        return RevokeResponse(
            session_id=session.session_id,
            state="closed",
            notified=notified,  # type: ignore[arg-type]
        )

    # -------------------------------------------------------------- 连接管理

    def attach(
        self,
        session_id: str,
        role: str,
        token: str,
        queue: "asyncio.Queue[dict[str, Any] | None]",
    ) -> AttachResult:
        role = self._validate_role(role)
        session = self._require_session(session_id)
        now = self._now()
        self._maybe_expire(session, now)
        self._require_open(session)
        slot = session.slot(role)
        if slot is None:
            raise ChannelError("role_not_in_session", status=403)
        if not token or not hmac.compare_digest(slot.token, token):
            raise ChannelError("token_invalid", status=403)

        old = self._conns.get((session_id, role))
        if old is not None:
            self._drop_connection(
                old, "connection_replaced", reason="connection_replaced", notify_peer=False
            )

        conn = Connection(
            session_id=session_id,
            role=role,
            queue=queue,
            connected_at=now,
            last_seen=now,
        )
        self._conns[(session_id, role)] = conn
        slot.online = True
        slot.connected_at = now
        session.expires_at = max(session.expires_at, now + self.session_ttl)

        peer_slot = self._peer_slot(session, role)
        peer_conn = self._conns.get((session_id, peer_slot.role)) if peer_slot else None
        if peer_conn is not None:
            session.state = "connected"
        elif session.joiner is not None:
            session.state = "paired"
        else:
            session.state = "waiting_peer"

        ready_event = self._event(
            ServerEvent.READY,
            session_id,
            role,
            {
                "role": role,
                "state": session.state,
                "transport": session.transport,
                "relay_available": RELAY_SUPPORTED,
                "peer_online": peer_conn is not None,
                "peer_role": peer_slot.role if peer_slot else None,
            },
        )
        peer_notice = None
        if peer_conn is not None:
            peer_notice = self._event(
                ServerEvent.PEER_JOINED,
                session_id,
                role,
                {"role": role, "state": session.state},
            )
        self._audit("peer_connected", session, {"role": role})
        return AttachResult(
            session=session,
            connection=conn,
            peer_conn=peer_conn,
            ready_event=ready_event,
            peer_notice=peer_notice,
        )

    def detach(
        self,
        session_id: str,
        role: str,
        reason: str = "disconnected",
        *,
        connection: Connection | None = None,
    ) -> DetachResult:
        current = self._conns.get((session_id, role))
        if current is None:
            # 该连接已被替换 / 已被清理，不再产生任何副作用，避免误伤新连接。
            return DetachResult(None, None, None)
        if connection is not None and current is not connection:
            # 旧连接迟到的 detach，绝不能把新连接从注册表里摘掉。
            return DetachResult(None, None, None)
        self._conns.pop((session_id, role), None)
        conn = current
        conn.closed = True
        session = self._sessions.get(session_id)
        if session is None:
            return DetachResult(conn, None, None)
        slot = session.slot(role)
        if slot is not None:
            slot.online = False
        if session.state not in TERMINAL_STATES:
            session.state = "paired" if session.joiner is not None else "waiting_peer"
        peer_conn = None
        peer_event = None
        peer_slot = self._peer_slot(session, role)
        if peer_slot is not None:
            peer_conn = self._conns.get((session_id, peer_slot.role))
            if peer_conn is not None:
                peer_event = self._event(
                    ServerEvent.PEER_LEFT,
                    session_id,
                    role,
                    {"role": role, "reason": reason},
                )
        self._audit("peer_disconnected", session, {"role": role, "reason": reason})
        return DetachResult(conn, peer_conn, peer_event)

    def touch(self, session_id: str, role: str) -> None:
        conn = self._conns.get((session_id, role))
        if conn is None:
            return
        now = self._now()
        conn.last_seen = now
        session = self._sessions.get(session_id)
        if session is not None and session.state not in TERMINAL_STATES:
            session.expires_at = max(session.expires_at, now + self.session_ttl)

    def route_signal(
        self,
        session_id: str,
        from_role: str,
        event_type: str,
        payload: Any,
    ) -> dict[str, Any]:
        """把 offer / answer / ICE 原样转给对端，不解析 SDP 内容。"""
        if event_type not in FORWARDABLE_EVENTS:
            raise ChannelError("unknown_event")
        session = self._require_session(session_id)
        now = self._now()
        self._maybe_expire(session, now)
        self._require_open(session)
        sender = self._conns.get((session_id, from_role))
        if sender is None or sender.closed:
            raise ChannelError("not_connected", status=409)
        if not isinstance(payload, dict):
            raise ChannelError("bad_event")
        size = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if size > self.max_signal_bytes:
            raise ChannelError("payload_too_large", status=413)
        peer_slot = self._peer_slot(session, from_role)
        peer = self._conns.get((session_id, peer_slot.role)) if peer_slot else None
        if peer is None or peer.closed:
            raise ChannelError("peer_offline", status=409)
        session.seq += 1
        event = self._event(event_type, session_id, from_role, payload, seq=session.seq)
        if not self.notify(peer, event):
            raise ChannelError("peer_busy", status=429)
        self.touch(session_id, from_role)
        return event

    def notify(self, conn: Connection | None, event: dict[str, Any] | None) -> bool:
        """把事件放进目标连接的发送队列；连接已关闭或队列已满则返回 False。"""
        if conn is None or event is None or conn.closed:
            return False
        try:
            conn.queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            return False

    # -------------------------------------------------------------- 事件构造

    def _event(
        self,
        event_type: str,
        session_id: str | None,
        from_role: str | None = None,
        payload: dict[str, Any] | None = None,
        seq: int | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "type": event_type,
            "session_id": session_id,
            "ts": round(self._now(), 3),
        }
        if from_role is not None:
            event["from"] = from_role
        if seq is not None:
            event["seq"] = seq
        if payload is not None:
            event["payload"] = payload
        return event

    def pong_event(self, session_id: str) -> dict[str, Any]:
        return self._event(ServerEvent.PONG, session_id)

    def error_event(
        self, session_id: str | None, code: str, message: str | None = None
    ) -> dict[str, Any]:
        event = self._event(ServerEvent.ERROR, session_id)
        event["ok"] = False
        event["code"] = code
        event["error"] = message or ERROR_MESSAGES.get(code, ERROR_MESSAGES["internal"])
        return event

    # ------------------------------------------------------------------ 清理

    def sweep(self, now: float | None = None) -> SweepReport:
        """回收过期取件码、过期会话、半开连接与长时间滞留的已结束会话。"""
        moment = self._now() if now is None else now
        report = SweepReport()

        for code, record in list(self._codes.items()):
            if record.used_at is None and moment >= record.expires_at:
                self._codes.pop(code, None)
                report.expired_codes += 1
            elif record.session_id not in self._sessions:
                self._codes.pop(code, None)

        for session_id, session in list(self._sessions.items()):
            if session.state in TERMINAL_STATES:
                closed_at = session.closed_at if session.closed_at is not None else moment
                if moment >= closed_at + CLOSED_RETENTION:
                    self._sessions.pop(session_id, None)
                    report.purged_sessions += 1
                continue
            if moment >= session.expires_at and not self._session_online(session):
                self._terminate(session, moment, "expired", "ttl_expired")
                report.expired_sessions += 1

        stale_after = max(self.heartbeat_timeout * 2, 1.0)
        for (session_id, role), conn in list(self._conns.items()):
            if moment - conn.last_seen <= stale_after:
                continue
            self._drop_connection(conn, "heartbeat_timeout", reason="heartbeat_timeout")
            report.dropped_connections += 1

        return report

    def _drop_connection(
        self,
        conn: Connection,
        code: str,
        reason: str = "disconnected",
        *,
        notify_peer: bool = True,
    ) -> None:
        """强制下线一条连接：先发错误事件再关队列，并通知对端。"""
        self._conns.pop((conn.session_id, conn.role), None)
        # 必须先投递事件再标记 closed，否则 notify 会被自身拦截
        self.notify(conn, self.error_event(conn.session_id, code))
        conn.closed = True
        try:
            conn.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        session = self._sessions.get(conn.session_id)
        if session is None:
            return
        slot = session.slot(conn.role)
        if slot is not None:
            slot.online = False
        if session.state == "connected":
            session.state = "paired"
        if not notify_peer:
            return
        peer_slot = self._peer_slot(session, conn.role)
        peer_conn = self._conns.get((session.session_id, peer_slot.role)) if peer_slot else None
        if peer_conn is not None:
            self.notify(
                peer_conn,
                self._event(
                    ServerEvent.PEER_LEFT,
                    session.session_id,
                    conn.role,
                    {"role": conn.role, "reason": reason},
                ),
            )

    def _terminate(self, session: Session, now: float, state: str, reason: str) -> list[str]:
        """结束会话：关掉所有连接、作废取件码，返回被通知的角色列表。"""
        session.state = state
        session.close_reason = reason
        session.closed_at = now
        for code, record in list(self._codes.items()):
            if record.session_id == session.session_id:
                self._codes.pop(code, None)
        notified: list[str] = []
        for role in ROLES:
            conn = self._conns.pop((session.session_id, role), None)
            slot = session.slot(role)
            if slot is not None:
                slot.online = False
            if conn is None:
                continue
            self.notify(
                conn,
                self._event(
                    ServerEvent.SESSION_CLOSED,
                    session.session_id,
                    None,
                    {"role": role, "reason": reason},
                ),
            )
            conn.closed = True
            try:
                conn.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            notified.append(role)
        self._audit("session_closed" if state == "closed" else "session_expired", session)
        return notified

    # ------------------------------------------------------------------ 工具

    def _now(self) -> float:
        return float(self._clock())

    def _validate_role(self, role: str | None) -> str:
        value = (role or "").strip().lower()
        if value not in ROLES:
            raise ChannelError("invalid_role")
        return value

    def _new_code(self) -> str:
        for _ in range(50):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(self.code_length))
            if code not in self._codes:
                return code
        raise ChannelError("internal", "取件码生成失败，请重试", status=500)

    def _active_session_count(self) -> int:
        return sum(
            1 for session in self._sessions.values() if session.state not in TERMINAL_STATES
        )

    def _require_session(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise ChannelError("session_not_found", status=404)
        return session

    def _require_open(self, session: Session) -> None:
        if session.state == "closed":
            raise ChannelError("session_closed", status=409)
        if session.state == "expired":
            raise ChannelError("session_expired", status=410)

    def _maybe_expire(self, session: Session, now: float) -> None:
        if session.state in TERMINAL_STATES:
            return
        if now >= session.expires_at and not self._session_online(session):
            self._terminate(session, now, "expired", "ttl_expired")

    def _peer_slot(self, session: Session, role: str) -> PeerSlot | None:
        if session.joiner is None:
            return None
        if role == session.initiator.role:
            return session.joiner
        if role == session.joiner.role:
            return session.initiator
        return None

    def _peer_online(self, session: Session, role: str) -> bool:
        peer_slot = self._peer_slot(session, role)
        if peer_slot is None:
            return False
        conn = self._conns.get((session.session_id, peer_slot.role))
        return conn is not None and not conn.closed

    def _session_online(self, session: Session) -> bool:
        return any(
            (session.session_id, role) in self._conns for role in ("mobile", "desktop")
        )

    def _peer_view(self, slot: PeerSlot) -> PeerView:
        return PeerView(
            role=slot.role,  # type: ignore[arg-type]
            device_name=slot.device_name,
            online=slot.online,
        )

    def _audit(
        self, event: str, session: Session, extra: dict[str, Any] | None = None
    ) -> None:
        """审计写入是可选能力，任何失败都在此吞掉，绝不影响主流程。"""
        if self.audit is None:
            return
        detail: dict[str, Any] = {
            "session_id": session.session_id,
            "state": session.state,
            "direction": session.direction,
            "transport": session.transport,
            "initiator_role": session.initiator.role,
            "joiner_role": session.joiner.role if session.joiner else None,
            "created_at": _iso(session.created_at),
            "paired_at": _iso(session.paired_at) if session.paired_at else None,
            "closed_at": _iso(session.closed_at) if session.closed_at else None,
            "close_reason": session.close_reason,
        }
        if extra:
            detail.update(extra)
        try:
            self.audit(event, detail)
        except Exception:
            pass


__all__ = [
    "AttachResult",
    "CODE_ALPHABET",
    "ChannelError",
    "Connection",
    "DetachResult",
    "PairCode",
    "PairingService",
    "PeerSlot",
    "RELAY_SUPPORTED",
    "Session",
    "SweepReport",
]
