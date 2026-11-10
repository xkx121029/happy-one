"""授权线 HTTP 路由：OIDC 发现、JWKS、授权码、设备码、令牌、userinfo。

OIDC 端点错误按 RFC 6749 返回，不使用业务错误包络（见 docs/API.md 1.4）。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from happyone.audit.service import AuditService
from happyone.authz.service import OAuthService
from happyone.core import errors
from happyone.core.errors import AppError, OAuthError
from happyone.deps import (
    Principal,
    get_audit,
    get_bearer_token,
    get_identity,
    get_ip,
    get_oauth,
    get_request_id,
    get_settings,
    require_session,
)
from happyone.identity.service import IdentityService

router = APIRouter(tags=["oidc"])

_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


@router.get("/.well-known/openid-configuration")
def openid_configuration(
    request: Request,
    settings: Any = Depends(get_settings),
) -> dict[str, Any]:
    issuer = settings.issuer.rstrip("/")
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "userinfo_endpoint": f"{issuer}/oauth/userinfo",
        "jwks_uri": f"{issuer}/.well-known/jwks.json",
        "device_authorization_endpoint": f"{issuer}/oauth/device_authorization",
        "revocation_endpoint": f"{issuer}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": [
            "authorization_code",
            "refresh_token",
            _DEVICE_GRANT,
        ],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["openid", "profile", "email", "offline_access"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["EdDSA"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
        "claims_supported": [
            "iss",
            "sub",
            "aud",
            "exp",
            "iat",
            "nonce",
            "preferred_username",
            "name",
            "email",
            "email_verified",
        ],
    }


@router.get("/.well-known/jwks.json")
def jwks(request: Request) -> dict[str, Any]:
    return request.app.state.oidc_signer.jwks()


@router.get("/oauth/authorize")
def authorize(
    request: Request,
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    scope: str | None = Query(default=None),
    state: str | None = Query(default=None),
    code_challenge: str | None = Query(default=None),
    code_challenge_method: str = Query(default="S256"),
    nonce: str | None = Query(default=None),
    principal: Principal = Depends(require_session),
    oauth: OAuthService = Depends(get_oauth),
    audit: AuditService = Depends(get_audit),
) -> RedirectResponse:
    if response_type != "code":
        raise OAuthError("unsupported_response_type", "仅支持 response_type=code")
    if not code_challenge:
        raise OAuthError("invalid_request", "缺少 code_challenge")

    code = oauth.create_authorization_code(
        client_id=client_id,
        user_id=principal.user_id,
        redirect_uri=redirect_uri,
        scope=scope or "",
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        nonce=nonce,
    )

    params: dict[str, str] = {"code": code}
    if state:
        params["state"] = state
    separator = "&" if "?" in redirect_uri else "?"
    audit.record(
        action="oauth.authorize",
        actor_user_id=principal.user_id,
        target_type="client",
        target_id=client_id,
        request_id=get_request_id(request),
        ip=get_ip(request),
        detail={"scope": scope},
    )
    return RedirectResponse(
        f"{redirect_uri}{separator}{urlencode(params)}", status_code=302
    )


@router.post("/oauth/token")
async def token(
    request: Request,
    oauth: OAuthService = Depends(get_oauth),
    audit: AuditService = Depends(get_audit),
) -> JSONResponse:
    form = await _read_params(request)
    grant_type = form.get("grant_type", "")
    client_id = form.get("client_id", "")

    if grant_type == "authorization_code":
        issued = oauth.exchange_authorization_code(
            code=form.get("code", ""),
            client_id=client_id,
            redirect_uri=form.get("redirect_uri", ""),
            code_verifier=form.get("code_verifier", ""),
            client_secret=form.get("client_secret"),
        )
    elif grant_type == "refresh_token":
        issued = oauth.exchange_refresh_token(
            refresh_token=form.get("refresh_token", ""),
            client_id=client_id,
            client_secret=form.get("client_secret"),
        )
    elif grant_type == _DEVICE_GRANT:
        issued = oauth.exchange_device_code(
            device_code=form.get("device_code", ""), client_id=client_id
        )
    else:
        raise OAuthError("unsupported_grant_type", "不支持的 grant_type")

    audit.record(
        action="oauth.token",
        target_type="client",
        target_id=client_id,
        request_id=get_request_id(request),
        ip=get_ip(request),
        detail={"grant_type": grant_type, "scope": issued.scope},
    )
    return JSONResponse(issued.to_dict())


@router.post("/oauth/device_authorization")
async def device_authorization(
    request: Request,
    oauth: OAuthService = Depends(get_oauth),
) -> JSONResponse:
    form = await _read_params(request)
    authorization = oauth.device_authorization(
        client_id=form.get("client_id", ""), scope=form.get("scope")
    )
    return JSONResponse(authorization.to_dict())


@router.post("/oauth/device/approve")
async def device_approve(
    request: Request,
    principal: Principal = Depends(require_session),
    oauth: OAuthService = Depends(get_oauth),
    audit: AuditService = Depends(get_audit),
) -> dict[str, Any]:
    form = await _read_params(request)
    result = oauth.approve_device(
        user_code=form.get("user_code", ""),
        user_id=principal.user_id,
        action=form.get("action", "approve"),
    )
    audit.record(
        action="oauth.device.approve",
        actor_user_id=principal.user_id,
        target_type="client",
        target_id=result["client_id"],
        request_id=get_request_id(request),
        ip=get_ip(request),
        detail={"status": result["status"], "scope": result["scope"]},
    )
    return result


@router.get("/oauth/userinfo")
def userinfo(
    token: str | None = Depends(get_bearer_token),
    oauth: OAuthService = Depends(get_oauth),
    identity: IdentityService = Depends(get_identity),
) -> dict[str, Any]:
    if not token:
        raise OAuthError("invalid_token", "缺少访问令牌", 401)
    try:
        info = oauth.verify_access_token(token)
    except AppError as exc:
        raise OAuthError("invalid_token", exc.message, 401) from exc

    scopes = info.scope.split()
    if "openid" not in scopes:
        raise OAuthError("insufficient_scope", "令牌缺少 openid scope", 403)

    user = identity.get_user(info.user_id)
    claims: dict[str, Any] = {"sub": user.id}
    if "profile" in scopes:
        claims["preferred_username"] = user.username
        claims["name"] = user.display_name
    if "email" in scopes:
        claims["email"] = user.email
        claims["email_verified"] = False
    return claims


@router.post("/oauth/revoke")
async def revoke(
    request: Request,
    oauth: OAuthService = Depends(get_oauth),
) -> dict[str, Any]:
    form = await _read_params(request)
    token_value = form.get("token", "")
    client_id = form.get("client_id", "")
    if not token_value or not client_id:
        raise OAuthError("invalid_request", "缺少 token 或 client_id")
    try:
        oauth.revoke(token=token_value, client_id=client_id)
    except AppError as exc:
        raise OAuthError("invalid_client", exc.message, 401) from exc
    return {}


async def _read_params(request: Request) -> dict[str, str]:
    """兼容表单与 JSON 请求体，避免引入 python-multipart 依赖。"""
    content_type = request.headers.get("content-type", "").lower()
    raw = await request.body()
    if "application/json" in content_type:
        try:
            data = json.loads(raw or b"{}")
        except ValueError as exc:
            raise OAuthError("invalid_request", "请求体不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise OAuthError("invalid_request", "请求体必须是 JSON 对象")
        return {str(key): "" if value is None else str(value) for key, value in data.items()}
    if "application/x-www-form-urlencoded" in content_type or not raw:
        try:
            parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError as exc:
            raise OAuthError("invalid_request", "请求体编码不合法") from exc
        return {key: values[0] for key, values in parsed.items()}
    raise OAuthError("invalid_request", "仅支持表单或 JSON 请求体")
