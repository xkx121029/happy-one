"""对外部依赖的最小接口声明与解析逻辑。

骨架线负责提供 ``happyone.config.settings`` 与 ``happyone.db.get_conn``。
本模块只声明「我需要的字段/方法」，并在它们尚不存在时退回到
环境变量 + 用户目录 sqlite 的独立实现，保证本模块可以单独跑通单测。

约定（骨架线只需满足这些，不必与本文档耦合）：

- ``settings.license_key_path``: str | Path | None，许可证私钥路径；缺省 ``~/.happyone/license/license_ed25519.pem``
- ``settings.license_grace_days``: int | None，宽限期天数；缺省 7
- ``settings.hub_secret_path``: str | Path | None，happy-hub 直链密钥路径，仅用于冲突检测
- ``get_conn()``: 返回 sqlite3 连接；连接生命周期由 happyone.db 管理，本模块只 commit，不 close
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

DEFAULT_GRACE_DAYS = 7
DEFAULT_KEY_PATH = Path.home() / ".happyone" / "license" / "license_ed25519.pem"
DEFAULT_DB_PATH = Path.home() / ".happyone" / "happyone.db"

ENV_KEY_PATH = "HAPPYONE_LICENSE_KEY_PATH"
ENV_GRACE_DAYS = "HAPPYONE_LICENSE_GRACE_DAYS"
ENV_HUB_SECRET_PATH = "HAPPYONE_HUB_SECRET_PATH"
ENV_DB_PATH = "HAPPYONE_DB_PATH"

HUB_SECRET_ATTRS = ("hub_secret_path", "happy_hub_secret_path", "hub_data_secret_path")


@runtime_checkable
class SettingsProtocol(Protocol):
    """本模块用到的配置项（全部允许缺失，缺失时用默认值/环境变量）。"""

    license_key_path: str | os.PathLike[str] | None
    license_grace_days: int | None
    hub_secret_path: str | os.PathLike[str] | None


@runtime_checkable
class ConnectionProtocol(Protocol):
    """sqlite3 连接的最小能力（不假设 row_factory、isolation_level 等配置）。"""

    def execute(self, sql: str, parameters: Sequence[Any] = ...) -> Any: ...

    def executescript(self, script: str) -> Any: ...

    def commit(self) -> None: ...


class FallbackSettings:
    """骨架线尚未提供 happyone.config 时使用的环境变量配置。"""

    def __init__(self) -> None:
        self.license_key_path: str | None = os.environ.get(ENV_KEY_PATH) or None
        raw_grace = os.environ.get(ENV_GRACE_DAYS)
        self.license_grace_days: int | None = int(raw_grace) if raw_grace and raw_grace.isdigit() else None
        self.hub_secret_path: str | None = os.environ.get(ENV_HUB_SECRET_PATH) or None


def get_settings() -> Any:
    """优先使用骨架线的 settings；模块不存在时退回 FallbackSettings。"""
    try:
        from happyone.config import settings as project_settings  # type: ignore[import-not-found]
    except ImportError:
        return FallbackSettings()
    return project_settings


def _attr(settings: Any, name: str) -> Any:
    if settings is None:
        return None
    try:
        return getattr(settings, name, None)
    except Exception:  # 某些 Settings 实现在属性缺失时会抛异常
        return None


def resolve_key_path(settings: Any = None) -> Path:
    raw = _attr(settings, "license_key_path")
    if not raw:
        raw = os.environ.get(ENV_KEY_PATH)
    return Path(raw).expanduser() if raw else DEFAULT_KEY_PATH


def resolve_grace_days(settings: Any = None) -> int:
    value = _attr(settings, "license_grace_days")
    if value is None:
        value = os.environ.get(ENV_GRACE_DAYS)
    try:
        days = int(value) if value is not None else DEFAULT_GRACE_DAYS
    except (TypeError, ValueError):
        days = DEFAULT_GRACE_DAYS
    return max(0, min(days, 365))


def resolve_hub_secret_path(settings: Any = None) -> Path | None:
    raw = None
    for name in HUB_SECRET_ATTRS:
        raw = _attr(settings, name)
        if raw:
            break
    if not raw:
        raw = os.environ.get(ENV_HUB_SECRET_PATH)
    return Path(raw).expanduser() if raw else None


def _fallback_db_conn() -> sqlite3.Connection:
    """骨架线缺少 happyone.db 时的兜底连接（每次新建，由调用方 close）。"""
    db_path = Path(os.environ.get(ENV_DB_PATH) or DEFAULT_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(db_path), check_same_thread=False)


def get_db_conn() -> ConnectionProtocol:
    """优先使用骨架线的 happyone.db.get_conn；不存在时退回用户目录 sqlite。"""
    try:
        from happyone.db import get_conn  # type: ignore[import-not-found]
    except ImportError:
        return _fallback_db_conn()
    return get_conn()
