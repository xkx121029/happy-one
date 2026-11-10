"""存储模块的外部依赖解析。

骨架线会提供 ``happyone.config.settings`` 与 ``happyone.db.get_conn``；在它们
落地之前，这里用 ``typing.Protocol`` 描述期望的形状，并从环境变量兜底，
保证本模块可以独立运行单测。本文件不创建那两个模块。

令牌只允许来自配置对象或环境变量，模块内不保存任何默认令牌。
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .client import HubClient
from .constants import CHUNK_SIZE, MULTIPART_THRESHOLD, SIGN_TTL_DEFAULT
from .errors import CODE_AUTH, CODE_UNAVAILABLE, StorageError

DEFAULT_HUB_TIMEOUT_SEC = 10.0
DEFAULT_HUB_MAX_RETRIES = 2
DEFAULT_CHUNK_SIZE = CHUNK_SIZE  # 与 happy-hub 分片会话约定一致
DEFAULT_MULTIPART_THRESHOLD = MULTIPART_THRESHOLD
DEFAULT_SIGN_TTL_SEC = SIGN_TTL_DEFAULT

ENV_BASE_URL = "HAPPYONE_HUB_BASE_URL"
ENV_TOKEN = "HAPPYONE_HUB_TOKEN"
ENV_TIMEOUT = "HAPPYONE_HUB_TIMEOUT_SEC"
ENV_MAX_RETRIES = "HAPPYONE_HUB_MAX_RETRIES"
ENV_SIGN_TTL = "HAPPYONE_STORAGE_SIGN_TTL_SEC"


class StorageSettings(Protocol):
    """骨架线配置对象需要满足的最小形状（属性名可用别名，见 ``_ALIASES``）。"""

    hub_base_url: str
    hub_token: str
    hub_timeout_sec: float
    hub_max_retries: int
    storage_chunk_size: int
    storage_multipart_threshold: int
    storage_sign_ttl_sec: int


@dataclass(frozen=True)
class StorageConfig:
    """本模块自带的最小配置实现，用于环境变量兜底与单测注入。"""

    hub_base_url: str
    hub_token: str
    hub_timeout_sec: float = DEFAULT_HUB_TIMEOUT_SEC
    hub_max_retries: int = DEFAULT_HUB_MAX_RETRIES
    storage_chunk_size: int = DEFAULT_CHUNK_SIZE
    storage_multipart_threshold: int = DEFAULT_MULTIPART_THRESHOLD
    storage_sign_ttl_sec: int = DEFAULT_SIGN_TTL_SEC

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "StorageConfig":
        source = os.environ if env is None else env
        base_url = str(source.get(ENV_BASE_URL, "")).strip()
        token = str(source.get(ENV_TOKEN, "")).strip()
        if not base_url:
            raise StorageError(
                CODE_UNAVAILABLE,
                f"存储服务尚未配置 happy-hub 地址（{ENV_BASE_URL}）",
                status=503,
            )
        if not token:
            raise StorageError(
                CODE_AUTH,
                f"存储服务尚未配置 happy-hub 令牌（{ENV_TOKEN}）",
                status=503,
            )
        return cls(
            hub_base_url=base_url,
            hub_token=token,
            hub_timeout_sec=_as_float(source.get(ENV_TIMEOUT), DEFAULT_HUB_TIMEOUT_SEC),
            hub_max_retries=_as_int(source.get(ENV_MAX_RETRIES), DEFAULT_HUB_MAX_RETRIES),
            storage_sign_ttl_sec=_as_int(source.get(ENV_SIGN_TTL), DEFAULT_SIGN_TTL_SEC),
        )


@dataclass(frozen=True)
class ConnProvider:
    """把 ``happyone.db.get_conn`` 包一层，便于单测替换成内存 sqlite。"""

    factory: Any

    def __call__(self) -> Any:
        return self.factory()


_ALIASES: dict[str, tuple[str, ...]] = {
    "hub_base_url": ("hub_base_url", "happy_hub_base_url", "hub_url", "hub.base_url"),
    "hub_token": ("hub_token", "happy_hub_token", "hh_token", "hub.token"),
    "hub_timeout_sec": ("hub_timeout_sec", "hub_timeout", "hub.timeout_sec"),
    "hub_max_retries": ("hub_max_retries", "hub.max_retries"),
    "storage_chunk_size": ("storage_chunk_size", "storage.chunk_size"),
    "storage_multipart_threshold": (
        "storage_multipart_threshold",
        "storage.multipart_threshold",
    ),
    "storage_sign_ttl_sec": ("storage_sign_ttl_sec", "storage.sign_ttl_sec"),
}

_client_lock = threading.Lock()
_client_cache: dict[str, HubClient] = {}


def resolve_settings() -> StorageSettings:
    """优先使用骨架线的配置对象，缺失时回退到环境变量。"""

    project_settings = _load_project_settings()
    if project_settings is None:
        return StorageConfig.from_env()

    base_url = _read(project_settings, "hub_base_url")
    token = _read(project_settings, "hub_token")
    if not base_url:
        raise StorageError(
            CODE_UNAVAILABLE,
            f"存储服务尚未配置 happy-hub 地址（{ENV_BASE_URL}）",
            status=503,
        )
    if not token:
        raise StorageError(
            CODE_AUTH,
            f"存储服务尚未配置 happy-hub 令牌（{ENV_TOKEN}）",
            status=503,
        )
    return StorageConfig(
        hub_base_url=str(base_url).strip(),
        hub_token=str(token).strip(),
        hub_timeout_sec=_as_float(
            _read(project_settings, "hub_timeout_sec"), DEFAULT_HUB_TIMEOUT_SEC
        ),
        hub_max_retries=_as_int(
            _read(project_settings, "hub_max_retries"), DEFAULT_HUB_MAX_RETRIES
        ),
        storage_chunk_size=_as_int(
            _read(project_settings, "storage_chunk_size"), DEFAULT_CHUNK_SIZE
        ),
        storage_multipart_threshold=_as_int(
            _read(project_settings, "storage_multipart_threshold"),
            DEFAULT_MULTIPART_THRESHOLD,
        ),
        storage_sign_ttl_sec=_as_int(
            _read(project_settings, "storage_sign_ttl_sec"), DEFAULT_SIGN_TTL_SEC
        ),
    )


def get_hub_client() -> HubClient:
    """返回进程内复用的 hub 客户端（连接池共享，令牌不会落盘）。"""

    settings = resolve_settings()
    token_fingerprint = hashlib.sha256(settings.hub_token.encode("utf-8")).hexdigest()[:12]
    cache_key = f"{settings.hub_base_url}|{token_fingerprint}"
    with _client_lock:
        client = _client_cache.get(cache_key)
        if client is None:
            client = HubClient(
                base_url=settings.hub_base_url,
                token=settings.hub_token,
                timeout_sec=settings.hub_timeout_sec,
                max_retries=settings.hub_max_retries,
            )
            _client_cache[cache_key] = client
        return client


async def aclose_hub_client() -> None:
    """应用关闭时释放连接池；骨架线可在 shutdown 事件里调用。"""

    with _client_lock:
        clients = list(_client_cache.values())
        _client_cache.clear()
    for client in clients:
        await client.aclose()


def get_conn() -> Any:
    """取 happy-one 的数据库连接；尚未接入时给出明确中文错误。"""

    loader = _load_project_db()
    if loader is None:
        raise StorageError(
            CODE_UNAVAILABLE,
            "尚未接入 happy-one 数据库连接（happyone.db.get_conn），无法记录文件元数据",
            status=503,
        )
    return loader()


def build_service() -> Any:
    """按当前配置组装 StorageService（router 的默认依赖）。"""

    from .service import StorageService

    settings = resolve_settings()
    return StorageService(
        client=get_hub_client(),
        conn_provider=ConnProvider(get_conn),
        chunk_size=settings.storage_chunk_size,
        multipart_threshold=settings.storage_multipart_threshold,
        sign_ttl_default=settings.storage_sign_ttl_sec,
    )


def _load_project_settings() -> Any | None:
    try:
        from happyone import config as project_config  # type: ignore import-not-found
    except ImportError:
        return None
    return getattr(project_config, "settings", None)


def _load_project_db() -> Any | None:
    try:
        from happyone import db as project_db  # type: ignore import-not-found
    except ImportError:
        return None
    return getattr(project_db, "get_conn", None)


def _read(source: Any, field: str) -> Any:
    for name in _ALIASES[field]:
        value = _read_path(source, name)
        if value not in (None, ""):
            return value
    return None


def _read_path(source: Any, path: str) -> Any:
    current = source
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, Mapping):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
    return current


def _as_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _as_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default
