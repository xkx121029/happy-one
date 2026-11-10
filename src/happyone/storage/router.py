"""存储模块的 HTTP 接口。

骨架线负责挂载：``app.include_router(storage_router)``，路径前缀已在路由内
声明为 ``/storage``。所有响应沿用 hub 风格包络：成功 ``{"ok": true, ...}``，
失败 ``{"ok": false, "code": ..., "error": "中文"}``。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse

from . import deps, keys
from .constants import READ_BLOCK_SIZE, SIGN_TTL_MAX, SIGN_TTL_MIN
from .errors import StorageError
from .models import StoredObject
from .service import StorageService

router = APIRouter(prefix="/storage", tags=["storage"])


@router.get("/info")
async def storage_info() -> dict[str, Any]:
    """返回模块支持的命名空间、桶与签名有效期范围，便于前端做选项。"""

    return _ok(
        namespaces=[
            {"namespace": ns, "bucket": bucket}
            for ns, bucket in sorted(keys.NAMESPACE_BUCKETS.items())
        ],
        sign_ttl={"min": SIGN_TTL_MIN, "max": SIGN_TTL_MAX},
    )


@router.post("/upload", status_code=201)
async def upload(
    file: UploadFile = File(..., description="要上传的文件"),
    namespace: str | None = Form(None, description="relay / license / backup"),
    entity_type: str | None = Form(None, description="业务实体类型，如 license"),
    entity_id: str | None = Form(None, description="业务实体编号"),
    filename: str | None = Form(None, description="覆盖 key 中的文件名，留空用上传文件名"),
    content_type: str | None = Form(None, description="覆盖内容类型，留空用上传声明的类型"),
) -> Any:
    staged_path: Path | None = None
    try:
        staged_path = await _spool(file)
        service = deps.build_service()
        outcome = await service.upload(
            namespace=namespace or "",
            entity_type=entity_type or "",
            entity_id=entity_id or "",
            filename=filename or file.filename,
            source=staged_path,
            content_type=content_type or file.content_type,
        )
        return _ok(**outcome.to_dict())
    except StorageError as exc:
        return _error(exc)
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)
        await file.close()


@router.get("/objects/{record_id}")
async def get_object(record_id: int) -> Any:
    try:
        return _ok(object=_record(deps.build_service().get(record_id)))
    except StorageError as exc:
        return _error(exc)


@router.get("/objects/{record_id}/link")
async def get_download_link(
    record_id: int,
    expires_sec: str | None = Query(
        None, description="下载短链有效期（秒），会被收敛到 60–900"
    ),
) -> Any:
    try:
        link = await deps.build_service().sign_download(record_id, expires_sec)
        return _ok(**link.to_dict())
    except StorageError as exc:
        return _error(exc)


@router.get("/objects/{record_id}/redirect")
async def redirect_download(
    record_id: int,
    expires_sec: str | None = Query(None, description="下载短链有效期（秒）"),
) -> Any:
    try:
        spec = await deps.build_service().sign_redirect(record_id, expires_sec)
        return RedirectResponse(url=spec.location, status_code=spec.status_code)
    except StorageError as exc:
        return _error(exc)


@router.delete("/objects/{record_id}")
async def delete_object(record_id: int) -> Any:
    try:
        outcome = await deps.build_service().delete(record_id)
        return _ok(**outcome.to_dict())
    except StorageError as exc:
        return _error(exc)


@router.get("/entities/{entity_type}/{entity_id}")
async def list_entity_files(
    entity_type: str,
    entity_id: str,
    namespace: str | None = Query(None, description="按命名空间过滤"),
) -> Any:
    try:
        records = deps.build_service().list_files(entity_type, entity_id, namespace=namespace)
        return _ok(items=[_record(item) for item in records], total=len(records))
    except StorageError as exc:
        return _error(exc)


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def _error(exc: StorageError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=exc.to_payload())


def _record(record: StoredObject) -> dict[str, Any]:
    return record.to_dict()


async def _spool(file: UploadFile) -> Path:
    """把上传流落到临时文件，避免大文件全部进内存。"""

    handle = tempfile.NamedTemporaryFile(prefix="happyone-upload-", delete=False)
    path = Path(handle.name)
    try:
        while True:
            block = await file.read(READ_BLOCK_SIZE)
            if not block:
                break
            handle.write(block)
    finally:
        handle.close()
    return path


__all__ = ["router"]
