"""授权线业务逻辑（纯 Python，不依赖 FastAPI）。

协议：OIDC Authorization Code + PKCE(S256)、Device Authorization、Refresh Token。
- access token / refresh token 为不透明随机串，库里只存 sha256。
- id_token 为 EdDSA(Ed25519) compact JWS，声明由用户目录提供。
- 所有客户端都必须使用 PKCE，不开放动态注册。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from datetime import datetime
from typing import Any, Protocol

from happyone.authz.models import (
    AccessTokenInfo,
    DeviceAuthorization,
    IssuedTokens,
    OAuthClient,
)
from happyone.authz.repository import OAuthRepository
from happyone.config import Settings
from happyone.core import errors
from happyone.core.errors import OAuthError
from happyone.core.ids import new_id, new_user_code, normalize_user_code
from happyone.core.security import (
    Ed25519Signer,
    b64url_encode,
    random_token,
    sha256_hex,
    token_fingerprint,
)
from happyone.core.timeutil import now_iso, now_utc, parse_iso
from happyone.db import Database

_CLIENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,63}$")
_FORBIDDEN_REDIRECT_SCHEMES = ("javascript", "data", "file", "vbscript")
_MAX_SCOPE_ITEMS = 16
_DEFAULT_SCOPE = "openid profile"


class UserDirectory(Protocol):
    """授权线只需要读用户基本信息，由路由层注入身份线实现。"""

    def get_user(self, user_id: str) -> Any:  # pragma: no cover - 结构协议
        ...


def _to_client(row: sqlite3.Row) -> OAuthClient:
    try:
        redirect_uris = json.loads(row["redirect_uris"] or "[]")
    except ValueError:
        redirect_uris = []
    return OAuthClient(
        client_id=row["client_id"],
        name=row["name"],
        client_type=row["client_type"],
        secret_hash=row["secret_hash"],
        redirect_uris=[uri for uri in redirect_uris if isinstance(uri, str)],
        scopes=(row["scopes"] or "").split(),
        status=row["status"],
        created_at=row["created_at"],
    )


def _normalize_scope(raw: str | None, allowed: list[str]) -> str:
    requested = (raw or "").split()
    if not requested:
        requested = [item for item in _DEFAULT_SCOPE.split() if item in allowed]
    if len(requested) > _MAX_SCOPE_ITEMS:
        raise OAuthError("invalid_scope", "请求的 scope 过多")
    seen: list[str] = []
    for item in requested:
        if item not in allowed:
            raise OAuthError("invalid_scope", f"未授权的 scope：{item}")
        if item not in seen:
            seen.append(item)
    return " ".join(seen)


def _scope_list(scope: str) -> list[str]:
    return scope.split()


class OAuthService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        signer: Ed25519Signer,
        users: UserDirectory | None = None,
    ) -> None:
        self._repo = OAuthRepository(db)
        self._settings = settings
        self._signer = signer
        self._users = users

    # ---- 客户端 ----

    def create_client(
        self,
        *,
        client_id: str,
        name: str,
        client_type: str = "public",
        redirect_uris: list[str],
        scopes: list[str],
        client_secret: str | None = None,
    ) -> tuple[OAuthClient, str | None]:
        """登记客户端；机密客户端未提供密钥时生成一次性明文返回。"""
        client_id = (client_id or "").strip().lower()
        if not _CLIENT_ID_RE.match(client_id):
            raise errors.validation(
                "client_id 需 3–64 位，仅限小写字母、数字与 _ . -"
            )
        if client_type not in ("public", "confidential"):
            raise errors.validation("client_type 只能是 public 或 confidential")
        if not redirect_uris:
            raise errors.validation("至少需要一个 redirect_uri")
        for uri in redirect_uris:
            self._validate_redirect_uri(uri)
        if not scopes:
            raise errors.validation("至少需要一个 scope")
        for scope in scopes:
            if not scope or any(ch.isspace() for ch in scope):
                raise errors.validation(f"scope 不合法：{scope}")

        plaintext_secret: str | None = None
        secret_hash: str | None = None
        if client_type == "confidential":
            plaintext_secret = client_secret or secrets.token_urlsafe(32)
            secret_hash = sha256_hex(plaintext_secret)

        try:
            self._repo.create_client(
                client_id=client_id,
                name=(name or client_id).strip()[:64],
                client_type=client_type,
                secret_hash=secret_hash,
                redirect_uris_json=json.dumps(redirect_uris, ensure_ascii=False),
                scopes=" ".join(scopes),
                created_at=now_iso(),
            )
        except sqlite3.IntegrityError as exc:
            raise errors.conflict("client_id 已存在") from exc

        client = self.get_client(client_id)
        return client, plaintext_secret

    def get_client(self, client_id: str) -> OAuthClient:
        row = self._repo.get_client((client_id or "").strip())
        if row is None:
            raise OAuthError("invalid_client", "客户端不存在", 401)
        client = _to_client(row)
        if client.status != "active":
            raise OAuthError("invalid_client", "客户端已停用", 401)
        return client

    def list_clients(self) -> list[OAuthClient]:
        return [_to_client(row) for row in self._repo.list_clients()]

    # ---- 授权码 ----

    def create_authorization_code(
        self,
        *,
        client_id: str,
        user_id: str,
        redirect_uri: str,
        scope: str,
        code_challenge: str,
        code_challenge_method: str,
        nonce: str | None,
    ) -> str:
        client = self.get_client(client_id)
        if not client.allows_redirect(redirect_uri):
            raise OAuthError("invalid_request", "redirect_uri 不在白名单内")
        normalized_scope = _normalize_scope(scope, client.scopes)
        if "openid" not in normalized_scope:
            raise OAuthError("invalid_scope", "授权码流程必须包含 openid")
        if code_challenge_method != "S256":
            raise OAuthError("invalid_request", "仅支持 code_challenge_method=S256")
        if not (43 <= len(code_challenge or "") <= 128):
            raise OAuthError("invalid_request", "code_challenge 长度不合法")

        code = random_token("ho_ac_")
        self._repo.insert_auth_code(
            code_hash=token_fingerprint(code),
            client_id=client.client_id,
            user_id=user_id,
            redirect_uri=redirect_uri,
            scope=normalized_scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            nonce=nonce,
            created_at=now_iso(),
            expires_at=_after(self._settings.auth_code_ttl_sec),
        )
        return code

    def exchange_authorization_code(
        self,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_verifier: str,
        client_secret: str | None,
    ) -> IssuedTokens:
        client = self._authenticate_client(client_id, client_secret)
        row = self._repo.consume_auth_code(token_fingerprint(code or ""), now_iso())
        if row is None:
            raise OAuthError("invalid_grant", "授权码无效、已使用或已过期")
        if row["client_id"] != client.client_id:
            raise OAuthError("invalid_grant", "授权码与客户端不匹配")
        if row["redirect_uri"] != redirect_uri:
            raise OAuthError("invalid_grant", "redirect_uri 不匹配")

        verifier = code_verifier or ""
        if not (43 <= len(verifier) <= 128):
            raise OAuthError("invalid_grant", "code_verifier 长度不合法")
        computed = b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
        if not hmac.compare_digest(computed, row["code_challenge"]):
            raise OAuthError("invalid_grant", "PKCE 校验失败")

        return self._issue_tokens(
            client=client,
            user_id=row["user_id"],
            scope=row["scope"],
            nonce=row["nonce"],
        )

    def exchange_refresh_token(
        self,
        *,
        refresh_token: str,
        client_id: str,
        client_secret: str | None,
    ) -> IssuedTokens:
        client = self._authenticate_client(client_id, client_secret)
        row = self._repo.get_token_by_hash(token_fingerprint(refresh_token or ""))
        if row is None or row["kind"] != "refresh":
            raise OAuthError("invalid_grant", "refresh_token 无效")
        if row["client_id"] != client.client_id:
            raise OAuthError("invalid_grant", "refresh_token 与客户端不匹配")
        if row["revoked_at"] is not None:
            raise OAuthError("invalid_grant", "refresh_token 已吊销")
        if row["expires_at"] <= now_iso():
            raise OAuthError("invalid_grant", "refresh_token 已过期")

        self._repo.revoke_token(row["token_hash"], now_iso())
        return self._issue_tokens(
            client=client, user_id=row["user_id"], scope=row["scope"]
        )

    # ---- 设备码 ----

    def device_authorization(
        self, *, client_id: str, scope: str | None
    ) -> DeviceAuthorization:
        client = self.get_client(client_id)
        normalized_scope = _normalize_scope(scope, client.scopes)
        if "openid" not in normalized_scope:
            raise OAuthError("invalid_scope", "设备授权必须包含 openid")

        device_code = random_token("ho_dc_")
        user_code = self._allocate_user_code()
        self._repo.insert_device_code(
            device_code_hash=token_fingerprint(device_code),
            user_code=user_code,
            client_id=client.client_id,
            scope=normalized_scope,
            interval_sec=self._settings.device_poll_interval_sec,
            created_at=now_iso(),
            expires_at=_after(self._settings.device_code_ttl_sec),
        )
        base = self._settings.issuer.rstrip("/")
        return DeviceAuthorization(
            device_code=device_code,
            user_code=user_code,
            verification_uri=f"{base}/device",
            verification_uri_complete=f"{base}/device?user_code={user_code}",
            expires_in=self._settings.device_code_ttl_sec,
            interval=self._settings.device_poll_interval_sec,
        )

    def approve_device(
        self, *, user_code: str, user_id: str, action: str
    ) -> dict[str, Any]:
        if action not in ("approve", "deny"):
            raise errors.validation("action 只能是 approve 或 deny")
        code = normalize_user_code(user_code or "")
        if not code:
            raise errors.AppError("E_AUTH_INVALID_USER_CODE", "用户码不存在", 404)
        status = "approved" if action == "approve" else "denied"
        outcome, row = self._repo.approve_device(
            user_code=code, user_id=user_id, status=status, now=now_iso()
        )
        if outcome == "not_found":
            raise errors.AppError("E_AUTH_INVALID_USER_CODE", "用户码不存在或已过期", 404)
        if outcome == "conflict":
            raise errors.conflict("该用户码已被处理")
        assert row is not None
        return {
            "status": row["status"],
            "client_id": row["client_id"],
            "scope": row["scope"],
        }

    def exchange_device_code(
        self, *, device_code: str, client_id: str
    ) -> IssuedTokens:
        client = self.get_client(client_id)
        row = self._repo.get_device_by_hash(token_fingerprint(device_code or ""))
        if row is None:
            raise OAuthError("invalid_grant", "device_code 无效")
        if row["client_id"] != client.client_id:
            raise OAuthError("invalid_grant", "device_code 与客户端不匹配")

        now = now_iso()
        if row["status"] == "denied":
            raise OAuthError("access_denied", "用户拒绝了本次授权")
        if row["status"] == "consumed":
            raise OAuthError("invalid_grant", "device_code 已使用")
        if row["expires_at"] <= now:
            raise OAuthError("expired_token", "device_code 已过期")
        if row["status"] == "pending":
            last = row["last_polled_at"]
            if last is not None:
                elapsed = (parse_iso(now) - parse_iso(last)).total_seconds()
                if elapsed < int(row["interval_sec"]):
                    raise OAuthError("slow_down", "轮询过快，请降低频率")
            self._repo.mark_device_polled(row["device_code_hash"], now)
            raise OAuthError("authorization_pending", "等待用户在设备上确认")

        consumed = self._repo.consume_device_code(row["device_code_hash"], now)
        if consumed is None:
            raise OAuthError("invalid_grant", "device_code 不可用")
        return self._issue_tokens(
            client=client, user_id=consumed["user_id"], scope=consumed["scope"]
        )

    # ---- 令牌 ----

    def verify_access_token(self, token: str) -> AccessTokenInfo:
        row = self._repo.get_token_by_hash(token_fingerprint(token or ""))
        if row is None or row["kind"] != "access":
            raise errors.unauthorized("访问令牌无效")
        if row["revoked_at"] is not None:
            raise errors.unauthorized("访问令牌已吊销")
        if row["expires_at"] <= now_iso():
            raise errors.unauthorized("访问令牌已过期")
        return AccessTokenInfo(
            user_id=row["user_id"],
            client_id=row["client_id"],
            scope=row["scope"],
            expires_at=row["expires_at"],
        )

    def revoke(self, *, token: str, client_id: str) -> None:
        client = self.get_client(client_id)
        row = self._repo.get_token_by_hash(token_fingerprint(token or ""))
        if row is None:
            return
        if row["client_id"] != client.client_id:
            return
        self._repo.revoke_token(row["token_hash"], now_iso())

    def purge_expired(self) -> None:
        self._repo.purge_expired(now_iso())

    def stats(self) -> dict[str, Any]:
        return self._repo.stats()

    # ---- 内部 ----

    def _authenticate_client(
        self, client_id: str, client_secret: str | None
    ) -> OAuthClient:
        client = self.get_client(client_id)
        if client.is_confidential:
            if not client_secret or not client.secret_hash:
                raise OAuthError("invalid_client", "缺少客户端密钥", 401)
            if not hmac.compare_digest(client.secret_hash, sha256_hex(client_secret)):
                raise OAuthError("invalid_client", "客户端密钥不正确", 401)
        elif client_secret:
            raise OAuthError("invalid_client", "公开客户端不应携带密钥", 401)
        return client

    def _issue_tokens(
        self,
        *,
        client: OAuthClient,
        user_id: str,
        scope: str,
        nonce: str | None = None,
    ) -> IssuedTokens:
        scopes = _scope_list(scope)
        issued_at = now_iso()
        access_token = random_token("ho_at_")
        access_expires = _after(self._settings.access_token_ttl_sec)
        self._repo.insert_token(
            token_id=new_id("tok"),
            kind="access",
            token_hash=token_fingerprint(access_token),
            client_id=client.client_id,
            user_id=user_id,
            scope=scope,
            parent_id=None,
            issued_at=issued_at,
            expires_at=access_expires,
        )

        refresh_token: str | None = None
        if "offline_access" in scopes:
            refresh_token = random_token("ho_rt_")
            self._repo.insert_token(
                token_id=new_id("tok"),
                kind="refresh",
                token_hash=token_fingerprint(refresh_token),
                client_id=client.client_id,
                user_id=user_id,
                scope=scope,
                parent_id=None,
                issued_at=issued_at,
                expires_at=_after(self._settings.refresh_token_ttl_sec),
            )

        id_token: str | None = None
        if "openid" in scopes:
            id_token = self._sign_id_token(
                client=client,
                user_id=user_id,
                scopes=scopes,
                nonce=nonce,
                issued_at=issued_at,
            )

        return IssuedTokens(
            access_token=access_token,
            token_type="Bearer",
            expires_in=self._settings.access_token_ttl_sec,
            scope=scope,
            refresh_token=refresh_token,
            id_token=id_token,
        )

    def _sign_id_token(
        self,
        *,
        client: OAuthClient,
        user_id: str,
        scopes: list[str],
        nonce: str | None,
        issued_at: str,
    ) -> str:
        now_epoch = int(now_utc().timestamp())
        claims: dict[str, Any] = {
            "iss": self._settings.issuer,
            "sub": user_id,
            "aud": client.client_id,
            "iat": now_epoch,
            "exp": now_epoch + self._settings.access_token_ttl_sec,
        }
        if nonce:
            claims["nonce"] = nonce

        user = self._lookup_user(user_id)
        if user is not None and "profile" in scopes:
            claims["preferred_username"] = getattr(user, "username", None)
            claims["name"] = getattr(user, "display_name", None)
        if user is not None and "email" in scopes:
            claims["email"] = getattr(user, "email", None)
            claims["email_verified"] = False

        token = self._signer.sign_jwt(claims)
        return token

    def _lookup_user(self, user_id: str) -> Any:
        if self._users is None:
            return None
        try:
            return self._users.get_user(user_id)
        except Exception:  # noqa: BLE001 - 用户缺失不应阻断令牌签发
            return None

    def _allocate_user_code(self) -> str:
        for _ in range(5):
            candidate = new_user_code()
            if self._repo.get_device_by_user_code(candidate) is None:
                return candidate
        raise errors.unavailable("用户码分配失败，请重试")

    @staticmethod
    def _validate_redirect_uri(uri: str) -> None:
        value = (uri or "").strip()
        if not value or any(ch.isspace() for ch in value):
            raise errors.validation(f"redirect_uri 不合法：{uri!r}")
        scheme = value.split("://", 1)[0].lower() if "://" in value else ""
        if not scheme:
            raise errors.validation(f"redirect_uri 必须包含协议：{uri!r}")
        if scheme in _FORBIDDEN_REDIRECT_SCHEMES:
            raise errors.validation(f"不允许的 redirect_uri 协议：{scheme}")


def _after(seconds: int) -> str:
    from happyone.core.timeutil import iso_after

    return iso_after(seconds)
