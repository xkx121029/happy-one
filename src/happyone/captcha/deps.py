"""模块级依赖适配层。

骨架线尚未落地时（仓库可能还是空的），本文件用 ``typing.Protocol`` 声明所需的
配置与数据库接口，并提供可独立运行的兜底实现：

* 配置：优先读 ``happyone.config.settings``，其次读环境变量，最后使用进程内临时密钥。
* 数据库：优先用 ``happyone.db.get_conn``，不可用时退回进程内共享的 ``:memory:``
  SQLite，并自动执行 ``src/happyone/migrations/005_captcha.sql``。

密钥不写入仓库：仅通过配置对象或环境变量 ``HAPPYONE_CAPTCHA_SECRET`` /
``CAPTCHA_SECRET`` 提供；两者都没有时生成临时密钥并打一条告警（重启后旧票据失效）。
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

SECRET_ENV_KEYS = ("HAPPYONE_CAPTCHA_SECRET", "CAPTCHA_SECRET")
SECRET_ATTRS = ("captcha_secret", "CAPTCHA_SECRET", "secret_key", "SECRET_KEY", "secret")

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "migrations" / "005_captcha.sql"


class ConfigLike(Protocol):
    """骨架线配置对象需要满足的最小形态（按属性或字典键读取）。"""

    def __getattr__(self, name: str) -> Any: ...  # pragma: no cover


@runtime_checkable
class ConnectionLike(Protocol):
    """DB-API 连接的最小形态（本项目按 sqlite3 方言实现）。"""

    def execute(self, sql: str, params: tuple[Any, ...] = ...) -> Any: ...

    def commit(self) -> None: ...


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


def _load_settings() -> Any | None:
    try:
        from happyone.config import settings as settings_obj  # type: ignore[import-not-found]
    except Exception:  # 骨架线尚未提供 config.py
        return None
    return settings_obj


def _settings_value(settings_obj: Any, name: str) -> Any:
    if settings_obj is None:
        return None
    if isinstance(settings_obj, dict):
        return settings_obj.get(name)
    return getattr(settings_obj, name, None)


_EPHEMERAL_SECRET: bytes | None = None
_SECRET_LOCK = threading.Lock()

_INJECTED_SETTINGS: Any = None
_INJECTED_DATABASE: Any = None


def configure(*, settings: Any = None, database: Any = None) -> None:
    """骨架线启动时可选的注入点。

    未注入时按「``happyone.db.get_conn`` → 进程内 SQLite」的顺序自动回退；
    之所以同时支持注入，是因为骨架线的 ``Database`` 是显式传递的工厂对象，
    不一定存在模块级 ``get_conn``。
    """
    global _INJECTED_SETTINGS, _INJECTED_DATABASE
    if settings is not None:
        _INJECTED_SETTINGS = settings
    if database is not None:
        _INJECTED_DATABASE = database


def get_secret() -> bytes:
    """取 HMAC 密钥（字节）。禁止把密钥硬编码进仓库。"""
    for env_key in SECRET_ENV_KEYS:
        value = os.environ.get(env_key)
        if value:
            return value.encode("utf-8")

    settings_obj = _INJECTED_SETTINGS if _INJECTED_SETTINGS is not None else _load_settings()
    for attr in SECRET_ATTRS:
        value = _settings_value(settings_obj, attr)
        if value:
            return str(value).encode("utf-8")

    global _EPHEMERAL_SECRET
    with _SECRET_LOCK:
        if _EPHEMERAL_SECRET is None:
            _EPHEMERAL_SECRET = secrets.token_bytes(32)
            logger.warning(
                "未配置 captcha 密钥（%s 或 settings.captcha_secret），"
                "已启用进程内临时密钥；进程重启后历史票据全部失效。",
                SECRET_ENV_KEYS[0],
            )
        return _EPHEMERAL_SECRET


# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #

_GET_CONN: Any = None
_GET_CONN_RESOLVED = False
_MEMORY_CONN: sqlite3.Connection | None = None
_MEMORY_LOCK = threading.RLock()
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_TEXT: str | None = None
_SCHEMA_READY = False


def _resolve_get_conn() -> Any:
    global _GET_CONN, _GET_CONN_RESOLVED
    if _GET_CONN_RESOLVED:
        return _GET_CONN
    try:
        from happyone.db import get_conn  # type: ignore[import-not-found]
    except Exception:  # 骨架线尚未提供 db.py
        get_conn = None
    _GET_CONN = get_conn
    _GET_CONN_RESOLVED = True
    return get_conn


def _schema_sql() -> str:
    global _SCHEMA_TEXT
    if _SCHEMA_TEXT is None:
        _SCHEMA_TEXT = _SCHEMA_PATH.read_text(encoding="utf-8")
    return _SCHEMA_TEXT


def _ensure_schema(conn: Any) -> None:
    """幂等地建表：真实库与内存兜底库共用同一份迁移 SQL，避免两份 DDL 漂移。"""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        sql = _schema_sql()
        if hasattr(conn, "executescript"):
            conn.executescript(sql)
        else:  # 兼容包装过的连接
            for statement in (part.strip() for part in sql.split(";")):
                if statement:
                    conn.execute(statement)
        if hasattr(conn, "commit"):
            conn.commit()
        _SCHEMA_READY = True


def _memory_conn() -> sqlite3.Connection:
    global _MEMORY_CONN
    if _MEMORY_CONN is None:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.execute("PRAGMA journal_mode=MEMORY")
        _ensure_schema(conn)
        _MEMORY_CONN = conn
    return _MEMORY_CONN


@contextmanager
def connection() -> Iterator[Any]:
    """获取一个可用连接，使用完自动提交；内存兜底库加锁串行化。"""
    database = _INJECTED_DATABASE
    if database is not None:
        # 骨架线的 Database：connection() 每次给新连接并负责关闭
        with database.connection() as conn:
            _ensure_schema(conn)
            yield conn
        return

    factory = _resolve_get_conn()
    if factory is None:
        with _MEMORY_LOCK:
            conn = _memory_conn()
            yield conn
        return

    acquired = factory()
    # 骨架线返回的可能是「连接对象」也可能是「上下文管理器」，两种都兼容；
    # 连接由调用方（骨架线）负责生命周期，这里只负责提交，不 close。
    if hasattr(acquired, "__enter__") and not isinstance(acquired, sqlite3.Connection):
        with acquired as conn:
            _ensure_schema(conn)
            yield conn
        return

    conn = acquired
    _ensure_schema(conn)
    yield conn


def commit(conn: Any) -> None:
    if hasattr(conn, "commit"):
        conn.commit()


def reset_test_state() -> None:
    """仅供单测：清空内存兜底库，避免用例之间互相影响。"""
    global _MEMORY_CONN, _SCHEMA_READY
    with _MEMORY_LOCK:
        if _MEMORY_CONN is not None:
            _MEMORY_CONN.close()
        _MEMORY_CONN = None
        _SCHEMA_READY = False
