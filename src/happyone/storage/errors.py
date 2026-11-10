"""存储模块错误类型，以及 happy-hub 响应包络到中文错误的映射。

happy-hub 成功返回 ``{"ok": true, ...}``，失败返回
``{"ok": false, "code": "EXXX", "error": "中文"}``。

对外暴露的错误对象统一带三层信息：
1. ``code``：本模块错误码（ST- 前缀），调用方可以稳定判断；
2. ``message``：给最终使用者看的中文文案；
3. ``hub_code`` / ``detail``：排障用，保留 hub 原始错误码与原始文案。
"""

from __future__ import annotations

from typing import Any, Mapping

CODE_INVALID = "ST-INVALID"
CODE_NOT_FOUND = "ST-NOT-FOUND"
CODE_CONFLICT = "ST-CONFLICT"
CODE_AUTH = "ST-AUTH"
CODE_THROTTLED = "ST-THROTTLED"
CODE_TOO_LARGE = "ST-TOO-LARGE"
CODE_UNAVAILABLE = "ST-UNAVAILABLE"
CODE_BAD_RESPONSE = "ST-BAD-RESPONSE"
CODE_HUB = "ST-HUB"

# HTTP 状态码 -> (本模块错误码, 中文文案)
HUB_HTTP_MESSAGES: dict[int, tuple[str, str]] = {
    400: (CODE_INVALID, "存储服务拒绝了这个请求，请检查桶与对象名是否符合规范"),
    401: (CODE_AUTH, "存储服务鉴权失败，请检查 happy-hub 令牌配置"),
    403: (CODE_AUTH, "存储服务拒绝访问，当前令牌没有该桶的操作权限"),
    404: (CODE_NOT_FOUND, "对象不存在或已被删除"),
    405: (CODE_INVALID, "存储服务不支持该操作，请检查接口版本"),
    409: (CODE_CONFLICT, "对象状态冲突，请稍后重试或改用新的对象名"),
    413: (CODE_TOO_LARGE, "文件超出存储服务允许的大小"),
    415: (CODE_INVALID, "存储服务不接受该文件类型"),
    422: (CODE_INVALID, "存储服务无法解析请求内容"),
    429: (CODE_THROTTLED, "存储服务限流，请稍后重试"),
    500: (CODE_UNAVAILABLE, "存储服务内部错误，请稍后重试"),
    502: (CODE_UNAVAILABLE, "存储服务网关异常，请稍后重试"),
    503: (CODE_UNAVAILABLE, "存储服务暂时不可用，请稍后重试"),
    504: (CODE_UNAVAILABLE, "存储服务响应超时，请稍后重试"),
}

# hub 业务错误码 -> 中文文案覆盖表。
# hub 的 EXXX 含义随部署版本变化，默认只按 HTTP 状态给出语义；如果部署方约定了
# 固定错误码语义，可在启动时调用 register_hub_code_message() 补充，或在部署配置
# 中直接填这张表。未登记的码会回退到 HTTP 状态映射，并保留 hub 原始文案。
HUB_CODE_MESSAGES: dict[str, str] = {}

# 这些 hub 错误码即使 HTTP 状态是 2xx 也视为业务失败，只用于文案覆盖时的提示。
_UNKNOWN_STATUS = (CODE_HUB, "存储服务返回了未预期的结果，请稍后重试")


def register_hub_code_message(code: str, message: str) -> None:
    """登记一个 hub 业务错误码的中文文案（供部署方按实际版本补充）。"""

    normalized = str(code).strip().upper()
    if not normalized:
        raise ValueError("hub 错误码不能为空")
    HUB_CODE_MESSAGES[normalized] = message


class StorageError(Exception):
    """存储模块的统一异常；``status`` 可直接作为 HTTP 响应码。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        hub_code: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.hub_code = hub_code
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志可读性
        return self.message

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": False, "code": self.code, "error": self.message}
        if self.hub_code:
            payload["hub_code"] = self.hub_code
        if self.detail:
            payload["detail"] = self.detail
        return payload


def invalid_argument(message: str) -> StorageError:
    return StorageError(CODE_INVALID, message, status=400)


def not_found(message: str = "对象不存在或已被删除") -> StorageError:
    return StorageError(CODE_NOT_FOUND, message, status=404)


def unavailable(message: str, *, detail: str | None = None) -> StorageError:
    return StorageError(CODE_UNAVAILABLE, message, status=503, detail=detail)


def bad_response(detail: str | None = None) -> StorageError:
    return StorageError(
        CODE_BAD_RESPONSE,
        "存储服务返回了无法解析的响应，请稍后重试",
        status=502,
        detail=detail,
    )


def error_from_hub(status: int, payload: Mapping[str, Any] | None) -> StorageError:
    """把 hub 的失败包络翻译成对使用者有意义的中文错误。"""

    body = payload or {}
    hub_code = str(body.get("code") or "").strip().upper() or None
    hub_error = str(body.get("error") or "").strip()

    if hub_code and hub_code in HUB_CODE_MESSAGES:
        mapped_code, mapped_message = _status_mapping(status)
        return StorageError(
            mapped_code,
            HUB_CODE_MESSAGES[hub_code],
            status=_http_status(status),
            hub_code=hub_code,
            detail=hub_error or None,
        )

    mapped_code, mapped_message = _status_mapping(status)
    if hub_error:
        detail = f"{hub_code}: {hub_error}" if hub_code else hub_error
        message = f"{mapped_message}（存储服务：{hub_error}）"
    else:
        detail = hub_code
        message = mapped_message

    return StorageError(
        mapped_code,
        message,
        status=_http_status(status),
        hub_code=hub_code,
        detail=detail,
    )


def _status_mapping(status: int) -> tuple[str, str]:
    if status in HUB_HTTP_MESSAGES:
        return HUB_HTTP_MESSAGES[status]
    if 400 <= status < 500:
        return CODE_INVALID, "存储服务拒绝了这次请求，请检查参数后重试"
    if status >= 500:
        return CODE_UNAVAILABLE, "存储服务暂时不可用，请稍后重试"
    return _UNKNOWN_STATUS


def _http_status(status: int) -> int:
    """把远端状态映射成本模块对外返回的状态，避免 5xx 被原样透传。"""

    if status in (400, 401, 403, 404, 409, 413, 415, 422, 429):
        return status
    if status >= 500:
        return 503
    return 502
