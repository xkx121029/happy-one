"""通道层对骨架项目的依赖适配。

契约要求配置与数据库来自 `happyone.config.settings` 与 `happyone.db.get_conn`。
骨架尚未落地时这里用 Protocol + 环境变量兜底，绝不在本模块创建那两个文件。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, runtime_checkable

from .service import PairingService

_ENV_PREFIX = "HAPPYONE_"

#: 未接入骨架配置时使用的默认值，接入后仍可通过环境变量覆盖。
DEFAULTS: dict[str, Any] = {
    "channel_pair_code_ttl": 600.0,
    "channel_session_ttl": 1800.0,
    "channel_heartbeat_timeout": 30.0,
    "channel_sweep_interval": 30.0,
    "channel_max_sessions": 64,
    "channel_max_signal_bytes": 64 * 1024,
}


@runtime_checkable
class SettingsProtocol(Protocol):
    """骨架 settings 对象需要满足的最小接口（全部带默认值兜底）。"""

    channel_pair_code_ttl: float
    channel_session_ttl: float
    channel_heartbeat_timeout: float
    channel_sweep_interval: float
    channel_max_sessions: int
    channel_max_signal_bytes: int


@runtime_checkable
class ConnProtocol(Protocol):
    """sqlite3.Connection 的最小可用子集，骨架换成连接池也兼容。"""

    def execute(self, sql: str, parameters: tuple[Any, ...] = ...) -> Any: ...

    def commit(self) -> None: ...

    def close(self) -> None: ...


_lock = threading.Lock()
_service: PairingService | None = None
_bound_settings: Any | None = None
_bound_database: Any | None = None


def configure(*, database: Any | None = None, settings: Any | None = None) -> None:
    """骨架插件挂载器（core/plugins.py）注入 database/settings 的入口。

    注入后会重建尚未使用的单例，保证通道层拿到的阈值来自骨架配置。
    """
    global _service, _bound_settings, _bound_database
    with _lock:
        if settings is not None:
            _bound_settings = settings
        if database is not None:
            _bound_database = database
        _service = None


def _external_settings() -> Any | None:
    """注入优先；否则在调用时尝试导入骨架配置，缺失时安静回退。"""
    if _bound_settings is not None:
        return _bound_settings
    try:
        from happyone.config import settings as external  # type: ignore
    except Exception:
        return None
    return external


def _external_conn_factory() -> Callable[[], ConnProtocol] | None:
    if _bound_database is not None:
        connect = getattr(_bound_database, "connect", None)
        if callable(connect):
            return connect  # type: ignore[no-any-return]
    try:
        from happyone.db import get_conn  # type: ignore
    except Exception:
        return None
    return get_conn  # type: ignore[no-any-return]


def resolve_setting(name: str) -> Any:
    """环境变量 > 骨架 settings > 内置默认值。"""
    env_value = os.environ.get(f"{_ENV_PREFIX}{name.upper()}")
    if env_value is not None:
        default = DEFAULTS.get(name)
        try:
            if isinstance(default, int) and not isinstance(default, bool):
                return int(env_value)
            return float(env_value)
        except ValueError:
            pass
    external = _external_settings()
    if external is not None:
        value = getattr(external, name, None)
        if value is not None:
            return value
    return DEFAULTS.get(name)


def make_audit_writer() -> Callable[[str, dict[str, Any]], None] | None:
    """把配对生命周期写入 SQLite 审计表（只写审计，不保存连接状态）。

    数据库不存在、表未建、写入失败时全部吞掉：通道层不因审计不可用而失败。
    """
    factory = _external_conn_factory()
    if factory is None:
        return None

    def _write(event: str, detail: dict[str, Any]) -> None:
        try:
            conn = factory()
        except Exception:
            return
        try:
            created = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO channel_events (session_id, event, detail, created_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    detail.get("session_id"),
                    event,
                    json.dumps(detail, ensure_ascii=False),
                    created,
                ),
            )
            conn.execute(
                "INSERT INTO channel_sessions (session_id, direction, state,"
                " initiator_role, joiner_role, transport, created_at, paired_at,"
                " closed_at, close_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " state=excluded.state,"
                " joiner_role=COALESCE(excluded.joiner_role, channel_sessions.joiner_role),"
                " paired_at=COALESCE(excluded.paired_at, channel_sessions.paired_at),"
                " closed_at=COALESCE(excluded.closed_at, channel_sessions.closed_at),"
                " close_reason=COALESCE(excluded.close_reason, channel_sessions.close_reason)",
                (
                    detail.get("session_id"),
                    detail.get("direction"),
                    detail.get("state"),
                    detail.get("initiator_role"),
                    detail.get("joiner_role"),
                    detail.get("transport", "direct"),
                    detail.get("created_at"),
                    detail.get("paired_at"),
                    detail.get("closed_at"),
                    detail.get("close_reason"),
                ),
            )
            conn.commit()
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    return _write


def build_service() -> PairingService:
    return PairingService(
        pair_ttl=float(resolve_setting("channel_pair_code_ttl")),
        session_ttl=float(resolve_setting("channel_session_ttl")),
        heartbeat_timeout=float(resolve_setting("channel_heartbeat_timeout")),
        sweep_interval=float(resolve_setting("channel_sweep_interval")),
        max_sessions=int(resolve_setting("channel_max_sessions")),
        max_signal_bytes=int(resolve_setting("channel_max_signal_bytes")),
        audit=make_audit_writer(),
    )


def get_service() -> PairingService:
    """模块级单例：单进程内存状态，供 router 复用。"""
    global _service
    if _service is None:
        with _lock:
            if _service is None:
                _service = build_service()
    return _service


def set_service(service: PairingService | None = None) -> PairingService:
    """替换单例，测试注入用；传 None 则重新按配置构建。"""
    global _service
    with _lock:
        _service = service
    return get_service()


__all__ = [
    "DEFAULTS",
    "ConnProtocol",
    "SettingsProtocol",
    "build_service",
    "configure",
    "get_service",
    "make_audit_writer",
    "resolve_setting",
    "set_service",
]
