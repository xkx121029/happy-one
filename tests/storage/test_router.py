"""对外 HTTP 接口：包络、状态码、302 跳转与中文错误。"""

from __future__ import annotations

import hashlib

import httpx
import pytest
from fastapi import FastAPI

from happyone.storage import repository
from happyone.storage import router as router_module

HUB = "http://hub.test"


def ok(**payload):
    return {"ok": True, **payload}


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture()
def app(service_factory, monkeypatch):
    application = FastAPI()
    application.include_router(router_module.router)
    service = service_factory()
    monkeypatch.setattr(router_module.deps, "build_service", lambda: service)
    return application


def asgi_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_info_lists_namespaces(app):
    async with asgi_client(app) as client:
        response = await client.get("/storage/info")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert {item["bucket"] for item in body["namespaces"]} == {
        "relay",
        "license-artifacts",
        "backup",
    }
    assert body["sign_ttl"] == {"min": 60, "max": 900}


@pytest.mark.asyncio
async def test_upload_endpoint_returns_mapping(respx_mock, app, conn):
    data = b"hello"
    digest = sha_of(data)
    respx_mock.head(f"{HUB}/api/v1/blobs/{digest}").mock(return_value=httpx.Response(404))
    respx_mock.put(f"{HUB}/api/v1/objects").mock(
        return_value=httpx.Response(200, json=ok(id="obj_1"))
    )
    respx_mock.get(f"{HUB}/api/v1/objects").mock(
        return_value=httpx.Response(200, json=ok(id="obj_1"))
    )

    async with asgi_client(app) as client:
        response = await client.post(
            "/storage/upload",
            files={"file": ("a.txt", data, "text/plain")},
            data={"namespace": "relay", "entity_type": "message", "entity_id": "ent_1"},
        )

    assert response.status_code == 201
    body = response.json()
    assert body["ok"] is True
    assert body["deduped"] is False
    assert body["object"]["key"] == "relay/message/ent_1/a.txt"
    assert body["object"]["object_id"] == "obj_1"
    assert len(repository.list_by_entity(conn, "message", "ent_1")) == 1


@pytest.mark.asyncio
async def test_link_endpoint_returns_plain_url(respx_mock, app, conn):
    record = repository.upsert_object(
        conn,
        namespace="relay",
        bucket="relay",
        key="relay/message/ent_1/a.txt",
        object_id="obj_1",
        sha256="f" * 64,
        size_bytes=5,
        content_type="text/plain",
        entity_type="message",
        entity_id="ent_1",
        filename="a.txt",
    )
    respx_mock.post(f"{HUB}/api/v1/objects/obj_1/sign").mock(
        return_value=httpx.Response(200, json=ok(url="https://cdn.test/a", expires_at=123))
    )

    async with asgi_client(app) as client:
        response = await client.get(f"/storage/objects/{record.id}/link?expires_sec=120")

    assert response.status_code == 200
    body = response.json()
    assert body["url"] == "https://cdn.test/a"
    assert body["expires_sec"] == 120
    assert "expires_at" in body


@pytest.mark.asyncio
async def test_redirect_endpoint_is_302(respx_mock, app, conn):
    record = repository.upsert_object(
        conn,
        namespace="relay",
        bucket="relay",
        key="relay/message/ent_1/b.txt",
        object_id="obj_2",
        sha256="a" * 64,
        size_bytes=5,
        content_type=None,
        entity_type="message",
        entity_id="ent_1",
        filename="b.txt",
    )
    respx_mock.post(f"{HUB}/api/v1/objects/obj_2/sign").mock(
        return_value=httpx.Response(200, json=ok(url="https://cdn.test/b"))
    )

    async with asgi_client(app) as client:
        response = await client.get(f"/storage/objects/{record.id}/redirect")

    assert response.status_code == 302
    assert response.headers["location"] == "https://cdn.test/b"


@pytest.mark.asyncio
async def test_delete_endpoint_removes_record(respx_mock, app, conn):
    record = repository.upsert_object(
        conn,
        namespace="relay",
        bucket="relay",
        key="relay/message/ent_1/c.txt",
        object_id="obj_3",
        sha256="b" * 64,
        size_bytes=5,
        content_type=None,
        entity_type="message",
        entity_id="ent_1",
        filename="c.txt",
    )
    respx_mock.delete(f"{HUB}/api/v1/objects/obj_3").mock(
        return_value=httpx.Response(200, json=ok())
    )

    async with asgi_client(app) as client:
        response = await client.delete(f"/storage/objects/{record.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["hub_deleted"] is True
    assert repository.get_by_id(conn, record.id) is None


@pytest.mark.asyncio
async def test_entity_list_endpoint(app, conn):
    for index, name in enumerate(("a.txt", "b.txt"), start=1):
        repository.upsert_object(
            conn,
            namespace="relay",
            bucket="relay",
            key=f"relay/message/ent_1/{name}",
            object_id=f"obj_{index}",
            sha256="c" * 64,
            size_bytes=5,
            content_type=None,
            entity_type="message",
            entity_id="ent_1",
            filename=name,
        )

    async with asgi_client(app) as client:
        response = await client.get("/storage/entities/message/ent_1")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["total"] == 2


@pytest.mark.asyncio
async def test_missing_record_returns_chinese_404(app):
    async with asgi_client(app) as client:
        response = await client.get("/storage/objects/999/link")

    assert response.status_code == 404
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == "ST-NOT-FOUND"
    assert "不存在" in body["error"]


@pytest.mark.asyncio
async def test_unknown_namespace_returns_400(app):
    async with asgi_client(app) as client:
        response = await client.post(
            "/storage/upload",
            files={"file": ("a.txt", b"x", "text/plain")},
            data={"namespace": "public", "entity_type": "message", "entity_id": "ent_1"},
        )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "ST-INVALID"
    assert "命名空间" in body["error"]


@pytest.mark.asyncio
async def test_missing_namespace_is_rejected_in_chinese(app):
    async with asgi_client(app) as client:
        response = await client.post(
            "/storage/upload",
            files={"file": ("a.txt", b"x", "text/plain")},
            data={"entity_type": "message", "entity_id": "ent_1"},
        )

    assert response.status_code == 400
    assert response.json()["code"] == "ST-INVALID"
    assert "命名空间" in response.json()["error"]
