"""统一错误类型与错误包络。

业务错误使用 AppError（中文文案 + 机器可读 code），由 app.py 的异常处理器
转换成统一包络。OAuth 端点使用 OAuthError，按 RFC 6749 输出，不复用包络。
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """业务异常。code 供客户端判断，message 为中文提示。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details

    def to_envelope(self, request_id: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
            "request_id": request_id,
        }


class OAuthError(Exception):
    """OAuth2 / OIDC 标准错误，输出 {"error": ..., "error_description": ...}。"""

    def __init__(
        self,
        error: str,
        description: str | None = None,
        status_code: int = 400,
    ) -> None:
        super().__init__(error)
        self.error = error
        self.description = description
        self.status_code = status_code

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.error}
        if self.description:
            payload["error_description"] = self.description
        return payload


# ---- 常用错误构造器（避免各线重复拼 code 与文案） ----


def unauthorized(message: str = "未登录或令牌已失效") -> AppError:
    return AppError("E_UNAUTHORIZED", message, 401)


def forbidden(message: str = "权限不足") -> AppError:
    return AppError("E_FORBIDDEN", message, 403)


def not_found(message: str = "资源不存在") -> AppError:
    return AppError("E_NOT_FOUND", message, 404)


def conflict(message: str) -> AppError:
    return AppError("E_CONFLICT", message, 409)


def validation(message: str, details: dict[str, Any] | None = None) -> AppError:
    return AppError("E_VALIDATION", message, 422, details)


def unavailable(message: str = "服务暂不可用") -> AppError:
    return AppError("E_UNAVAILABLE", message, 503)
