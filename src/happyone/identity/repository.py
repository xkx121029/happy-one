"""身份线仓储：只做 SQL，不含业务判断。"""

from __future__ import annotations

import sqlite3
from typing import Any, Sequence

from happyone.db import Database


class IdentityRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ---- 用户 ----

    def create_user(
        self,
        *,
        user_id: str,
        username: str,
        email: str | None,
        display_name: str,
        password_hash: str,
        is_admin: bool,
        created_at: str,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO users (id, username, email, display_name, password_hash,
                               is_admin, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                user_id,
                username,
                email,
                display_name,
                password_hash,
                1 if is_admin else 0,
                created_at,
                created_at,
            ),
        )

    def get_user(self, user_id: str) -> sqlite3.Row | None:
        return self._db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))

    def get_user_by_login(self, login: str) -> sqlite3.Row | None:
        """登录名匹配：用户名（小写）或邮箱（小写）。"""
        return self._db.query_one(
            "SELECT * FROM users WHERE username = ? OR (email IS NOT NULL AND email = ?)",
            (login, login),
        )

    def list_users(
        self, *, limit: int, after_rowid: int | None, query: str | None
    ) -> list[sqlite3.Row]:
        sql = "SELECT rowid AS _rowid, * FROM users"
        clauses: list[str] = []
        params: list[Any] = []
        if after_rowid is not None:
            clauses.append("rowid > ?")
            params.append(after_rowid)
        if query:
            like = f"%{query}%"
            clauses.append(
                "(username LIKE ? OR email LIKE ? OR display_name LIKE ?)"
            )
            params.extend([like, like, like])
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY rowid LIMIT ?"
        params.append(limit)
        return self._db.query_all(sql, params)

    def count_users(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row else 0

    def update_user(self, user_id: str, fields: dict[str, Any]) -> None:
        if not fields:
            return
        allowed = {"display_name", "email", "status", "is_admin", "updated_at"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        params: list[Any] = list(updates.values())
        params.append(user_id)
        self._db.execute(f"UPDATE users SET {assignments} WHERE id = ?", params)

    def update_password(self, user_id: str, password_hash: str, updated_at: str) -> None:
        self._db.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (password_hash, updated_at, user_id),
        )

    def touch_login(self, user_id: str, at: str) -> None:
        self._db.execute(
            "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
            (at, at, user_id),
        )

    # ---- 会话 ----

    def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        token_hash: str,
        device_name: str | None,
        ip: str | None,
        user_agent: str | None,
        created_at: str,
        expires_at: str,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO sessions (id, user_id, token_hash, device_name, ip,
                                  user_agent, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                user_id,
                token_hash,
                device_name,
                ip,
                user_agent,
                created_at,
                expires_at,
            ),
        )

    def get_session_by_hash(self, token_hash: str) -> sqlite3.Row | None:
        return self._db.query_one(
            """
            SELECT s.*, u.username, u.email, u.display_name, u.is_admin,
                   u.status AS user_status, u.created_at AS user_created_at,
                   u.updated_at AS user_updated_at, u.last_login_at
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
            """,
            (token_hash,),
        )

    def list_sessions(self, user_id: str) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM sessions WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )

    def get_session(self, session_id: str, user_id: str) -> sqlite3.Row | None:
        return self._db.query_one(
            "SELECT * FROM sessions WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        )

    def revoke_session(self, session_id: str, user_id: str, at: str) -> int:
        return self._db.execute(
            "UPDATE sessions SET revoked_at = ?"
            " WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            (at, session_id, user_id),
        )

    def revoke_session_by_hash(self, token_hash: str, at: str) -> int:
        return self._db.execute(
            "UPDATE sessions SET revoked_at = ?"
            " WHERE token_hash = ? AND revoked_at IS NULL",
            (at, token_hash),
        )

    def revoke_all(
        self, user_id: str, at: str, except_session_id: str | None = None
    ) -> int:
        if except_session_id:
            return self._db.execute(
                "UPDATE sessions SET revoked_at = ?"
                " WHERE user_id = ? AND revoked_at IS NULL AND id <> ?",
                (at, user_id, except_session_id),
            )
        return self._db.execute(
            "UPDATE sessions SET revoked_at = ?"
            " WHERE user_id = ? AND revoked_at IS NULL",
            (at, user_id),
        )

    def purge_expired(self, now: str) -> int:
        return self._db.execute(
            "DELETE FROM sessions WHERE expires_at < ?", (now,)
        )
