"""存储业务逻辑（纯 Python，不 import fastapi）。

职责：
- 计算 sha256、打秒传探针，命中则不传字节；
- 小文件直传、大文件分片三段式上传；
- 用 happy-one 自己的库记录已传分片，实现断点续传（不依赖 hub 的内存会话）；
- 签名短链、删除、按业务实体查询；
- 把 hub 元数据落到映射表（只记录元数据，不复制内容）。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

from . import keys, repository
from .constants import (
    CHUNK_SIZE,
    MULTIPART_THRESHOLD,
    READ_BLOCK_SIZE,
    SIGN_TTL_DEFAULT,
    SIGN_TTL_MAX,
    SIGN_TTL_MIN,
)
from .errors import (
    CODE_HUB,
    StorageError,
    bad_response,
    invalid_argument,
    not_found,
)
from .models import (
    DeleteOutcome,
    RedirectSpec,
    SignedLink,
    StoredObject,
    UploadOutcome,
)

UploadSource = bytes | bytearray | memoryview | str | os.PathLike[str] | BinaryIO


@dataclass(frozen=True)
class _Staged:
    path: Path
    sha256: str
    size: int
    cleanup: bool


class StorageService:
    """上层唯一需要接触的门面；client 与数据库连接均由外部注入。"""

    def __init__(
        self,
        client: Any,
        conn_provider: Callable[[], Any],
        *,
        chunk_size: int = CHUNK_SIZE,
        multipart_threshold: int = MULTIPART_THRESHOLD,
        sign_ttl_default: int = SIGN_TTL_DEFAULT,
        resume_enabled: bool = True,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须大于 0")
        self._client = client
        self._conn_provider = conn_provider
        self._chunk_size = int(chunk_size)
        self._multipart_threshold = max(0, int(multipart_threshold))
        self._sign_ttl_default = _clamp_ttl(sign_ttl_default)
        self._resume_enabled = bool(resume_enabled)

    # ------------------------------------------------------------------ 上传

    async def upload(
        self,
        *,
        namespace: str,
        entity_type: str,
        entity_id: str,
        filename: str | None = None,
        source: UploadSource,
        content_type: str | None = None,
        force_multipart: bool = False,
    ) -> UploadOutcome:
        """上传一个文件并登记到业务实体。

        ``filename`` 为空时使用内容寻址 key（``<namespace>/<sha256>``），
        有文件名时使用 ``<namespace>/<entity_type>/<entity_id>/<filename>``。
        """

        ns = _namespace(namespace)
        type_value = _required(entity_type, "entity_type")
        id_value = _required(entity_id, "entity_id")
        bucket = keys.bucket_for(ns)

        with _staged_upload(source) as staged:
            if filename:
                key = _key(lambda: keys.entity_key(ns, type_value, id_value, filename))
            else:
                key = keys.content_addressed_key(ns, staged.sha256)

            existing = await self._try_dedupe(bucket, key, staged.sha256)
            if existing is not None:
                record = self._save_mapping(
                    namespace=ns,
                    bucket=bucket,
                    key=key,
                    staged=staged,
                    content_type=content_type,
                    entity_type=type_value,
                    entity_id=id_value,
                    filename=filename,
                    payload=existing,
                )
                return UploadOutcome(record=record, deduped=True)

            if staged.size > 0 and (
                force_multipart or staged.size > self._multipart_threshold
            ):
                payload, resumed, uploaded_chunks, deduped = await self._upload_multipart(
                    bucket=bucket,
                    key=key,
                    staged=staged,
                )
                record = self._save_mapping(
                    namespace=ns,
                    bucket=bucket,
                    key=key,
                    staged=staged,
                    content_type=content_type,
                    entity_type=type_value,
                    entity_id=id_value,
                    filename=filename,
                    payload=payload,
                )
                return UploadOutcome(
                    record=record,
                    deduped=deduped,
                    resumed=resumed,
                    uploaded_chunks=uploaded_chunks,
                )

            payload = await self._put_small(bucket, key, staged, content_type)
            meta = await self._client.get_object(bucket, key) or payload
            record = self._save_mapping(
                namespace=ns,
                bucket=bucket,
                key=key,
                staged=staged,
                content_type=content_type,
                entity_type=type_value,
                entity_id=id_value,
                filename=filename,
                payload=meta,
            )
            return UploadOutcome(record=record, uploaded_chunks=1)

    async def _try_dedupe(self, bucket: str, key: str, sha256: str) -> dict[str, Any] | None:
        """探针命中且该 key 已登记时才走秒传；否则照常上传。"""

        if not await self._client.probe_blob(sha256):
            return None
        return await self._client.get_object(bucket, key)

    async def _put_small(
        self, bucket: str, key: str, staged: _Staged, content_type: str | None
    ) -> dict[str, Any]:
        with staged.path.open("rb") as handle:
            return await self._client.put_object(
                bucket,
                key,
                file=handle,
                size=staged.size,
                content_type=content_type,
            )

    async def _upload_multipart(
        self,
        *,
        bucket: str,
        key: str,
        staged: _Staged,
        allow_restart: bool = True,
        force_new: bool = False,
    ) -> tuple[dict[str, Any], bool, int, bool]:
        """分片上传，返回 (末次响应, 是否续传, 本次上传分片数, 是否秒传)。"""

        session = None
        if self._resume_enabled and not force_new:
            session = repository.find_upload_session(
                self._conn(),
                bucket=bucket,
                key=key,
                sha256=staged.sha256,
                size_bytes=staged.size,
            )

        chunk_size = session.chunk_size if session else self._chunk_size
        chunk_size = max(1, int(chunk_size))
        total_chunks = max(1, (staged.size + chunk_size - 1) // chunk_size)
        resumed = session is not None
        uploaded_chunks = 0

        if session is not None:
            upload_id = session.upload_id
            done = set(session.uploaded_chunks)
        else:
            payload = await self._client.create_upload(
                bucket,
                key,
                size=staged.size,
                sha256=staged.sha256,
                chunk_size=chunk_size,
            )
            if _instant_hit(payload):
                meta = await self._client.get_object(bucket, key)
                if meta is None:
                    raise StorageError(
                        CODE_HUB,
                        "存储服务返回秒传命中，但未能取到对象元数据，请稍后重试",
                        status=502,
                        detail=repr(payload),
                    )
                return meta, False, 0, True
            upload_id = str(payload.get("upload_id") or "").strip()
            if not upload_id:
                raise bad_response("建分片会话响应缺少 upload_id")
            repository.upsert_upload_session(
                self._conn(),
                upload_id=upload_id,
                bucket=bucket,
                key=key,
                sha256=staged.sha256,
                size_bytes=staged.size,
                chunk_size=chunk_size,
            )
            done = set()

        try:
            with staged.path.open("rb") as handle:
                for index in range(total_chunks):
                    if index in done:
                        continue
                    handle.seek(index * chunk_size)
                    data = handle.read(chunk_size)
                    if not data:
                        raise StorageError(
                            CODE_HUB,
                            "本地文件在分片上传过程中变短，请重新上传",
                            status=500,
                        )
                    await self._client.put_chunk(upload_id, index, data)
                    repository.mark_chunk_uploaded(self._conn(), upload_id, index)
                    uploaded_chunks += 1
            payload = await self._client.complete_upload(upload_id)
        except StorageError as exc:
            # hub 会话只在内存里，重启后 upload_id 失效：本机记录整段作废，
            # 重新建会话并完整重传一次，不做无限重试。
            if resumed and allow_restart and exc.status == 404:
                repository.delete_upload_session(self._conn(), upload_id)
                return await self._upload_multipart(
                    bucket=bucket,
                    key=key,
                    staged=staged,
                    allow_restart=False,
                    force_new=True,
                )
            raise

        repository.delete_upload_session(self._conn(), upload_id)
        meta = await self._client.get_object(bucket, key)
        return (meta or payload), resumed, uploaded_chunks, False

    def _save_mapping(
        self,
        *,
        namespace: str,
        bucket: str,
        key: str,
        staged: _Staged,
        content_type: str | None,
        entity_type: str,
        entity_id: str,
        filename: str | None,
        payload: dict[str, Any],
    ) -> StoredObject:
        object_id = _object_id(payload)
        if not object_id:
            raise bad_response("存储服务未返回对象标识，无法登记文件元数据")
        meta_type = payload.get("content_type") or payload.get("mime")
        return repository.upsert_object(
            self._conn(),
            namespace=namespace,
            bucket=bucket,
            key=key,
            object_id=object_id,
            sha256=staged.sha256,
            size_bytes=staged.size,
            content_type=str(content_type or meta_type or "application/octet-stream"),
            entity_type=entity_type,
            entity_id=entity_id,
            filename=filename,
        )

    # ------------------------------------------------------------------ 签名 / 跳转

    async def sign_download(
        self, record_id: int, expires_sec: int | str | None = None
    ) -> SignedLink:
        record = self._require_record(record_id)
        if not record.object_id:
            raise StorageError(
                CODE_HUB,
                "该文件缺少存储对象标识，无法生成下载短链，请重新上传",
                status=409,
            )
        ttl = _clamp_ttl(self._sign_ttl_default if expires_sec is None else expires_sec)
        payload = await self._client.sign_object(record.object_id, ttl)
        url = str(payload.get("url") or "").strip()
        if not url:
            raise bad_response("签名接口未返回下载地址")
        return SignedLink(url=url, expires_sec=ttl, expires_at=payload.get("expires_at"))

    async def sign_redirect(
        self, record_id: int, expires_sec: int | str | None = None
    ) -> RedirectSpec:
        """302 跳转辅助：service 只给出状态码与目标地址，由 router 构造响应。"""

        link = await self.sign_download(record_id, expires_sec)
        return RedirectSpec(status_code=302, location=link.url)

    # ------------------------------------------------------------------ 查询 / 删除

    def get(self, record_id: int) -> StoredObject:
        return self._require_record(record_id)

    def list_files(
        self, entity_type: str, entity_id: str, *, namespace: str | None = None
    ) -> list[StoredObject]:
        type_value = _required(entity_type, "entity_type")
        id_value = _required(entity_id, "entity_id")
        ns = _namespace(namespace) if namespace else None
        return repository.list_by_entity(
            self._conn(),
            type_value,
            id_value,
            namespace=ns,
        )

    async def delete(self, record_id: int) -> DeleteOutcome:
        """删除文件；多个实体共用同一个 hub 对象时只删映射，不动本体。"""

        record = self._require_record(record_id)
        conn = self._conn()
        shared = (
            repository.count_shares(conn, record.object_id, exclude_record_id=record.id)
            if record.object_id
            else 0
        )
        hub_deleted = False
        if shared == 0 and record.object_id:
            hub_deleted = await self._client.delete_object(record.object_id)
        record_deleted = repository.delete_record(conn, record.id)
        return DeleteOutcome(
            record_id=record.id,
            object_id=record.object_id,
            hub_deleted=hub_deleted,
            record_deleted=record_deleted,
            shared_records=shared,
        )

    # ------------------------------------------------------------------ 内部工具

    def _conn(self) -> Any:
        return self._conn_provider()

    def _require_record(self, record_id: int) -> StoredObject:
        try:
            numeric = int(record_id)
        except (TypeError, ValueError):
            raise invalid_argument("文件记录编号必须是整数") from None
        record = repository.get_by_id(self._conn(), numeric)
        if record is None:
            raise not_found(f"文件记录不存在：{record_id}")
        return record


def _namespace(value: str) -> str:
    try:
        return keys.normalize_namespace(value)
    except ValueError as exc:
        raise invalid_argument(str(exc)) from None


def _required(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise invalid_argument(f"{field} 不能为空")
    return text


def _key(factory: Callable[[], str]) -> str:
    try:
        return factory()
    except ValueError as exc:
        raise invalid_argument(str(exc)) from None


def _clamp_ttl(value: int | None) -> int:
    try:
        number = int(value if value is not None else SIGN_TTL_DEFAULT)
    except (TypeError, ValueError):
        raise invalid_argument("expires_sec 必须是整数秒") from None
    return max(SIGN_TTL_MIN, min(SIGN_TTL_MAX, number))


def _object_id(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    for field in ("id", "object_id", "blob_id"):
        value = payload.get(field)
        if value not in (None, ""):
            return str(value)
    nested = payload.get("object")
    if isinstance(nested, dict):
        return _object_id(nested)
    return None


def _instant_hit(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    return bool(payload.get("instant") or payload.get("deduped"))


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(READ_BLOCK_SIZE)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


@contextmanager
def _staged_upload(source: UploadSource) -> Iterator[_Staged]:
    """把上传源统一成磁盘文件，边读边算 sha256；只清理自己创建的临时文件。"""

    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
        path = _temp_file()
        try:
            path.write_bytes(data)
            yield _Staged(path=path, sha256=hashlib.sha256(data).hexdigest(), size=len(data), cleanup=True)
        finally:
            path.unlink(missing_ok=True)
        return

    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
        if not path.is_file():
            raise invalid_argument(f"上传源文件不存在：{path}")
        sha256, size = _hash_file(path)
        yield _Staged(path=path, sha256=sha256, size=size, cleanup=False)
        return

    path = _temp_file()
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("wb") as target:
            while True:
                block = source.read(READ_BLOCK_SIZE)
                if not block:
                    break
                if isinstance(block, str):
                    raise invalid_argument("上传源必须是二进制流")
                digest.update(block)
                size += len(block)
                target.write(block)
        yield _Staged(path=path, sha256=digest.hexdigest(), size=size, cleanup=True)
    finally:
        path.unlink(missing_ok=True)


def _temp_file() -> Path:
    handle = tempfile.NamedTemporaryFile(prefix="happyone-upload-", delete=False)
    handle.close()
    return Path(handle.name)


__all__ = ["StorageService", "UploadSource"]
