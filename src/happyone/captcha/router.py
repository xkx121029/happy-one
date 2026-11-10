"""HTTP 路由：由骨架线通过 ``app.include_router(router)`` 挂载。

业务错误统一返回 ``{"ok": false, "code": ..., "error": "中文"}``；
本模块只做参数装配与错误映射，判定逻辑全部在 :mod:`happyone.captcha.service`。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import service
from .models import (
    ChallengeResponse,
    SolutionRequest,
    SolveResponse,
    VerifyRequest,
    VerifyResponse,
)

router = APIRouter(prefix="/captcha/v1", tags=["captcha"])


def _client_ip(request: Request) -> str | None:
    # 本地单机部署：只用直连地址，不信任可伪造的转发头
    return request.client.host if request.client else None


def _fail(exc: service.CaptchaError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=exc.to_dict())


@router.post("/challenge", response_model=ChallengeResponse, summary="获取人机验证挑战")
def get_challenge() -> Any:
    return service.create_challenge()


@router.post("/solve", response_model=SolveResponse, summary="提交操作轨迹，换取票据")
def solve(payload: SolutionRequest, request: Request) -> Any:
    try:
        return service.submit_solution(payload, client_ip=_client_ip(request))
    except service.CaptchaError as exc:
        return _fail(exc)


@router.post("/verify", response_model=VerifyResponse, summary="校验并核销票据（内部使用）")
def verify(payload: VerifyRequest, request: Request) -> Any:
    try:
        return service.verify_ticket(
            payload.ticket, purpose=payload.purpose, client_ip=_client_ip(request)
        )
    except service.CaptchaError as exc:
        return _fail(exc)
