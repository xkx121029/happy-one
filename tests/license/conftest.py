"""许可证模块测试的公共夹具。

本目录的测试不依赖仓库其它线路：自行把 ``src/`` 加入 sys.path，
并使用内存 sqlite + 固定时钟，保证结果稳定可复现。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from happyone.license.keys import generate_keypair  # noqa: E402
from happyone.license.models import IssueRequest  # noqa: E402
from happyone.license.service import LicenseService  # noqa: E402
from happyone.license.store import LicenseStore  # noqa: E402

# 固定时间：2027-01-15 08:00:00 UTC，避免测试依赖真实时钟
FIXED_NOW = 1_800_000_000
DAY = 86_400


@pytest.fixture
def keypair():
    return generate_keypair()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "license_test.db"


@pytest.fixture
def store(db_path):
    # 与骨架线约定一致：每次取新连接，用完由 store 关闭
    return LicenseStore(lambda: sqlite3.connect(db_path, check_same_thread=False))


@pytest.fixture
def conn(db_path, store):
    """直接查询数据库用的连接（测试断言表内容）。"""
    store.ensure_schema()
    connection = sqlite3.connect(db_path, check_same_thread=False)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def service(keypair, store):
    return LicenseService(
        keypair,
        store=store,
        grace_days=7,
        clock=lambda: FIXED_NOW,
    )


@pytest.fixture
def issue(service):
    """便捷签发：默认 30 天、无设备绑定，可被关键字覆盖。"""

    def _issue(**overrides):
        params = {
            "user_id": "user_001",
            "product_slug": "happy-one",
            "features": ("core", "export"),
            "expires_in_days": 30,
        }
        params.update(overrides)
        if "expires_at" in overrides and "expires_in_days" not in overrides:
            params.pop("expires_in_days", None)
        return service.issue(IssueRequest(**params), now=FIXED_NOW)

    return _issue
