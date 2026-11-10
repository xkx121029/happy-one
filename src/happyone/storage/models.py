"""存储模块的数据模型（纯 dataclass，不依赖 fastapi / pydantic）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StoredObject:
    """映射表 ``storage_objects`` 的一行，只描述元数据，不承载文件内容。"""

    id: int
    namespace: str
    bucket: str
    key: str
    object_id: str | None
    sha256: str
    size_bytes: int
    content_type: str | None
    entity_type: str
    entity_id: str
    filename: str | None
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "namespace": self.namespace,
            "bucket": self.bucket,
            "key": self.key,
            "object_id": self.object_id,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "filename": self.filename,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class UploadSession:
    """分片上传会话的本机记录。

    hub 的会话只存在内存里，重启即丢；本表用于跨请求续传与排障，
    因此每次成功上传一个分片都要落库。
    """

    upload_id: str
    bucket: str
    key: str
    sha256: str
    size_bytes: int
    chunk_size: int
    uploaded_chunks: tuple[int, ...] = field(default_factory=tuple)
    updated_at: str | None = None


@dataclass(frozen=True)
class UploadOutcome:
    """一次上传的结果；``deduped`` 表示命中秒传，未传输字节。"""

    record: StoredObject
    deduped: bool = False
    resumed: bool = False
    uploaded_chunks: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "object": self.record.to_dict(),
            "deduped": self.deduped,
            "resumed": self.resumed,
            "uploaded_chunks": self.uploaded_chunks,
        }


@dataclass(frozen=True)
class SignedLink:
    """签名短链；``expires_at`` 原样透传 hub 返回值（可能是 ISO 时间或时间戳）。"""

    url: str
    expires_sec: int
    expires_at: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "expires_sec": self.expires_sec,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class RedirectSpec:
    """302 跳转描述，供 router 直接构造响应，避免 service 依赖 fastapi。"""

    status_code: int
    location: str


@dataclass(frozen=True)
class DeleteOutcome:
    record_id: int
    object_id: str | None
    hub_deleted: bool
    record_deleted: bool
    shared_records: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "object_id": self.object_id,
            "hub_deleted": self.hub_deleted,
            "record_deleted": self.record_deleted,
            "shared_records": self.shared_records,
        }
