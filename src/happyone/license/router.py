"""许可证 HTTP 接口（模块级 ``router``，由骨架线挂载）。

统一响应包络：
- 成功：``{"ok": true, "data": {...}}``
- 失败：``{"ok": false, "code": "LICENSE_*", "error": "中文原因"}``

接口：
- POST /license/issue                签发
- GET  /license/public-key           获取公钥
- GET  /license/revocations/snapshot 获取吊销快照
- POST /license/revoke               吊销
- POST /license/verify               校验（离线验签 → 过期 → 吊销 → 设备绑定 → 宽限期）
- GET  /license/{license_id}         查询签发记录
"""

from __future__ import annotations

import functools
from typing import Any, Awaitable, Callable, Mapping

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from . import deps
from .keys import ensure_isolated_path, load_or_create
from .models import ErrorCode, IssueRequest
from .service import (
    LicenseError,
    LicenseNotFoundError,
    LicenseService,
    LicenseServiceUnavailable,
)
from .store import LicenseStore

router = APIRouter(prefix="/license", tags=["license"])

_service: LicenseService | None = None
_init_error: str | None = None


def build_license_service() -> LicenseService:
    """按约定从 settings / db 组装服务（首次调用时执行）。"""
    settings = deps.get_settings()
    key_path = ensure_isolated_path(
        deps.resolve_key_path(settings),
        hub_secret_path=deps.resolve_hub_secret_path(settings),
    )
    key_pair = load_or_create(key_path)
    store = LicenseStore(deps.get_db_conn)
    return LicenseService(key_pair, store=store, grace_days=deps.resolve_grace_days(settings))


def get_license_service() -> LicenseService | None:
    """FastAPI 依赖。初始化失败时返回 None，由端点输出 503 包络（测试可覆盖该依赖）。"""
    global _service, _init_error
    if _service is None and _init_error is None:
        try:
            _service = build_license_service()
        except Exception as exc:  # 密钥路径冲突、DB 不可用等
            _init_error = f"{type(exc).__name__}: {exc}"
    return _service


def reset_license_service() -> None:
    """测试辅助：清空单例与服务初始化错误。"""
    global _service, _init_error
    _service = None
    _init_error = None


class RequestBodyError(ValueError):
    """请求体字段不合法（统一转成 400 包络）。"""


def _svc(service: LicenseService | None) -> LicenseService:
    if service is None:
        raise LicenseServiceUnavailable(f"许可证服务初始化失败：{_init_error or '未知原因'}")
    return service


def _ok(data: Mapping[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True, "data": dict(data)}, status_code=status_code)


def _err(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "code": code, "error": message}, status_code=status_code)


def _envelope(func: Callable[..., Awaitable[JSONResponse]]) -> Callable[..., Awaitable[JSONResponse]]:
    """把异常统一翻译成包络，保证任何失败都带中文原因。"""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> JSONResponse:
        try:
            return await func(*args, **kwargs)
        except RequestBodyError as exc:
            return _err(400, ErrorCode.INVALID_REQUEST, str(exc))
        except LicenseError as exc:
            return _err(exc.status_code, exc.code, exc.message)
        except Exception as exc:  # 兜底，不把堆栈暴露给客户端
            return _err(500, ErrorCode.INTERNAL_ERROR, f"处理许可证请求时发生内部错误：{exc}")

    return wrapper


async def _read_json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise RequestBodyError("请求体必须是合法的 JSON") from exc
    if not isinstance(body, dict):
        raise RequestBodyError("请求体必须是 JSON 对象")
    return body


def _text(body: Mapping[str, Any], key: str, *, required: bool = False, max_len: int = 512) -> str | None:
    value = body.get(key)
    if value is None:
        if required:
            raise RequestBodyError(f"缺少必填字段 {key}")
        return None
    if not isinstance(value, str):
        raise RequestBodyError(f"字段 {key} 必须是字符串")
    if required and not value.strip():
        raise RequestBodyError(f"字段 {key} 不能为空")
    if len(value) > max_len:
        raise RequestBodyError(f"字段 {key} 长度不能超过 {max_len}")
    return value


def _integer(body: Mapping[str, Any], key: str) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RequestBodyError(f"字段 {key} 必须是整数")
    return value


def _string_list(body: Mapping[str, Any], key: str) -> list[str]:
    value = body.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RequestBodyError(f"字段 {key} 必须是字符串数组")
    return list(value)


def _issue_request(body: Mapping[str, Any]) -> IssueRequest:
    return IssueRequest(
        user_id=_text(body, "user_id", required=True, max_len=128) or "",
        product_slug=_text(body, "product_slug", required=True, max_len=64) or "",
        features=tuple(_string_list(body, "features")),
        expires_at=_integer(body, "expires_at"),
        expires_in_days=_integer(body, "expires_in_days"),
        device_fingerprint=_text(body, "device_fingerprint", max_len=512),
        max_devices=_integer(body, "max_devices"),
        license_id=_text(body, "license_id", max_len=64),
    )


@router.get("/public-key")
@_envelope
async def get_public_key(service: LicenseService | None = Depends(get_license_service)) -> JSONResponse:
    """分发验签公钥（可随客户端打包，离线验签用）。"""
    return _ok(_svc(service).public_key_info())


@router.get("/revocations/snapshot")
@_envelope
async def get_revocation_snapshot(
    service: LicenseService | None = Depends(get_license_service),
) -> JSONResponse:
    """导出已签名的吊销快照，客户端可离线判断某许可证是否被吊销。"""
    result = _svc(service).revocation_snapshot()
    return _ok(result.to_envelope()["data"])


@router.post("/issue")
@_envelope
async def issue_license(
    request: Request,
    service: LicenseService | None = Depends(get_license_service),
) -> JSONResponse:
    """签发许可证。请求体见 ``_issue_request`` 字段说明。"""
    body = await _read_json(request)
    issued = _svc(service).issue(_issue_request(body))
    return _ok(
        {
            "license_id": issued.payload.license_id,
            "kid": issued.payload.kid,
            "token": issued.token,
            "payload": issued.payload.to_dict(),
        },
        status_code=201,
    )


@router.post("/revoke")
@_envelope
async def revoke_license(
    request: Request,
    service: LicenseService | None = Depends(get_license_service),
) -> JSONResponse:
    """吊销许可证：写入吊销表，后续快照会包含该项。"""
    body = await _read_json(request)
    license_id = _text(body, "license_id", required=True, max_len=64) or ""
    reason = _text(body, "reason", max_len=200) or ""
    revoked_by = _text(body, "revoked_by", max_len=128)
    entry = _svc(service).revoke(license_id, reason=reason, revoked_by=revoked_by)
    return _ok(
        {
            "license_id": entry.license_id,
            "revoked_at": entry.revoked_at,
            "reason": entry.reason,
        }
    )


@router.post("/verify")
@_envelope
async def verify_license(
    request: Request,
    service: LicenseService | None = Depends(get_license_service),
) -> JSONResponse:
    """校验许可证。失败时同样返回 200 + ok=false，方便客户端统一解析。"""
    body = await _read_json(request)
    token = _text(body, "license", required=True, max_len=8192) or ""
    fingerprint = _text(body, "device_fingerprint", max_len=512)
    result = _svc(service).verify(token, device_fingerprint=fingerprint)
    return JSONResponse(result.to_envelope(), status_code=200)


@router.get("/{license_id}")
@_envelope
async def query_license(
    license_id: str,
    service: LicenseService | None = Depends(get_license_service),
) -> JSONResponse:
    """查询签发记录与吊销状态。"""
    if not license_id:
        raise LicenseNotFoundError("未找到许可证：编号为空")
    return _ok(_svc(service).describe(license_id))
