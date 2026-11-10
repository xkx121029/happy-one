"""身份线 HTTP 路由。只做协议解析与审计，业务逻辑在 service.py。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from happyone.audit.service import AuditService
from happyone.core import errors
from happyone.core.errors import AppError
from happyone.deps import (
    Principal,
    get_audit,
    get_identity,
    get_ip,
    get_request_id,
    get_user_agent,
    require_admin,
    require_principal,
    require_session,
)
from happyone.identity.service import IdentityService

router = APIRouter(prefix="/api/v1", tags=["identity"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128)
    device_name: str | None = Field(default=None, max_length=64)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    email: str | None = None
    display_name: str | None = Field(default=None, max_length=64)
    is_admin: bool = False


class UpdateUserRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    email: str | None = None
    status: str | None = None
    is_admin: bool | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


@router.post("/auth/login")
def login(
    payload: LoginRequest,
    request: Request,
    identity: IdentityService = Depends(get_identity),
    audit: AuditService = Depends(get_audit),
) -> dict[str, Any]:
    request_id = get_request_id(request)
    ip = get_ip(request)
    try:
        user = identity.authenticate(payload.username, payload.password)
    except AppError as exc:
        audit.record(
            action="auth.login",
            outcome="failure",
            target_type="user",
            request_id=request_id,
            ip=ip,
            detail={"login": payload.username.strip().lower(), "reason": exc.code},
        )
        raise

    token, session = identity.create_session(
        user,
        device_name=payload.device_name,
        ip=ip,
        user_agent=get_user_agent(request),
    )
    audit.record(
        action="auth.login",
        actor_user_id=user.id,
        target_type="user",
        target_id=user.id,
        outcome="success",
        request_id=request_id,
        ip=ip,
        detail={"session_id": session.id, "device_name": session.device_name},
    )
    return {
        "token": token,
        "token_type": "Bearer",
        "expires_at": session.expires_at,
        "user": user.to_public_dict(),
    }


@router.post("/auth/logout", status_code=204)
def logout(
    request: Request,
    principal: Principal = Depends(require_principal),
    identity: IdentityService = Depends(get_identity),
    audit: AuditService = Depends(get_audit),
) -> Response:
    if principal.auth_method == "session":
        identity.revoke_session_token(principal.token)
    audit.record(
        action="auth.logout",
        actor_user_id=principal.user_id,
        target_type="session",
        target_id=principal.session_id,
        request_id=get_request_id(request),
        ip=get_ip(request),
    )
    return Response(status_code=204)


@router.post("/auth/logout-all", status_code=204)
def logout_all(
    request: Request,
    principal: Principal = Depends(require_session),
    identity: IdentityService = Depends(get_identity),
    audit: AuditService = Depends(get_audit),
) -> Response:
    identity.revoke_all(principal.user_id, keep_session_id=principal.session_id)
    audit.record(
        action="auth.logout_all",
        actor_user_id=principal.user_id,
        target_type="user",
        target_id=principal.user_id,
        request_id=get_request_id(request),
        ip=get_ip(request),
    )
    return Response(status_code=204)


@router.get("/me")
def me(principal: Principal = Depends(require_principal)) -> dict[str, Any]:
    return {
        "user": principal.user.to_public_dict(),
        "auth_method": principal.auth_method,
        "scopes": sorted(principal.scopes),
    }


@router.post("/me/password", status_code=204)
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    principal: Principal = Depends(require_session),
    identity: IdentityService = Depends(get_identity),
    audit: AuditService = Depends(get_audit),
) -> Response:
    identity.change_password(
        principal.user_id,
        current_password=payload.current_password,
        new_password=payload.new_password,
        keep_session_id=principal.session_id,
    )
    audit.record(
        action="auth.password_change",
        actor_user_id=principal.user_id,
        target_type="user",
        target_id=principal.user_id,
        request_id=get_request_id(request),
        ip=get_ip(request),
    )
    return Response(status_code=204)


@router.get("/auth/sessions")
def list_sessions(
    principal: Principal = Depends(require_session),
    identity: IdentityService = Depends(get_identity),
) -> dict[str, Any]:
    return {
        "items": identity.list_sessions(principal.user, principal.token),
        "next_cursor": None,
    }


@router.delete("/auth/sessions/{session_id}", status_code=204)
def revoke_session(
    session_id: str,
    request: Request,
    principal: Principal = Depends(require_session),
    identity: IdentityService = Depends(get_identity),
    audit: AuditService = Depends(get_audit),
) -> Response:
    identity.revoke_session_by_id(principal.user_id, session_id)
    audit.record(
        action="session.revoke",
        actor_user_id=principal.user_id,
        target_type="session",
        target_id=session_id,
        request_id=get_request_id(request),
        ip=get_ip(request),
    )
    return Response(status_code=204)


# ---- 用户管理（管理员） ----


@router.post("/users", status_code=201)
def create_user(
    payload: CreateUserRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
    identity: IdentityService = Depends(get_identity),
    audit: AuditService = Depends(get_audit),
) -> dict[str, Any]:
    user = identity.create_user(
        username=payload.username,
        password=payload.password,
        email=payload.email,
        display_name=payload.display_name,
        is_admin=payload.is_admin,
    )
    audit.record(
        action="user.create",
        actor_user_id=principal.user_id,
        target_type="user",
        target_id=user.id,
        request_id=get_request_id(request),
        ip=get_ip(request),
        detail={"username": user.username, "is_admin": user.is_admin},
    )
    return user.to_public_dict()


@router.get("/users")
def list_users(
    principal: Principal = Depends(require_admin),
    identity: IdentityService = Depends(get_identity),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    q: str | None = None,
) -> dict[str, Any]:
    users, next_cursor = identity.list_users(limit=limit, cursor=cursor, query=q)
    return {
        "items": [user.to_public_dict() for user in users],
        "next_cursor": next_cursor,
    }


@router.get("/users/{user_id}")
def get_user(
    user_id: str,
    principal: Principal = Depends(require_admin),
    identity: IdentityService = Depends(get_identity),
) -> dict[str, Any]:
    return identity.get_user(user_id).to_public_dict()


@router.patch("/users/{user_id}")
def update_user(
    user_id: str,
    payload: UpdateUserRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
    identity: IdentityService = Depends(get_identity),
    audit: AuditService = Depends(get_audit),
) -> dict[str, Any]:
    if payload.status is not None and payload.status not in ("active", "disabled"):
        raise errors.validation("status 只能是 active 或 disabled")
    user = identity.update_user(
        user_id,
        display_name=payload.display_name,
        email=payload.email,
        status=payload.status,
        is_admin=payload.is_admin,
    )
    audit.record(
        action="user.update",
        actor_user_id=principal.user_id,
        target_type="user",
        target_id=user.id,
        request_id=get_request_id(request),
        ip=get_ip(request),
        detail=payload.model_dump(exclude_none=True),
    )
    return user.to_public_dict()


@router.post("/users/{user_id}/password", status_code=204)
def reset_password(
    user_id: str,
    payload: ResetPasswordRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
    identity: IdentityService = Depends(get_identity),
    audit: AuditService = Depends(get_audit),
) -> Response:
    identity.set_password(user_id, payload.new_password)
    audit.record(
        action="user.password_reset",
        actor_user_id=principal.user_id,
        target_type="user",
        target_id=user_id,
        request_id=get_request_id(request),
        ip=get_ip(request),
    )
    return Response(status_code=204)
