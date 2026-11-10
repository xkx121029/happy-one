"""身份线业务逻辑（纯 Python，不依赖 FastAPI）。

不变量：
- 用户名统一小写存储，长度 3–32，只允许字母数字与 _ . -。
- 密码 8–128 字符，只存 scrypt 摘要。
- 会话令牌只存 sha256 摘要，过期或被吊销即不可用。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from happyone.config import Settings
from happyone.core import errors
from happyone.core.ids import new_id
from happyone.core.security import (
    hash_password,
    random_token,
    token_fingerprint,
    verify_password,
)
from happyone.core.timeutil import is_expired, iso_after, now_iso
from happyone.db import Database
from happyone.identity.models import Session, User
from happyone.identity.repository import IdentityRepository

_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PASSWORD_MIN = 8
_PASSWORD_MAX = 128
_MAX_PAGE_SIZE = 200
_DUMMY_HASH: str | None = None


def _dummy_password_hash() -> str:
    """未知账号也做一次等价开销的校验，降低登录时序差异。"""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("happy-one-dummy-password")
    return _DUMMY_HASH


def _to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        email=row["email"],
        display_name=row["display_name"],
        is_admin=bool(row["is_admin"]),
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_login_at=row["last_login_at"],
    )


def _to_session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        user_id=row["user_id"],
        device_name=row["device_name"],
        ip=row["ip"],
        user_agent=row["user_agent"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
    )


class IdentityService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._repo = IdentityRepository(db)
        self._settings = settings

    # ---- 用户 ----

    def create_user(
        self,
        *,
        username: str,
        password: str,
        email: str | None = None,
        display_name: str | None = None,
        is_admin: bool = False,
    ) -> User:
        username = self._validate_username(username)
        password = self._validate_password(password)
        email = self._validate_email(email)
        display_name = self._validate_display_name(display_name or username)

        if self._repo.get_user_by_login(username) is not None:
            raise errors.conflict("用户名已存在")

        user_id = new_id("usr")
        timestamp = now_iso()
        try:
            self._repo.create_user(
                user_id=user_id,
                username=username,
                email=email,
                display_name=display_name,
                password_hash=hash_password(password),
                is_admin=is_admin,
                created_at=timestamp,
            )
        except sqlite3.IntegrityError as exc:
            raise errors.conflict("用户名或邮箱已存在") from exc

        user = self.get_user(user_id)
        return user

    def count_users(self) -> int:
        return self._repo.count_users()

    def get_user(self, user_id: str) -> User:
        row = self._repo.get_user(user_id)
        if row is None:
            raise errors.not_found("用户不存在")
        return _to_user(row)

    def authenticate(self, login: str, password: str) -> User:
        normalized = (login or "").strip().lower()
        row = self._repo.get_user_by_login(normalized)
        if row is None:
            # 等价开销校验，避免明显的账号存在性时序差异
            verify_password(_dummy_password_hash(), password or "")
            raise errors.AppError(
                "E_AUTH_INVALID_CREDENTIALS", "用户名或密码不正确", 401
            )
        if not verify_password(row["password_hash"], password or ""):
            raise errors.AppError(
                "E_AUTH_INVALID_CREDENTIALS", "用户名或密码不正确", 401
            )
        if row["status"] != "active":
            raise errors.AppError("E_AUTH_DISABLED", "账号已停用", 403)

        self._repo.touch_login(row["id"], now_iso())
        return self.get_user(row["id"])

    def list_users(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        query: str | None = None,
    ) -> tuple[list[User], str | None]:
        limit = max(1, min(limit, _MAX_PAGE_SIZE))
        after = _decode_rowid_cursor(cursor)
        rows = self._repo.list_users(
            limit=limit + 1,
            after_rowid=after,
            query=(query or "").strip() or None,
        )
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = str(rows[-1]["_rowid"])
        return [_to_user(row) for row in rows], next_cursor

    def update_user(
        self,
        user_id: str,
        *,
        display_name: str | None = None,
        email: str | None = None,
        status: str | None = None,
        is_admin: bool | None = None,
    ) -> User:
        updates: dict[str, Any] = {"updated_at": now_iso()}

        if display_name is not None:
            updates["display_name"] = self._validate_display_name(display_name)
        if email is not None:
            updates["email"] = self._validate_email(email)
        if status is not None:
            if status not in ("active", "disabled"):
                raise errors.validation("status 只能是 active 或 disabled")
            updates["status"] = status
        if is_admin is not None:
            updates["is_admin"] = 1 if is_admin else 0

        try:
            self._repo.update_user(user_id, updates)
        except sqlite3.IntegrityError as exc:
            raise errors.conflict("邮箱已被使用") from exc

        if updates.get("status") == "disabled":
            self._repo.revoke_all(user_id, now_iso())
        return self.get_user(user_id)

    def set_password(
        self,
        user_id: str,
        new_password: str,
        *,
        keep_session_id: str | None = None,
    ) -> None:
        self.get_user(user_id)
        password = self._validate_password(new_password)
        self._repo.update_password(user_id, hash_password(password), now_iso())
        self._repo.revoke_all(user_id, now_iso(), except_session_id=keep_session_id)

    def change_password(
        self,
        user_id: str,
        *,
        current_password: str,
        new_password: str,
        keep_session_id: str | None,
    ) -> None:
        row = self._repo.get_user(user_id)
        if row is None:
            raise errors.not_found("用户不存在")
        if not verify_password(row["password_hash"], current_password or ""):
            raise errors.AppError(
                "E_AUTH_INVALID_CREDENTIALS", "当前密码不正确", 401
            )
        self.set_password(user_id, new_password, keep_session_id=keep_session_id)

    # ---- 会话 ----

    def create_session(
        self,
        user: User,
        *,
        device_name: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[str, Session]:
        token = random_token("ho_sess_")
        session = Session(
            id=new_id("ses"),
            user_id=user.id,
            device_name=(device_name or "").strip()[:64] or None,
            ip=ip,
            user_agent=(user_agent or "")[:256] or None,
            created_at=now_iso(),
            expires_at=iso_after(self._settings.session_ttl_sec),
            revoked_at=None,
        )
        self._repo.create_session(
            session_id=session.id,
            user_id=session.user_id,
            token_hash=token_fingerprint(token),
            device_name=session.device_name,
            ip=session.ip,
            user_agent=session.user_agent,
            created_at=session.created_at,
            expires_at=session.expires_at,
        )
        return token, session

    def resolve_session(self, token: str) -> tuple[User, Session]:
        row = self._repo.get_session_by_hash(token_fingerprint(token))
        if row is None:
            raise errors.unauthorized("会话不存在或已注销")
        if row["revoked_at"] is not None:
            raise errors.unauthorized("会话已注销")
        if is_expired(row["expires_at"]):
            self._repo.revoke_session_by_hash(token_fingerprint(token), now_iso())
            raise errors.unauthorized("会话已过期")
        if row["user_status"] != "active":
            raise errors.AppError("E_AUTH_DISABLED", "账号已停用", 403)

        session = _to_session(row)
        user = User(
            id=row["user_id"],
            username=row["username"],
            email=row["email"],
            display_name=row["display_name"],
            is_admin=bool(row["is_admin"]),
            status=row["user_status"],
            created_at=row["user_created_at"],
            updated_at=row["user_updated_at"],
            last_login_at=row["last_login_at"],
        )
        return user, session

    def revoke_session_token(self, token: str) -> None:
        self._repo.revoke_session_by_hash(token_fingerprint(token), now_iso())

    def revoke_session_by_id(self, user_id: str, session_id: str) -> None:
        changed = self._repo.revoke_session(session_id, user_id, now_iso())
        if changed == 0:
            exists = self._repo.get_session(session_id, user_id)
            if exists is None:
                raise errors.not_found("会话不存在")
            # 已吊销视为幂等成功

    def revoke_all(self, user_id: str, *, keep_session_id: str | None = None) -> None:
        self._repo.revoke_all(user_id, now_iso(), except_session_id=keep_session_id)

    def list_sessions(self, user: User, current_token: str) -> list[dict[str, Any]]:
        current_hash = token_fingerprint(current_token)
        rows = self._repo.list_sessions(user.id)
        result: list[dict[str, Any]] = []
        for row in rows:
            if row["revoked_at"] is not None:
                continue
            if is_expired(row["expires_at"]):
                continue
            result.append(
                _to_session(row).to_public_dict(current=row["token_hash"] == current_hash)
            )
        return result

    def purge_expired(self) -> int:
        return self._repo.purge_expired(now_iso())

    # ---- 校验 ----

    @staticmethod
    def _validate_username(value: str) -> str:
        username = (value or "").strip().lower()
        if not _USERNAME_RE.match(username):
            raise errors.validation(
                "用户名需 3–32 位，仅限小写字母、数字与 _ . -，且以字母或数字开头"
            )
        return username

    @staticmethod
    def _validate_password(value: str) -> str:
        if not isinstance(value, str) or not (_PASSWORD_MIN <= len(value) <= _PASSWORD_MAX):
            raise errors.validation(
                f"密码长度需在 {_PASSWORD_MIN}–{_PASSWORD_MAX} 个字符之间"
            )
        return value

    @staticmethod
    def _validate_email(value: str | None) -> str | None:
        if value is None:
            return None
        email = value.strip().lower()
        if not email:
            return None
        if len(email) > 254 or not _EMAIL_RE.match(email):
            raise errors.validation("邮箱格式不正确")
        return email

    @staticmethod
    def _validate_display_name(value: str) -> str:
        name = (value or "").strip()
        if not name or len(name) > 64:
            raise errors.validation("显示名需为 1–64 个字符")
        return name


def _decode_rowid_cursor(cursor: str | None) -> int | None:
    if not cursor:
        return None
    try:
        value = int(cursor)
    except (TypeError, ValueError) as exc:
        raise errors.validation("分页游标不合法") from exc
    return value
