"""上传、秒传、分片、续传、签名、删除的业务行为（HTTP 全部 mock）。"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from happyone.storage import errors, repository
from happyone.storage.errors import StorageError

HUB = "http://hub.test"


def ok(**payload):
    return {"ok": True, **payload}


def envelope_error(code="E0001", error="出错了", status=400):
    return httpx.Response(status, json={"ok": False, "code": code, "error": error})


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def seed_record(conn, *, entity_id="ent_1", object_id="obj_1", key=None, sha=None, size=5):
    return repository.upsert_object(
        conn,
        namespace="relay",
        bucket="relay",
        key=key or "relay/message/ent_1/a.txt",
        object_id=object_id,
        sha256=sha or "e" * 64,
        size_bytes=size,
        content_type="text/plain",
        entity_type="message",
        entity_id=entity_id,
        filename="a.txt",
    )


@pytest.mark.asyncio
async def test_small_upload_streams_and_maps(respx_mock, service_factory, conn):
    data = b"hello"
    digest = sha_of(data)
    respx_mock.head(f"{HUB}/api/v1/blobs/{digest}").mock(return_value=httpx.Response(404))
    put_route = respx_mock.put(f"{HUB}/api/v1/objects").mock(
        return_value=httpx.Response(200, json=ok(id="obj_1"))
    )
    respx_mock.get(f"{HUB}/api/v1/objects").mock(
        return_value=httpx.Response(200, json=ok(id="obj_1", content_type="text/plain"))
    )
    service = service_factory(threshold=8)

    outcome = await service.upload(
        namespace="relay",
        entity_type="message",
        entity_id="ent_1",
        filename="a.txt",
        source=data,
    )

    assert outcome.record.key == "relay/message/ent_1/a.txt"
    assert outcome.record.sha256 == digest
    assert outcome.record.object_id == "obj_1"
    assert outcome.record.size_bytes == 5
    assert outcome.deduped is False
    assert put_route.call_count == 1
    assert put_route.calls[0].request.content == data
    assert repository.get_by_id(conn, outcome.record.id) is not None


@pytest.mark.asyncio
async def test_dedupe_probe_hit_skips_bytes(respx_mock, service_factory, conn):
    data = b"hello"
    digest = sha_of(data)
    respx_mock.head(f"{HUB}/api/v1/blobs/{digest}").mock(return_value=httpx.Response(200))
    respx_mock.get(f"{HUB}/api/v1/objects").mock(
        return_value=httpx.Response(200, json=ok(id="obj_cached"))
    )
    put_route = respx_mock.put(f"{HUB}/api/v1/objects").mock(
        return_value=httpx.Response(200, json=ok(id="obj_cached"))
    )
    service = service_factory(threshold=8)

    outcome = await service.upload(
        namespace="relay",
        entity_type="message",
        entity_id="ent_1",
        filename="a.txt",
        source=data,
    )

    assert outcome.deduped is True
    assert put_route.call_count == 0
    assert outcome.record.object_id == "obj_cached"
    assert len(repository.list_by_entity(conn, "message", "ent_1")) == 1


@pytest.mark.asyncio
async def test_probe_hit_but_key_missing_falls_back_to_upload(respx_mock, service_factory):
    data = b"hello"
    digest = sha_of(data)
    respx_mock.head(f"{HUB}/api/v1/blobs/{digest}").mock(return_value=httpx.Response(200))
    respx_mock.get(f"{HUB}/api/v1/objects").mock(
        side_effect=[
            httpx.Response(404),
            httpx.Response(200, json=ok(id="obj_new")),
        ]
    )
    put_route = respx_mock.put(f"{HUB}/api/v1/objects").mock(
        return_value=httpx.Response(200, json=ok(id="obj_new"))
    )
    service = service_factory(threshold=8)

    outcome = await service.upload(
        namespace="relay",
        entity_type="message",
        entity_id="ent_1",
        filename="a.txt",
        source=data,
    )

    assert outcome.deduped is False
    assert put_route.call_count == 1
    assert outcome.record.object_id == "obj_new"


@pytest.mark.asyncio
async def test_multipart_upload_sends_every_chunk(respx_mock, service_factory, conn):
    data = b"0123456789"
    digest = sha_of(data)
    respx_mock.head(f"{HUB}/api/v1/blobs/{digest}").mock(return_value=httpx.Response(404))
    create_route = respx_mock.post(f"{HUB}/api/v1/uploads").mock(
        return_value=httpx.Response(200, json=ok(upload_id="up_1"))
    )
    chunk_routes = {
        index: respx_mock.put(f"{HUB}/api/v1/uploads/up_1/chunks/{index}").mock(
            return_value=httpx.Response(200, json=ok())
        )
        for index in range(3)
    }
    respx_mock.post(f"{HUB}/api/v1/uploads/up_1/complete").mock(
        return_value=httpx.Response(200, json=ok(id="obj_9"))
    )
    respx_mock.get(f"{HUB}/api/v1/objects").mock(
        return_value=httpx.Response(200, json=ok(id="obj_9"))
    )
    service = service_factory(chunk_size=4, threshold=4)

    outcome = await service.upload(
        namespace="relay",
        entity_type="message",
        entity_id="ent_1",
        filename="a.txt",
        source=data,
    )

    assert outcome.deduped is False
    assert outcome.resumed is False
    assert outcome.uploaded_chunks == 3
    assert create_route.call_count == 1
    assert chunk_routes[0].calls[0].request.content == b"0123"
    assert chunk_routes[1].calls[0].request.content == b"4567"
    assert chunk_routes[2].calls[0].request.content == b"89"
    assert repository.get_upload_session(conn, "up_1") is None
    assert outcome.record.size_bytes == 10


@pytest.mark.asyncio
async def test_multipart_resumes_from_local_session(respx_mock, service_factory, conn):
    data = b"0123456789"
    digest = sha_of(data)
    key = "relay/message/ent_1/a.txt"
    repository.upsert_upload_session(
        conn,
        upload_id="up_resume",
        bucket="relay",
        key=key,
        sha256=digest,
        size_bytes=len(data),
        chunk_size=4,
        uploaded_chunks=(0, 1),
    )
    respx_mock.head(f"{HUB}/api/v1/blobs/{digest}").mock(return_value=httpx.Response(404))
    create_route = respx_mock.post(f"{HUB}/api/v1/uploads").mock(
        return_value=httpx.Response(200, json=ok(upload_id="should_not_use"))
    )
    skipped = {
        index: respx_mock.put(f"{HUB}/api/v1/uploads/up_resume/chunks/{index}").mock(
            return_value=httpx.Response(200, json=ok())
        )
        for index in (0, 1)
    }
    last_chunk = respx_mock.put(f"{HUB}/api/v1/uploads/up_resume/chunks/2").mock(
        return_value=httpx.Response(200, json=ok())
    )
    respx_mock.post(f"{HUB}/api/v1/uploads/up_resume/complete").mock(
        return_value=httpx.Response(200, json=ok(id="obj_9"))
    )
    respx_mock.get(f"{HUB}/api/v1/objects").mock(
        return_value=httpx.Response(200, json=ok(id="obj_9"))
    )
    service = service_factory(chunk_size=4, threshold=4)

    outcome = await service.upload(
        namespace="relay",
        entity_type="message",
        entity_id="ent_1",
        filename="a.txt",
        source=data,
    )

    assert outcome.resumed is True
    assert outcome.uploaded_chunks == 1
    assert create_route.call_count == 0
    assert skipped[0].call_count == 0
    assert skipped[1].call_count == 0
    assert last_chunk.calls[0].request.content == b"89"


@pytest.mark.asyncio
async def test_lost_hub_session_restarts_once(respx_mock, service_factory, conn):
    data = b"0123456789"
    digest = sha_of(data)
    key = "relay/message/ent_1/a.txt"
    repository.upsert_upload_session(
        conn,
        upload_id="up_lost",
        bucket="relay",
        key=key,
        sha256=digest,
        size_bytes=len(data),
        chunk_size=4,
        uploaded_chunks=(0,),
    )
    respx_mock.head(f"{HUB}/api/v1/blobs/{digest}").mock(return_value=httpx.Response(404))
    respx_mock.put(f"{HUB}/api/v1/uploads/up_lost/chunks/1").mock(
        return_value=httpx.Response(404, json={"ok": False, "code": "E4040", "error": "会话不存在"})
    )
    create_route = respx_mock.post(f"{HUB}/api/v1/uploads").mock(
        return_value=httpx.Response(200, json=ok(upload_id="up_new"))
    )
    fresh_chunks = {
        index: respx_mock.put(f"{HUB}/api/v1/uploads/up_new/chunks/{index}").mock(
            return_value=httpx.Response(200, json=ok())
        )
        for index in range(3)
    }
    respx_mock.post(f"{HUB}/api/v1/uploads/up_new/complete").mock(
        return_value=httpx.Response(200, json=ok(id="obj_9"))
    )
    respx_mock.get(f"{HUB}/api/v1/objects").mock(
        return_value=httpx.Response(200, json=ok(id="obj_9"))
    )
    service = service_factory(chunk_size=4, threshold=4)

    outcome = await service.upload(
        namespace="relay",
        entity_type="message",
        entity_id="ent_1",
        filename="a.txt",
        source=data,
    )

    assert create_route.call_count == 1
    assert outcome.resumed is False
    assert outcome.uploaded_chunks == 3
    assert all(route.call_count == 1 for route in fresh_chunks.values())
    assert repository.get_upload_session(conn, "up_lost") is None


@pytest.mark.asyncio
async def test_create_upload_instant_is_treated_as_dedupe(respx_mock, service_factory):
    data = b"0123456789"
    digest = sha_of(data)
    respx_mock.head(f"{HUB}/api/v1/blobs/{digest}").mock(return_value=httpx.Response(404))
    respx_mock.post(f"{HUB}/api/v1/uploads").mock(
        return_value=httpx.Response(200, json=ok(instant=True, id="obj_instant"))
    )
    respx_mock.get(f"{HUB}/api/v1/objects").mock(
        return_value=httpx.Response(200, json=ok(id="obj_instant"))
    )
    chunk_route = respx_mock.put(url__regex=rf"{HUB}/api/v1/uploads/.*/chunks/.*").mock(
        return_value=httpx.Response(200, json=ok())
    )
    service = service_factory(chunk_size=4, threshold=4)

    outcome = await service.upload(
        namespace="relay",
        entity_type="message",
        entity_id="ent_1",
        filename="a.txt",
        source=data,
    )

    assert outcome.deduped is True
    assert outcome.uploaded_chunks == 0
    assert chunk_route.call_count == 0
    assert outcome.record.object_id == "obj_instant"


@pytest.mark.asyncio
async def test_signed_link_ttl_is_clamped(respx_mock, service_factory, conn):
    record = seed_record(conn)
    route = respx_mock.post(f"{HUB}/api/v1/objects/obj_1/sign").mock(
        return_value=httpx.Response(200, json=ok(url="https://cdn.test/x", expires_at=999))
    )
    service = service_factory()

    low = await service.sign_download(record.id, 30)
    high = await service.sign_download(record.id, 5000)
    default = await service.sign_download(record.id)

    assert low.expires_sec == 60
    assert high.expires_sec == 900
    assert default.expires_sec == 300
    assert low.url == "https://cdn.test/x"
    assert b'"expires_sec":60' in route.calls[0].request.content
    assert b'"expires_sec":900' in route.calls[1].request.content
    assert b'"expires_sec":300' in route.calls[2].request.content

    redirect = await service.sign_redirect(record.id, 120)
    assert redirect.status_code == 302
    assert redirect.location == "https://cdn.test/x"


@pytest.mark.asyncio
async def test_sign_missing_record_raises_not_found(service_factory):
    service = service_factory()
    with pytest.raises(StorageError) as info:
        await service.sign_download(999)
    assert info.value.code == errors.CODE_NOT_FOUND
    assert "不存在" in info.value.message


@pytest.mark.asyncio
async def test_delete_keeps_shared_object_until_last_mapping(respx_mock, service_factory, conn):
    first = seed_record(conn, entity_id="ent_1", object_id="obj_shared")
    second = seed_record(conn, entity_id="ent_2", object_id="obj_shared")
    delete_route = respx_mock.delete(f"{HUB}/api/v1/objects/obj_shared").mock(
        return_value=httpx.Response(200, json=ok())
    )
    service = service_factory()

    outcome = await service.delete(first.id)

    assert outcome.shared_records == 1
    assert outcome.hub_deleted is False
    assert delete_route.call_count == 0
    assert repository.get_by_id(conn, first.id) is None
    assert repository.get_by_id(conn, second.id) is not None

    final = await service.delete(second.id)

    assert final.hub_deleted is True
    assert delete_route.call_count == 1
    assert repository.get_by_id(conn, second.id) is None


@pytest.mark.asyncio
async def test_delete_missing_record_raises(service_factory):
    with pytest.raises(StorageError) as info:
        await service_factory().delete(404)
    assert info.value.code == errors.CODE_NOT_FOUND


@pytest.mark.asyncio
async def test_list_files_by_entity(service_factory, conn):
    seed_record(conn, entity_id="ent_1")
    seed_record(conn, entity_id="ent_2", object_id="obj_2")
    service = service_factory()

    records = service.list_files("message", "ent_1")
    assert len(records) == 1
    assert records[0].entity_id == "ent_1"
    assert service.list_files("message", "ent_1", namespace="license") == []


@pytest.mark.asyncio
async def test_invalid_namespace_fails_before_http(service_factory):
    with pytest.raises(StorageError) as info:
        await service_factory().upload(
            namespace="public",
            entity_type="message",
            entity_id="ent_1",
            filename="a.txt",
            source=b"x",
        )
    assert info.value.code == errors.CODE_INVALID
    assert "命名空间" in info.value.message


@pytest.mark.asyncio
async def test_content_addressed_key_when_no_filename(respx_mock, service_factory):
    data = b"hello"
    digest = sha_of(data)
    respx_mock.head(f"{HUB}/api/v1/blobs/{digest}").mock(return_value=httpx.Response(404))
    respx_mock.put(f"{HUB}/api/v1/objects").mock(
        return_value=httpx.Response(200, json=ok(id="obj_1"))
    )
    respx_mock.get(f"{HUB}/api/v1/objects").mock(
        return_value=httpx.Response(200, json=ok(id="obj_1"))
    )
    service = service_factory(threshold=8)

    outcome = await service.upload(
        namespace="relay",
        entity_type="message",
        entity_id="ent_1",
        source=data,
    )

    assert outcome.record.key == f"relay/{digest}"
    assert outcome.record.filename is None


@pytest.mark.asyncio
async def test_hub_rejection_is_translated_and_not_mapped(respx_mock, service_factory, conn):
    data = b"hello"
    digest = sha_of(data)
    respx_mock.head(f"{HUB}/api/v1/blobs/{digest}").mock(return_value=httpx.Response(404))
    respx_mock.put(f"{HUB}/api/v1/objects").mock(
        return_value=httpx.Response(
            413, json={"ok": False, "code": "E4130", "error": "对象过大"}
        )
    )
    service = service_factory(threshold=8)

    with pytest.raises(StorageError) as info:
        await service.upload(
            namespace="relay",
            entity_type="message",
            entity_id="ent_1",
            filename="a.txt",
            source=data,
        )

    assert info.value.code == errors.CODE_TOO_LARGE
    assert info.value.status == 413
    assert "对象过大" in info.value.message
    assert repository.list_by_entity(conn, "message", "ent_1") == []


@pytest.mark.asyncio
async def test_upload_from_local_path(respx_mock, service_factory, tmp_path):
    data = b"path-source"
    digest = sha_of(data)
    source = tmp_path / "b.txt"
    source.write_bytes(data)
    respx_mock.head(f"{HUB}/api/v1/blobs/{digest}").mock(return_value=httpx.Response(404))
    respx_mock.put(f"{HUB}/api/v1/objects").mock(
        return_value=httpx.Response(200, json=ok(id="obj_1"))
    )
    respx_mock.get(f"{HUB}/api/v1/objects").mock(
        return_value=httpx.Response(200, json=ok(id="obj_1"))
    )
    service = service_factory(threshold=64)

    outcome = await service.upload(
        namespace="relay",
        entity_type="message",
        entity_id="ent_1",
        filename="b.txt",
        source=str(source),
    )

    assert outcome.record.size_bytes == len(data)
    assert source.exists()  # 外部路径不会被清理
