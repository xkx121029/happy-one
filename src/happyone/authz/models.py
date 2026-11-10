"""授权线数据结构（不依赖 FastAPI）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    name: str
    client_type: str
    secret_hash: str | None
    redirect_uris: list[str]
    scopes: list[str]
    status: str
    created_at: str

    @property
    def is_confidential(self) -> bool:
        return self.client_type == "confidential"

    def allows_redirect(self, uri: str) -> bool:
        return uri in self.redirect_uris


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    token_type: str
    expires_in: int
    scope: str
    refresh_token: str | None = None
    id_token: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "scope": self.scope,
        }
        if self.refresh_token:
            payload["refresh_token"] = self.refresh_token
        if self.id_token:
            payload["id_token"] = self.id_token
        return payload


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_code": self.device_code,
            "user_code": self.user_code,
            "verification_uri": self.verification_uri,
            "verification_uri_complete": self.verification_uri_complete,
            "expires_in": self.expires_in,
            "interval": self.interval,
        }


@dataclass(frozen=True)
class AccessTokenInfo:
    user_id: str
    client_id: str
    scope: str
    expires_at: str
