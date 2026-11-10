"""存储模块单测的公共夹具：内存 sqlite + 不联网的配置。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
MIGRATION = SRC / "happyone" / "migrations" / "003_storage.sql"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from happyone.storage.client import HubClient  # noqa: E402
from happyone.storage.deps import StorageConfig  # noqa: E402
from happyone.storage.service import StorageService  # noqa: E402

HUB_BASE_URL = "http://hub.test"


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(MIGRATION.read_text(encoding="utf-8"))
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def settings() -> StorageConfig:
    return StorageConfig(
        hub_base_url=HUB_BASE_URL,
        hub_token="hh_test_token",
        hub_timeout_sec=2.0,
        hub_max_retries=0,
        storage_chunk_size=4,
        storage_multipart_threshold=4,
        storage_sign_ttl_sec=300,
    )


def make_client(max_retries: int = 0) -> HubClient:
    return HubClient(
        HUB_BASE_URL,
        "hh_test_token",
        timeout_sec=2.0,
        max_retries=max_retries,
        backoff_base=0.0,
        backoff_max=0.0,
    )


def make_service(
    conn: sqlite3.Connection,
    client: HubClient | None = None,
    *,
    chunk_size: int = 4,
    threshold: int = 1024,
) -> StorageService:
    return StorageService(
        client or make_client(),
        lambda: conn,
        chunk_size=chunk_size,
        multipart_threshold=threshold,
        sign_ttl_default=300,
    )


@pytest.fixture()
def client_factory():
    """返回一个按需构造 HubClient 的工厂，方便个别用例打开重试。"""

    return make_client


@pytest.fixture()
def service_factory(conn):
    """返回绑定当前内存库的 StorageService 工厂。"""

    def factory(client: HubClient | None = None, *, chunk_size: int = 4, threshold: int = 1024):
        return make_service(conn, client, chunk_size=chunk_size, threshold=threshold)

    return factory
