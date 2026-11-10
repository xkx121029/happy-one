"""happy-hub 存储服务的异步 HTTP 客户端。

只做协议封装：连接复用、超时、有限次退避重试、包络解析与错误翻译，
不包含任何业务命名或落库逻辑（那些在 service / repository 里）。

重试策略（与 README 保持一致）：
- 只对幂等操作重试：HEAD 探针、GET 元数据、PUT 对象、PUT 分片、POST 签名、
  POST 完成分片、DELETE 对象；
- 建分片会话 ``POST /api/v1/uploads`` 不重试，避免一次失败在 hub 侧留下
  多个孤儿会话；
- 重试对象：连接/读写超时、网络错误、408/429/5xx；
- 次数有上限（默认 2 次重试），退避 0.2s 起指数增长并带抖动，封顶 2s。
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, BinaryIO, Mapping
from urllib.parse import quote

import httpx

from .errors import bad_response, error_from_hub, unavailable

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)

DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_BASE = 0.2
DEFAULT_BACKOFF_MAX = 2.0
STREAM_BLOCK_SIZE = 1024 * 1024


class _FileStream(httpx.AsyncByteStream):
    """把本地文件包装成异步字节流，避免大文件整体读进内存。

    重试时可以 ``seek(0)`` 回到开头重新发送，因此附带一个 seek 方法。
    """

    def __init__(self, file: BinaryIO, block_size: int = STREAM_BLOCK_SIZE) -> None:
        self._file = file
        self._block_size = block_size

    async def __aiter__(self):
        while True:
            block = await asyncio.to_thread(self._file.read, self._block_size)
            if not block:
                break
            yield block

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._file.seek(offset, whence)


class HubClient:
    """一个应用一枚令牌，客户端内部复用一个 httpx.AsyncClient。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_sec: float = 10.0,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
        client: httpx.AsyncClient | None = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        normalized_base = str(base_url or "").strip().rstrip("/")
        normalized_token = str(token or "").strip()
        if not normalized_base:
            raise ValueError("happy-hub 地址不能为空")
        if not normalized_token:
            raise ValueError("happy-hub 令牌不能为空，请从配置读取")
        if max_retries < 0:
            raise ValueError("max_retries 不能为负数")

        self._base_url = normalized_base
        self._token = normalized_token
        self._max_retries = int(max_retries)
        self._backoff_base = max(0.0, float(backoff_base))
        self._backoff_max = max(0.0, float(backoff_max))
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=normalized_base,
            timeout=httpx.Timeout(
                connect=min(5.0, float(timeout_sec)),
                read=float(timeout_sec),
                write=float(timeout_sec),
                pool=min(5.0, float(timeout_sec)),
            ),
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=30.0,
            ),
        )

    def __repr__(self) -> str:  # pragma: no cover - 避免令牌进日志
        return f"HubClient(base_url={self._base_url!r}, token=***)"

    @property
    def base_url(self) -> str:
        return self._base_url

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "HubClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ 协议方法

    async def probe_blob(self, sha256: str) -> bool:
        """秒传探针：HEAD /api/v1/blobs/{sha256}，200 存在、404 不存在。"""

        response = await self._request("HEAD", f"/api/v1/blobs/{quote(sha256, safe='')}")
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        self._raise(response)
        return False  # pragma: no cover - _raise 必抛

    async def put_object(
        self,
        bucket: str,
        key: str,
        *,
        file: BinaryIO,
        size: int,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """小文件直传：PUT /api/v1/objects 原始字节流。"""

        headers = {
            "Content-Type": content_type or "application/octet-stream",
            "Content-Length": str(int(size)),
        }
        response = await self._request(
            "PUT",
            "/api/v1/objects",
            params={"bucket": bucket, "key": key},
            content=_FileStream(file),
            headers=headers,
        )
        return self._ensure_ok(response)

    async def create_upload(
        self,
        bucket: str,
        key: str,
        *,
        size: int,
        sha256: str | None = None,
        chunk_size: int | None = None,
    ) -> dict[str, Any]:
        """建分片会话；命中秒传时 hub 返回 ``instant: true``。

        不重试：失败即让调用方决定是否重新发起，避免孤儿会话。
        """

        body: dict[str, Any] = {"bucket": bucket, "key": key, "size": int(size)}
        if sha256:
            body["sha256"] = sha256
        if chunk_size:
            body["chunk_size"] = int(chunk_size)
        response = await self._request("POST", "/api/v1/uploads", json=body, retry=False)
        return self._ensure_ok(response)

    async def put_chunk(self, upload_id: str, index: int, data: bytes) -> dict[str, Any]:
        """上传单个分片：PUT /api/v1/uploads/{id}/chunks/{index}。"""

        path = f"/api/v1/uploads/{quote(str(upload_id), safe='')}/chunks/{int(index)}"
        response = await self._request(
            "PUT",
            path,
            content=data,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(data)),
            },
        )
        return self._ensure_ok(response)

    async def complete_upload(self, upload_id: str) -> dict[str, Any]:
        """收尾分片会话：POST /api/v1/uploads/{id}/complete（幂等，可重试）。"""

        path = f"/api/v1/uploads/{quote(str(upload_id), safe='')}/complete"
        response = await self._request("POST", path, json={})
        return self._ensure_ok(response)

    async def get_object(self, bucket: str, key: str) -> dict[str, Any] | None:
        """获取对象元数据；404 返回 None（未登记到该 key）。"""

        response = await self._request(
            "GET", "/api/v1/objects", params={"bucket": bucket, "key": key}
        )
        if response.status_code == 404:
            return None
        return self._ensure_ok(response)

    async def sign_object(self, object_id: str, expires_sec: int) -> dict[str, Any]:
        """签名直链：POST /api/v1/objects/{id}/sign，返回 ``{url, expires_at}``。"""

        path = f"/api/v1/objects/{quote(str(object_id), safe='')}/sign"
        response = await self._request("POST", path, json={"expires_sec": int(expires_sec)})
        return self._ensure_ok(response)

    async def delete_object(self, object_id: str) -> bool:
        """删除对象；本来就不存在时返回 False，不当作失败。"""

        path = f"/api/v1/objects/{quote(str(object_id), safe='')}"
        response = await self._request("DELETE", path)
        if response.status_code == 404:
            return False
        self._ensure_ok(response)
        return True

    # ------------------------------------------------------------------ 内部实现

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        content: Any = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = True,
    ) -> httpx.Response:
        attempts = self._max_retries + 1 if retry else 1
        request_headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if headers:
            request_headers.update({str(k): str(v) for k, v in headers.items()})

        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = await self._client.request(
                    method,
                    path,
                    params=params,
                    json=json,
                    content=content,
                    headers=request_headers,
                )
            except RETRYABLE_EXCEPTIONS as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise unavailable(
                        "无法连接 happy-hub 存储服务，请检查服务地址与网络",
                        detail=f"{type(exc).__name__}: {exc}",
                    ) from exc
                self._rewind(content)
                await self._sleep(self._backoff(attempt, None))
                continue

            if response.status_code in RETRYABLE_STATUS and attempt + 1 < attempts:
                await self._sleep(self._backoff(attempt, response))
                self._rewind(content)
                continue

            return response

        raise unavailable(  # pragma: no cover - 循环必然在上面返回或抛出
            "存储服务请求失败",
            detail=repr(last_error),
        )

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(float(retry_after), self._backoff_max)
                except ValueError:
                    pass
        delay = self._backoff_base * (2**attempt)
        delay = min(delay, self._backoff_max)
        jitter = 1.0 + random.random() * 0.25
        return delay * jitter

    @staticmethod
    def _rewind(content: Any) -> None:
        """重试前把文件指针拨回开头，避免第二次发送空 body。"""

        seek = getattr(content, "seek", None)
        if callable(seek):
            try:
                seek(0)
            except (OSError, ValueError):
                pass

    def _ensure_ok(self, response: httpx.Response) -> dict[str, Any]:
        payload = self._payload(response)
        if response.status_code >= 400:
            self._raise(response, payload)
        if response.status_code in (204, 205):
            return {"ok": True}
        if payload is None:
            raise bad_response(f"HTTP {response.status_code} 响应体不是 JSON")
        if payload.get("ok") is not True:
            raise error_from_hub(response.status_code, payload)
        return payload

    def _raise(
        self, response: httpx.Response, payload: Mapping[str, Any] | None = None
    ) -> None:
        if payload is None:
            payload = self._payload(response)
        raise error_from_hub(response.status_code, payload)

    @staticmethod
    def _payload(response: httpx.Response) -> dict[str, Any] | None:
        if not response.content:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None


__all__ = [
    "HubClient",
    "RETRYABLE_STATUS",
    "DEFAULT_MAX_RETRIES",
]
