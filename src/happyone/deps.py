"""API 层公共依赖（地基线）：鉴权、服务装配、请求上下文。

放在应用根目录而不是 core/，因为这里需要同时组合身份线与授权线；
core/ 保持无业务依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from happyone.audit.service import AuditService
from happyone.authz.service import OAuthService
from happyone.config import Settings
from happyone.core import errors
from happyone.core.ids import new_id
from happyone.db import Database
from happyone.identity.models import User
from happyone.identity.service import IdentityService

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    """一次请求的调用者。会话令牌拥有全部 scope，OIDC 令牌按授权 scope。"""

    user: User
    auth_method: str
    scopes: frozenset[str]
    token: str
    session_id: str | None = None
    client_id: str | None = None

    @property
    def user_id(self) -> str:
        return self.user.id

    @property
    def is_admin(self) -> bool:
        return self.user.is_admin

    def has_scope(self, scope: str) -> bool:
        if self.auth_method == "session":
            return True
        return scope in self.scopes


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_identity(request: Request) -> IdentityService:
    state = request.app.state
    return IdentityService(state.db, state.settings)


def get_oauth(request: Request) -> OAuthService:
    state = request.app.state
    return OAuthService(
        state.db,
        state.settings,
        state.oidc_signer,
        users=IdentityService(state.db, state.settings),
    )


def get_audit(request: Request) -> AuditService:
    return AuditService(request.app.state.db)


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or new_id("req")


def get_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def get_user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


def get_bearer_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str | None:
    return credentials.credentials if credentials else None


def _resolve_principal(token: str, request: Request) -> Principal:
    identity = get_identity(request)

    if token.startswith("ho_at_"):
        oauth = get_oauth(request)
        info = oauth.verify_access_token(token)
        user = identity.get_user(info.user_id)
        if not user.is_active:
            raise errors.AppError("E_AUTH_DISABLED", "账号已停用", 403)
        return Principal(
            user=user,
            auth_method="oidc",
            scopes=frozenset(info.scope.split()),
            token=token,
            client_id=info.client_id,
        )

    user, session = identity.resolve_session(token)
    return Principal(
        user=user,
        auth_method="session",
        scopes=frozenset({"openid", "profile", "email", "offline_access"}),
        token=token,
        session_id=session.id,
    )


def get_optional_principal(
    request: Request,
    token: str | None = Depends(get_bearer_token),
) -> Principal | None:
    if not token:
        return None
    return _resolve_principal(token, request)


def require_principal(
    principal: Principal | None = Depends(get_optional_principal),
) -> Principal:
    if principal is None:
        raise errors.unauthorized()
    return principal


def require_session(
    principal: Principal = Depends(require_principal),
) -> Principal:
    if principal.auth_method != "session":
        raise errors.forbidden("该操作需要第一方登录会话")
    return principal


def require_admin(
    principal: Principal = Depends(require_principal),
) -> Principal:
    if not principal.is_admin:
        raise errors.forbidden("需要管理员权限")
    return principal


def require_scope(scope: str):
    """生成限定 scope 的依赖；会话令牌自动通过。"""

    def dependency(
        principal: Principal = Depends(require_principal),
    ) -> Principal:
        if not principal.has_scope(scope):
            raise errors.forbidden(f"缺少 scope：{scope}")
        return principal

    return dependency


def request_context(request: Request) -> dict[str, Any]:
    """审计常用上下文，避免各路由重复取值。"""
    return {
        "request_id": get_request_id(request),
        "ip": get_ip(request),
    }
