"""身份线数据结构（不依赖 FastAPI）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class User:
    id: str
    username: str
    email: str | None
    display_name: str
    is_admin: bool
    status: str
    created_at: str
    updated_at: str
    last_login_at: str | None

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def to_public_dict(self) -> dict[str, Any]:
        """对外表示：绝不包含密码摘要。"""
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "display_name": self.display_name,
            "is_admin": self.is_admin,
            "status": self.status,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
        }


@dataclass(frozen=True)
class Session:
    id: str
    user_id: str
    device_name: str | None
    ip: str | None
    user_agent: str | None
    created_at: str
    expires_at: str
    revoked_at: str | None

    def to_public_dict(self, current: bool = False) -> dict[str, Any]:
        return {
            "id": self.id,
            "device_name": self.device_name,
            "ip": self.ip,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "current": current,
        }
