"""hub 客户端：认证头、包络解析、错误翻译、有限重试。"""

from __future__ import annotations

import io

import httpx
import pytest

from happyone.storage import errors
from happyone.storage.errors import StorageError

SHA = "c" * 64


@pytest.mark.asyncio
async def test_probe_sends_bearer_token(respx_mock, client_factory):
    route = respx_mock.head(f"http://hub.test/api/v1/blobs/{SHA}").mock(
        return_value=httpx.Response(200)
    )
    client = client_factory()

    assert await client.probe_blob(SHA) is True

    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer hh_test_token"
    assert "hh_test_token" not in str(request.url)
    await client.aclose()


@pytest.mark.asyncio
async def test_probe_missing_returns_false(respx_mock, client_factory):
    respx_mock.head(f"http://hub.test/api/v1/blobs/{SHA}").mock(
        return_value=httpx.Response(404)
    )
    client = client_factory()
    assert await client.probe_blob(SHA) is False
    await client.aclose()


@pytest.mark.asyncio
async def test_put_object_sends_raw_bytes_with_length(respx_mock, client_factory):
    route = respx_mock.put(
        "http://hub.test/api/v1/objects",
        params={"bucket": "relay", "key": "relay/a.txt"},
    ).mock(return_value=httpx.Response(200, json={"ok": True, "id": "obj_1"}))

    client = client_factory()
    payload = await client.put_object(
        "relay",
        "relay/a.txt",
        file=io.BytesIO(b"hello"),
        size=5,
        content_type="text/plain",
    )

    assert payload["id"] == "obj_1"
    request = route.calls[0].request
    assert request.headers["content-length"] == "5"
    assert request.headers["content-type"] == "text/plain"
    assert request.content == b"hello"
    await client.aclose()


@pytest.mark.asyncio
async def test_retryable_status_retries_then_succeeds(respx_mock, client_factory):
    route = respx_mock.head(f"http://hub.test/api/v1/blobs/{SHA}").mock(
        side_effect=[httpx.Response(503), httpx.Response(200)]
    )
    client = client_factory(max_retries=2)

    assert await client.probe_blob(SHA) is True
    assert route.call_count == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_retry_is_bounded(respx_mock, client_factory):
    route = respx_mock.head(f"http://hub.test/api/v1/blobs/{SHA}").mock(
        return_value=httpx.Response(503, json={"ok": False, "code": "E5001", "error": "后端不可用"})
    )
    client = client_factory(max_retries=2)

    with pytest.raises(StorageError) as info:
        await client.probe_blob(SHA)

    assert info.value.code == errors.CODE_UNAVAILABLE
    assert info.value.status == 503
    assert "存储服务" in info.value.message
    assert route.call_count == 3  # 1 次请求 + 2 次重试，不再继续
    await client.aclose()


@pytest.mark.asyncio
async def test_create_upload_is_not_retried(respx_mock, client_factory):
    route = respx_mock.post("http://hub.test/api/v1/uploads").mock(
        return_value=httpx.Response(503, json={"ok": False, "code": "E5002", "error": "会话创建失败"})
    )
    client = client_factory(max_retries=2)

    with pytest.raises(StorageError):
        await client.create_upload("relay", "relay/a.txt", size=10)

    assert route.call_count == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_auth_error_message_is_actionable(respx_mock, client_factory):
    respx_mock.get("http://hub.test/api/v1/objects").mock(
        return_value=httpx.Response(
            401, json={"ok": False, "code": "E4010", "error": "invalid token"}
        )
    )
    client = client_factory()

    with pytest.raises(StorageError) as info:
        await client.get_object("relay", "relay/a.txt")

    assert info.value.code == errors.CODE_AUTH
    assert info.value.status == 401
    assert "令牌" in info.value.message
    assert info.value.hub_code == "E4010"
    await client.aclose()


@pytest.mark.asyncio
async def test_unknown_hub_code_can_be_registered(respx_mock, client_factory):
    respx_mock.get("http://hub.test/api/v1/objects").mock(
        return_value=httpx.Response(
            409, json={"ok": False, "code": "E9001", "error": "版本冲突"}
        )
    )
    client = client_factory()

    with pytest.raises(StorageError) as info:
        await client.get_object("relay", "relay/a.txt")
    assert info.value.code == errors.CODE_CONFLICT
    assert "版本冲突" in info.value.message
    assert info.value.hub_code == "E9001"

    errors.register_hub_code_message("E9001", "对象正在被其他任务占用，请稍后再试")
    try:
        with pytest.raises(StorageError) as overridden:
            await client.get_object("relay", "relay/a.txt")
        assert overridden.value.message == "对象正在被其他任务占用，请稍后再试"
    finally:
        errors.HUB_CODE_MESSAGES.pop("E9001", None)
    await client.aclose()


@pytest.mark.asyncio
async def test_get_object_404_returns_none(respx_mock, client_factory):
    respx_mock.get("http://hub.test/api/v1/objects").mock(return_value=httpx.Response(404))
    client = client_factory()
    assert await client.get_object("relay", "relay/a.txt") is None
    await client.aclose()


@pytest.mark.asyncio
async def test_sign_object_payload_and_url(respx_mock, client_factory):
    route = respx_mock.post("http://hub.test/api/v1/objects/obj_1/sign").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "url": "https://cdn.test/a?sign=1", "expires_at": 123}
        )
    )
    client = client_factory()

    payload = await client.sign_object("obj_1", 120)

    assert payload["url"].startswith("https://cdn.test/")
    assert route.calls[0].request.content == b'{"expires_sec":120}'
    await client.aclose()


@pytest.mark.asyncio
async def test_delete_missing_object_returns_false(respx_mock, client_factory):
    respx_mock.delete("http://hub.test/api/v1/objects/obj_missing").mock(
        return_value=httpx.Response(404)
    )
    client = client_factory()
    assert await client.delete_object("obj_missing") is False
    await client.aclose()


@pytest.mark.asyncio
async def test_delete_204_is_success(respx_mock, client_factory):
    respx_mock.delete("http://hub.test/api/v1/objects/obj_1").mock(
        return_value=httpx.Response(204)
    )
    client = client_factory()
    assert await client.delete_object("obj_1") is True
    await client.aclose()


@pytest.mark.asyncio
async def test_network_error_becomes_unavailable(respx_mock, client_factory):
    route = respx_mock.head(f"http://hub.test/api/v1/blobs/{SHA}").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    client = client_factory(max_retries=1)

    with pytest.raises(StorageError) as info:
        await client.probe_blob(SHA)

    assert info.value.code == errors.CODE_UNAVAILABLE
    assert "无法连接" in info.value.message
    assert route.call_count == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_non_json_success_is_reported(respx_mock, client_factory):
    respx_mock.get("http://hub.test/api/v1/objects").mock(
        return_value=httpx.Response(200, text="<html>gateway</html>")
    )
    client = client_factory()

    with pytest.raises(StorageError) as info:
        await client.get_object("relay", "relay/a.txt")

    assert info.value.code == errors.CODE_BAD_RESPONSE
    await client.aclose()
