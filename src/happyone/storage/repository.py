"""``storage_objects`` / ``storage_upload_sessions`` 的读写。

连接来自 happy-one 自己的数据库（``happyone.db.get_conn``），这里只做 SQL，
不做任何网络访问。兼容 sqlite3 默认返回 tuple 的连接，不需要 row_factory。
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from .models import StoredObject, UploadSession

_OBJECT_COLUMNS = (
    "id, namespace, bucket, object_key, object_id, sha256, size_bytes, "
    "content_type, entity_type, entity_id, filename, created_at, updated_at"
)


def upsert_object(
    conn: Any,
    *,
    namespace: str,
    bucket: str,
    key: str,
    object_id: str | None,
    sha256: str,
    size_bytes: int,
    content_type: str | None,
    entity_type: str,
    entity_id: str,
    filename: str | None,
) -> StoredObject:
    """写入或更新一条实体映射；唯一键为 (entity_type, entity_id, bucket, object_key)。"""

    conn.execute(
        """
        INSERT INTO storage_objects (
            namespace, bucket, object_key, object_id, sha256, size_bytes,
            content_type, entity_type, entity_id, filename
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (entity_type, entity_id, bucket, object_key) DO UPDATE SET
            namespace = excluded.namespace,
            object_id = excluded.object_id,
            sha256 = excluded.sha256,
            size_bytes = excluded.size_bytes,
            content_type = excluded.content_type,
            filename = excluded.filename,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        """,
        (
            namespace,
            bucket,
            key,
            object_id,
            sha256,
            int(size_bytes),
            content_type,
            entity_type,
            entity_id,
            filename,
        ),
    )
    _commit(conn)
    row = _fetch_one(
        conn,
        f"SELECT {_OBJECT_COLUMNS} FROM storage_objects "
        "WHERE entity_type = ? AND entity_id = ? AND bucket = ? AND object_key = ?",
        (entity_type, entity_id, bucket, key),
    )
    if row is None:  # pragma: no cover - 刚写入必然存在
        raise RuntimeError("存储映射写入后未读到记录")
    return _to_object(row)


def get_by_id(conn: Any, record_id: int) -> StoredObject | None:
    row = _fetch_one(
        conn,
        f"SELECT {_OBJECT_COLUMNS} FROM storage_objects WHERE id = ?",
        (int(record_id),),
    )
    return _to_object(row) if row else None


def list_by_entity(
    conn: Any,
    entity_type: str,
    entity_id: str,
    *,
    namespace: str | None = None,
    limit: int = 200,
) -> list[StoredObject]:
    sql = f"SELECT {_OBJECT_COLUMNS} FROM storage_objects WHERE entity_type = ? AND entity_id = ?"
    params: list[Any] = [entity_type, entity_id]
    if namespace:
        sql += " AND namespace = ?"
        params.append(namespace)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return [_to_object(row) for row in _fetch_all(conn, sql, tuple(params))]


def count_shares(conn: Any, object_id: str, *, exclude_record_id: int | None = None) -> int:
    """统计还有多少条映射指向同一个 hub 对象（用于判断能否真正删除对象）。"""

    if not object_id:
        return 0
    sql = "SELECT COUNT(1) FROM storage_objects WHERE object_id = ?"
    params: list[Any] = [object_id]
    if exclude_record_id is not None:
        sql += " AND id <> ?"
        params.append(int(exclude_record_id))
    cursor = conn.execute(sql, tuple(params))
    row = cursor.fetchone()
    return int(row[0]) if row else 0


def delete_record(conn: Any, record_id: int) -> bool:
    cursor = conn.execute("DELETE FROM storage_objects WHERE id = ?", (int(record_id),))
    _commit(conn)
    return bool(getattr(cursor, "rowcount", 0))


def find_upload_session(
    conn: Any, *, bucket: str, key: str, sha256: str, size_bytes: int
) -> UploadSession | None:
    row = _fetch_one(
        conn,
        "SELECT upload_id, bucket, object_key, sha256, size_bytes, chunk_size, "
        "uploaded_chunks, updated_at FROM storage_upload_sessions "
        "WHERE bucket = ? AND object_key = ? AND sha256 = ? AND size_bytes = ? "
        "ORDER BY updated_at DESC LIMIT 1",
        (bucket, key, sha256, int(size_bytes)),
    )
    return _to_session(row) if row else None


def get_upload_session(conn: Any, upload_id: str) -> UploadSession | None:
    row = _fetch_one(
        conn,
        "SELECT upload_id, bucket, object_key, sha256, size_bytes, chunk_size, "
        "uploaded_chunks, updated_at FROM storage_upload_sessions WHERE upload_id = ?",
        (upload_id,),
    )
    return _to_session(row) if row else None


def upsert_upload_session(
    conn: Any,
    *,
    upload_id: str,
    bucket: str,
    key: str,
    sha256: str,
    size_bytes: int,
    chunk_size: int,
    uploaded_chunks: Sequence[int] = (),
) -> None:
    conn.execute(
        """
        INSERT INTO storage_upload_sessions (
            upload_id, bucket, object_key, sha256, size_bytes, chunk_size, uploaded_chunks
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (upload_id) DO UPDATE SET
            bucket = excluded.bucket,
            object_key = excluded.object_key,
            sha256 = excluded.sha256,
            size_bytes = excluded.size_bytes,
            chunk_size = excluded.chunk_size,
            uploaded_chunks = excluded.uploaded_chunks,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        """,
        (
            upload_id,
            bucket,
            key,
            sha256,
            int(size_bytes),
            int(chunk_size),
            _encode_chunks(uploaded_chunks),
        ),
    )
    _commit(conn)


def mark_chunk_uploaded(conn: Any, upload_id: str, index: int) -> UploadSession | None:
    """把已确认上传的分片下标合并进本机记录。"""

    session = get_upload_session(conn, upload_id)
    if session is None:
        return None
    chunks = sorted(set(session.uploaded_chunks) | {int(index)})
    conn.execute(
        "UPDATE storage_upload_sessions SET uploaded_chunks = ?, "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE upload_id = ?",
        (_encode_chunks(chunks), upload_id),
    )
    _commit(conn)
    return get_upload_session(conn, upload_id)


def delete_upload_session(conn: Any, upload_id: str) -> None:
    conn.execute("DELETE FROM storage_upload_sessions WHERE upload_id = ?", (upload_id,))
    _commit(conn)


def _encode_chunks(chunks: Sequence[int]) -> str:
    return json.dumps(sorted({int(item) for item in chunks}), ensure_ascii=False)


def _decode_chunks(raw: Any) -> tuple[int, ...]:
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    result: set[int] = set()
    for item in parsed:
        try:
            result.add(int(item))
        except (TypeError, ValueError):
            continue
    return tuple(sorted(result))


def _to_object(row: dict[str, Any]) -> StoredObject:
    return StoredObject(
        id=int(row["id"]),
        namespace=str(row["namespace"]),
        bucket=str(row["bucket"]),
        key=str(row["object_key"]),
        object_id=str(row["object_id"]) if row["object_id"] is not None else None,
        sha256=str(row["sha256"]),
        size_bytes=int(row["size_bytes"]),
        content_type=row["content_type"],
        entity_type=str(row["entity_type"]),
        entity_id=str(row["entity_id"]),
        filename=row["filename"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _to_session(row: dict[str, Any]) -> UploadSession:
    return UploadSession(
        upload_id=str(row["upload_id"]),
        bucket=str(row["bucket"]),
        key=str(row["object_key"]),
        sha256=str(row["sha256"]),
        size_bytes=int(row["size_bytes"]),
        chunk_size=int(row["chunk_size"]),
        uploaded_chunks=_decode_chunks(row["uploaded_chunks"]),
        updated_at=row["updated_at"],
    )


def _fetch_one(conn: Any, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    rows = _fetch_all(conn, sql, params)
    return rows[0] if rows else None


def _fetch_all(conn: Any, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, params)
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _commit(conn: Any) -> None:
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()
