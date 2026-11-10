"""通道层对外数据模型、错误码与事件名。

本文件不依赖 FastAPI，HTTP / WebSocket 适配统一放在 router.py。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["mobile", "desktop"]
Direction = Literal["mobile_to_desktop", "desktop_to_mobile"]
Transport = Literal["direct", "relay"]
SessionState = Literal["waiting_peer", "paired", "connected", "closed", "expired"]

#: 所有对外错误文案统一在这里维护，保证「错误信息永远是中文」。
ERROR_MESSAGES: dict[str, str] = {
    "bad_request": "请求参数不合法",
    "bad_json": "请求体不是合法的 JSON",
    "bad_event": "消息格式不合法",
    "invalid_role": "设备角色必须是 mobile 或 desktop",
    "role_not_in_session": "该角色不在当前配对会话中",
    "role_conflict": "配对双方角色不能相同",
    "code_format": "取件码格式不正确，应为 6 位字符",
    "code_not_found": "取件码不存在，请确认后重试",
    "code_expired": "取件码已过期，请重新生成",
    "code_used": "取件码已被使用，请重新生成",
    "session_not_found": "配对会话不存在或已被清理",
    "session_closed": "配对会话已结束，请重新配对",
    "session_expired": "配对会话已超时，请重新配对",
    "too_many_sessions": "当前配对会话过多，请稍后再试",
    "token_required": "缺少会话令牌",
    "token_invalid": "会话令牌无效",
    "not_connected": "当前连接尚未注册到配对会话",
    "connection_replaced": "该角色的旧连接已被新连接替换",
    "peer_offline": "对端尚未连接，消息未送达",
    "peer_busy": "对端繁忙，请稍后重试",
    "payload_too_large": "信令内容超过大小限制",
    "unknown_event": "不支持的事件类型",
    "heartbeat_timeout": "心跳超时，连接已断开",
    "relay_not_enabled": "中继转发尚未启用",
    "internal": "通道层内部错误",
}


class ClientEvent:
    """客户端 -> 服务端。"""

    OFFER = "signal.offer"
    ANSWER = "signal.answer"
    ICE = "signal.ice"
    PING = "ping"
    BYE = "bye"


class ServerEvent:
    """服务端 -> 客户端。"""

    READY = "signal.ready"
    PEER_JOINED = "signal.peer_joined"
    PEER_LEFT = "signal.peer_left"
    SESSION_CLOSED = "signal.session_closed"
    PONG = "signal.pong"
    ERROR = "signal.error"
    # 以下三个是转发事件，服务端只做路由，不解析内容
    OFFER = ClientEvent.OFFER
    ANSWER = ClientEvent.ANSWER
    ICE = ClientEvent.ICE


#: 允许服务端原样转发的客户端事件。
FORWARDABLE_EVENTS = frozenset({ClientEvent.OFFER, ClientEvent.ANSWER, ClientEvent.ICE})


class PeerView(BaseModel):
    role: Role
    device_name: str | None = None
    online: bool = False


class ConnectionView(BaseModel):
    initiator_online: bool
    joiner_online: bool
    transport: Transport
    relay_available: bool = False


class PairCreateRequest(BaseModel):
    role: str = Field(description="创建方角色：mobile 或 desktop")
    device_name: str | None = Field(default=None, max_length=64)


class PairJoinRequest(BaseModel):
    code: str = Field(description="6 位取件码")
    role: str = Field(description="加入方角色：mobile 或 desktop")
    device_name: str | None = Field(default=None, max_length=64)


class PairCreateResponse(BaseModel):
    ok: Literal[True] = True
    code: str
    session_id: str
    token: str
    role: Role
    direction: Direction
    state: SessionState
    transport: Transport
    relay_available: bool
    expires_at: str
    expires_in: int
    ws_path: str = "/ws/signal"


class PairJoinResponse(BaseModel):
    ok: Literal[True] = True
    session_id: str
    token: str
    role: Role
    direction: Direction
    state: SessionState
    transport: Transport
    relay_available: bool
    peer: PeerView
    expires_at: str
    expires_in: int
    ws_path: str = "/ws/signal"


class PairStatusResponse(BaseModel):
    ok: Literal[True] = True
    session_id: str
    state: SessionState
    direction: Direction
    transport: Transport
    relay_available: bool
    initiator: PeerView
    joiner: PeerView | None = None
    connection: ConnectionView
    created_at: str
    expires_at: str
    expires_in: int
    close_reason: str | None = None


class RevokeResponse(BaseModel):
    ok: Literal[True] = True
    session_id: str
    state: SessionState = "closed"
    notified: list[Role] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    ok: Literal[False] = False
    code: str
    error: str
