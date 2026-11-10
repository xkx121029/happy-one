"""迁移脚本与存储层测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from happyone.license.models import LicenseRecord
from happyone.license.store import LicenseStore

SCHEMA = Path(__file__).resolve().parents[2] / "src" / "happyone" / "migrations" / "002_license.sql"


def test_schema_file_exists_and_is_idempotent(conn):
    assert SCHEMA.exists()
    script = SCHEMA.read_text(encoding="utf-8")
    conn.executescript(script)
    conn.executescript(script)  # 重复执行不应报错

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert {"license_record", "license_revocation", "license_device"} <= tables


def test_store_creates_schema_lazily(db_path):
    store = LicenseStore(lambda: sqlite3.connect(db_path))
    assert store.count_revocations() == 0
    assert store.list_revocations() == ()


def test_record_roundtrip(db_path):
    store = LicenseStore(lambda: sqlite3.connect(db_path))
    record = LicenseRecord(
        license_id="lic_1",
        user_id="u1",
        product_slug="p1",
        features=("a", "b"),
        issued_at=100,
        expires_at=200,
        kid="k_x",
        token="hlic1.x.y",
        device_max=2,
        device_fp_hash=None,
        created_at=100,
    )
    store.save_license(record)
    loaded = store.get_license("lic_1")
    assert loaded is not None
    assert loaded.features == ("a", "b")
    assert loaded.device_max == 2
    assert loaded.token == "hlic1.x.y"
