"""授权线仓储：客户端、授权码、设备码、令牌。"""

from __future__ import annotations

import sqlite3
from typing import Any

from happyone.db import Database


class OAuthRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ---- 客户端 ----

    def create_client(
        self,
        *,
        client_id: str,
        name: str,
        client_type: str,
        secret_hash: str | None,
        redirect_uris_json: str,
        scopes: str,
        created_at: str,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO oidc_clients (client_id, name, client_type, secret_hash,
                                      redirect_uris, scopes, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                client_id,
                name,
                client_type,
                secret_hash,
                redirect_uris_json,
                scopes,
                created_at,
            ),
        )

    def get_client(self, client_id: str) -> sqlite3.Row | None:
        return self._db.query_one(
            "SELECT * FROM oidc_clients WHERE client_id = ?", (client_id,)
        )

    def list_clients(self) -> list[sqlite3.Row]:
        return self._db.query_all("SELECT * FROM oidc_clients ORDER BY created_at")

    # ---- 授权码 ----

    def insert_auth_code(
        self,
        *,
        code_hash: str,
        client_id: str,
        user_id: str,
        redirect_uri: str,
        scope: str,
        code_challenge: str,
        code_challenge_method: str,
        nonce: str | None,
        created_at: str,
        expires_at: str,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO oidc_auth_codes (code_hash, client_id, user_id, redirect_uri,
                                         scope, code_challenge, code_challenge_method,
                                         nonce, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code_hash,
                client_id,
                user_id,
                redirect_uri,
                scope,
                code_challenge,
                code_challenge_method,
                nonce,
                created_at,
                expires_at,
            ),
        )

    def consume_auth_code(self, code_hash: str, now: str) -> sqlite3.Row | None:
        """原子消费授权码：已消费、不存在、已过期一律返回 None。"""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM oidc_auth_codes WHERE code_hash = ?", (code_hash,)
            ).fetchone()
            if row is None or row["consumed_at"] is not None:
                return None
            if row["expires_at"] <= now:
                return None
            conn.execute(
                "UPDATE oidc_auth_codes SET consumed_at = ?"
                " WHERE code_hash = ? AND consumed_at IS NULL",
                (now, code_hash),
            )
            return row

    # ---- 设备码 ----

    def insert_device_code(
        self,
        *,
        device_code_hash: str,
        user_code: str,
        client_id: str,
        scope: str,
        interval_sec: int,
        created_at: str,
        expires_at: str,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO oidc_device_codes (device_code_hash, user_code, client_id,
                                           scope, status, interval_sec,
                                           created_at, expires_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                device_code_hash,
                user_code,
                client_id,
                scope,
                interval_sec,
                created_at,
                expires_at,
            ),
        )

    def get_device_by_user_code(self, user_code: str) -> sqlite3.Row | None:
        return self._db.query_one(
            "SELECT * FROM oidc_device_codes WHERE user_code = ?", (user_code,)
        )

    def get_device_by_hash(self, device_code_hash: str) -> sqlite3.Row | None:
        return self._db.query_one(
            "SELECT * FROM oidc_device_codes WHERE device_code_hash = ?",
            (device_code_hash,),
        )

    def approve_device(
        self,
        *,
        user_code: str,
        user_id: str,
        status: str,
        now: str,
    ) -> tuple[str, sqlite3.Row | None]:
        """返回 (结果, 行)。结果：ok / not_found / conflict。"""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM oidc_device_codes WHERE user_code = ?", (user_code,)
            ).fetchone()
            if row is None or row["expires_at"] <= now:
                return "not_found", None
            if row["status"] != "pending":
                return "conflict", row
            conn.execute(
                "UPDATE oidc_device_codes"
                " SET status = ?, user_id = ?, approved_at = ?"
                " WHERE user_code = ? AND status = 'pending'",
                (status, user_id, now, user_code),
            )
            updated = conn.execute(
                "SELECT * FROM oidc_device_codes WHERE user_code = ?", (user_code,)
            ).fetchone()
            return "ok", updated

    def mark_device_polled(self, device_code_hash: str, now: str) -> None:
        self._db.execute(
            "UPDATE oidc_device_codes SET last_polled_at = ?"
            " WHERE device_code_hash = ?",
            (now, device_code_hash),
        )

    def consume_device_code(self, device_code_hash: str, now: str) -> sqlite3.Row | None:
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM oidc_device_codes WHERE device_code_hash = ?",
                (device_code_hash,),
            ).fetchone()
            if row is None or row["status"] != "approved":
                return None
            if row["expires_at"] <= now:
                return None
            conn.execute(
                "UPDATE oidc_device_codes SET status = 'consumed'"
                " WHERE device_code_hash = ? AND status = 'approved'",
                (device_code_hash,),
            )
            return row

    # ---- 令牌 ----

    def insert_token(
        self,
        *,
        token_id: str,
        kind: str,
        token_hash: str,
        client_id: str,
        user_id: str,
        scope: str,
        parent_id: str | None,
        issued_at: str,
        expires_at: str,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO oidc_tokens (id, kind, token_hash, client_id, user_id,
                                     scope, parent_id, issued_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token_id,
                kind,
                token_hash,
                client_id,
                user_id,
                scope,
                parent_id,
                issued_at,
                expires_at,
            ),
        )

    def get_token_by_hash(self, token_hash: str) -> sqlite3.Row | None:
        return self._db.query_one(
            "SELECT * FROM oidc_tokens WHERE token_hash = ?", (token_hash,)
        )

    def revoke_token(self, token_hash: str, now: str) -> int:
        return self._db.execute(
            "UPDATE oidc_tokens SET revoked_at = ?"
            " WHERE token_hash = ? AND revoked_at IS NULL",
            (now, token_hash),
        )

    # ---- 清理 ----

    def purge_expired(self, now: str) -> None:
        self._db.execute("DELETE FROM oidc_auth_codes WHERE expires_at < ?", (now,))
        self._db.execute("DELETE FROM oidc_device_codes WHERE expires_at < ?", (now,))
        self._db.execute(
            "DELETE FROM oidc_tokens WHERE expires_at < ? AND revoked_at IS NOT NULL",
            (now,),
        )

    def all_tokens_for_user(self, user_id: str) -> list[sqlite3.Row]:
        return self._db.query_all(
            "SELECT * FROM oidc_tokens WHERE user_id = ? AND revoked_at IS NULL",
            (user_id,),
        )

    def stats(self) -> dict[str, Any]:
        clients = self._db.query_one("SELECT COUNT(*) AS n FROM oidc_clients")
        tokens = self._db.query_one(
            "SELECT COUNT(*) AS n FROM oidc_tokens WHERE revoked_at IS NULL"
        )
        return {
            "clients": int(clients["n"]) if clients else 0,
            "active_tokens": int(tokens["n"]) if tokens else 0,
        }
