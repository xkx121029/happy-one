"""许可证持久化：签发记录、吊销项、多设备激活记录。

设计要点：
- 不修改连接的任何配置（row_factory / isolation_level 等），避免影响同库的其他线路；
- 骨架线约定 ``get_conn()`` 每次返回新连接、由调用方 close，本模块按此约定使用后关闭；
- 首次使用时自动执行 002_license.sql，保证模块脱离骨架线也能独立跑通。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, Sequence

from .models import LicenseRecord, RevocationEntry

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "migrations" / "002_license.sql"


class DeviceLimitError(Exception):
    """设备数超过许可证上限。"""

    def __init__(self, license_id: str, limit: int, current: int) -> None:
        super().__init__(f"许可证 {license_id} 最多允许 {limit} 台设备，已激活 {current} 台")
        self.license_id = license_id
        self.limit = limit
        self.current = current


class LicenseStore:
    """基于 sqlite3 的存储实现，构造时注入取连接的函数。"""

    def __init__(
        self,
        connect: Callable[[], Any],
        *,
        schema_path: Path | None = None,
        close_after_use: bool = True,
    ) -> None:
        self._connect = connect
        self._schema_path = Path(schema_path) if schema_path is not None else SCHEMA_PATH
        self._close_after_use = close_after_use
        self._schema_ready = False

    # ---------- 连接与会话 ----------

    @contextmanager
    def _session(self) -> Iterator[Any]:
        conn = self._connect()
        try:
            if not self._schema_ready:
                conn.executescript(self._schema_path.read_text(encoding="utf-8"))
                conn.commit()
                self._schema_ready = True
            yield conn
        finally:
            if self._close_after_use:
                conn.close()

    def ensure_schema(self) -> None:
        with self._session():
            pass

    # ---------- 签发记录 ----------

    def save_license(self, record: LicenseRecord) -> None:
        with self._session() as conn, conn:
            conn.execute(
                """
                INSERT INTO license_record (
                    license_id, user_id, product_slug, features_json, issued_at,
                    expires_at, device_max, device_fp_hash, kid, token, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.license_id,
                    record.user_id,
                    record.product_slug,
                    json.dumps(list(record.features), ensure_ascii=False),
                    record.issued_at,
                    record.expires_at,
                    record.device_max,
                    record.device_fp_hash,
                    record.kid,
                    record.token,
                    record.created_at,
                ),
            )

    def get_license(self, license_id: str) -> LicenseRecord | None:
        with self._session() as conn:
            row = conn.execute(
                """
                SELECT license_id, user_id, product_slug, features_json, issued_at,
                       expires_at, device_max, device_fp_hash, kid, token, created_at
                FROM license_record WHERE license_id = ?
                """,
                (license_id,),
            ).fetchone()
        return _row_to_record(row) if row is not None else None

    # ---------- 吊销 ----------

    def add_revocation(self, entry: RevocationEntry, revoked_by: str | None = None) -> bool:
        """写入吊销项；已存在则返回 False。"""
        with self._session() as conn:
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO license_revocation (license_id, revoked_at, reason, revoked_by) "
                        "VALUES (?, ?, ?, ?)",
                        (entry.license_id, entry.revoked_at, entry.reason, revoked_by),
                    )
            except sqlite3.IntegrityError:
                return False
        return True

    def is_revoked(self, license_id: str) -> bool:
        with self._session() as conn:
            row = conn.execute(
                "SELECT 1 FROM license_revocation WHERE license_id = ?", (license_id,)
            ).fetchone()
        return row is not None

    def get_revocation(self, license_id: str) -> RevocationEntry | None:
        with self._session() as conn:
            row = conn.execute(
                "SELECT license_id, revoked_at, reason FROM license_revocation WHERE license_id = ?",
                (license_id,),
            ).fetchone()
        return RevocationEntry.from_row(row) if row is not None else None

    def list_revocations(self) -> Sequence[RevocationEntry]:
        with self._session() as conn:
            rows = conn.execute(
                "SELECT license_id, revoked_at, reason FROM license_revocation "
                "ORDER BY revoked_at ASC, license_id ASC"
            ).fetchall()
        return tuple(RevocationEntry.from_row(row) for row in rows)

    def count_revocations(self) -> int:
        with self._session() as conn:
            row = conn.execute("SELECT COUNT(*) FROM license_revocation").fetchone()
        return int(row[0]) if row is not None else 0

    # ---------- 设备激活 ----------

    def count_devices(self, license_id: str) -> int:
        with self._session() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM license_device WHERE license_id = ?", (license_id,)
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def ensure_device(self, license_id: str, device_hash: str, max_devices: int, now: int) -> str:
        """登记设备；返回 ``existing`` 或 ``added``，超限抛 DeviceLimitError。"""
        with self._session() as conn:
            exists = conn.execute(
                "SELECT 1 FROM license_device WHERE license_id = ? AND device_hash = ?",
                (license_id, device_hash),
            ).fetchone()
            if exists is not None:
                return "existing"

            current = conn.execute(
                "SELECT COUNT(*) FROM license_device WHERE license_id = ?", (license_id,)
            ).fetchone()[0]
            if int(current) >= max_devices:
                raise DeviceLimitError(license_id, max_devices, int(current))

            try:
                with conn:
                    conn.execute(
                        "INSERT INTO license_device (license_id, device_hash, activated_at) VALUES (?, ?, ?)",
                        (license_id, device_hash, now),
                    )
            except sqlite3.IntegrityError:
                # 并发下同一设备被重复登记，视为已存在
                return "existing"

            total = conn.execute(
                "SELECT COUNT(*) FROM license_device WHERE license_id = ?", (license_id,)
            ).fetchone()[0]
            if int(total) > max_devices:
                with conn:
                    conn.execute(
                        "DELETE FROM license_device WHERE license_id = ? AND device_hash = ?",
                        (license_id, device_hash),
                    )
                raise DeviceLimitError(license_id, max_devices, max_devices)

        return "added"


def _row_to_record(row: Sequence[Any]) -> LicenseRecord:
    try:
        features = tuple(json.loads(row[3] or "[]"))
    except (TypeError, ValueError):
        features = ()
    return LicenseRecord(
        license_id=str(row[0]),
        user_id=str(row[1]),
        product_slug=str(row[2]),
        features=tuple(str(item) for item in features),
        issued_at=int(row[4]),
        expires_at=int(row[5]),
        device_max=int(row[6]) if row[6] is not None else None,
        device_fp_hash=str(row[7]) if row[7] is not None else None,
        kid=str(row[8]),
        token=str(row[9]),
        created_at=int(row[10]),
    )


class StoreProtocol(Protocol):
    """service 需要的存储能力，测试可注入内存假实现。"""

    def save_license(self, record: LicenseRecord) -> None: ...

    def get_license(self, license_id: str) -> LicenseRecord | None: ...

    def add_revocation(self, entry: RevocationEntry, revoked_by: str | None = None) -> bool: ...

    def is_revoked(self, license_id: str) -> bool: ...

    def get_revocation(self, license_id: str) -> RevocationEntry | None: ...

    def list_revocations(self) -> Sequence[RevocationEntry]: ...

    def ensure_device(self, license_id: str, device_hash: str, max_devices: int, now: int) -> str: ...
