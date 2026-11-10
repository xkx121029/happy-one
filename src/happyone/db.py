"""SQLite 访问与迁移执行器。

- 单进程、单机、WAL；每个请求独立连接，事务只在读改写竞态点使用
  BEGIN IMMEDIATE。
- 迁移按 migrations/*.sql 文件名排序执行，记录版本与 sha256；已执行文件
  被修改时拒绝启动（防止多人并行改历史）。
- 迁移失败直接抛出，由应用启动流程 fail-closed。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from happyone.core.timeutil import now_iso

_MIGRATION_NAME = re.compile(r"^\d{3}_[A-Za-z0-9_\-]+\.sql$")
_BUSY_TIMEOUT_MS = 5000


@dataclass(frozen=True)
class MigrationResult:
    applied: list[str] = field(default_factory=list)
    total: int = 0


@dataclass(frozen=True)
class MigrationStatus:
    version: str
    checksum: str
    applied_at: str


class Database:
    """连接工厂 + 轻量查询助手。仓储层通过它拿连接，不直接碰 sqlite3。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._wal_ready = False
        self._wal_lock = threading.Lock()

    # ---- 连接 ----

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,  # 自动提交；显式事务见 transaction()
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        self._ensure_wal(conn)
        return conn

    def _ensure_wal(self, conn: sqlite3.Connection) -> None:
        if self._wal_ready:
            return
        with self._wal_lock:
            if self._wal_ready:
                return
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != "wal":
                conn.execute("PRAGMA journal_mode = WAL")
            self._wal_ready = True

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """读改写竞态点使用：立即取得写锁，异常回滚。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ---- 查询助手 ----

    def query_one(
        self, sql: str, params: Sequence[Any] = ()
    ) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute(sql, params).fetchone()

    def query_all(
        self, sql: str, params: Sequence[Any] = ()
    ) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return list(conn.execute(sql, params).fetchall())

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.connection() as conn:
            cursor = conn.execute(sql, params)
            return cursor.rowcount

    # ---- 迁移 ----

    def migrate(self, migrations_dir: Path | None = None) -> MigrationResult:
        directory = Path(migrations_dir or (Path(__file__).parent / "migrations"))
        files = self._migration_files(directory)
        applied: list[str] = []

        with self.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    TEXT PRIMARY KEY,
                    checksum   TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            known = {
                row["version"]: row["checksum"]
                for row in conn.execute(
                    "SELECT version, checksum FROM schema_migrations"
                ).fetchall()
            }

        for path in files:
            content = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
            version = path.name

            if version in known:
                if known[version] != checksum:
                    raise RuntimeError(
                        f"迁移文件 {version} 在已执行后被修改，拒绝启动；"
                        "请恢复原文件或新增迁移编号"
                    )
                continue

            statements = _split_sql(content)
            with self.transaction() as conn:
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, checksum, applied_at)"
                    " VALUES (?, ?, ?)",
                    (version, checksum, now_iso()),
                )
            applied.append(version)

        return MigrationResult(applied=applied, total=len(files))

    @staticmethod
    def _migration_files(directory: Path) -> list[Path]:
        if not directory.is_dir():
            return []
        files = [
            path
            for path in directory.iterdir()
            if path.is_file() and _MIGRATION_NAME.match(path.name)
        ]
        return sorted(files, key=lambda item: item.name)

    def migration_status(self) -> list[MigrationStatus]:
        row = self.query_one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        )
        if row is None:
            return []
        rows = self.query_all(
            "SELECT version, checksum, applied_at FROM schema_migrations"
            " ORDER BY version"
        )
        return [
            MigrationStatus(
                version=item["version"],
                checksum=item["checksum"],
                applied_at=item["applied_at"],
            )
            for item in rows
        ]

    def ping(self) -> None:
        self.query_one("SELECT 1")


def _split_sql(script: str) -> list[str]:
    """按完整语句切分 SQL，避免在字符串或注释内的分号处截断。"""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    tail = buffer.strip()
    if tail:
        statements.append(tail)
    return statements


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


# ---- 业务线兼容接口 ----
# 四条业务线按「骨架线提供 happyone.db.get_conn」的约定读取连接。应用启动时
# 通过 bind_database 绑定当前 Database；未绑定时按全局配置惰性创建。

_active_database: Database | None = None
_active_lock = threading.Lock()


def bind_database(database: Database) -> None:
    """绑定进程内数据库实例（应用启动 / CLI 初始化时调用）。"""
    global _active_database
    with _active_lock:
        _active_database = database


def get_conn() -> sqlite3.Connection:
    """返回一个新连接；生命周期由调用方负责 close。"""
    global _active_database
    with _active_lock:
        if _active_database is None:
            from happyone.config import get_global_settings

            _active_database = Database(get_global_settings().database_path)
        database = _active_database
    return database.connect()

