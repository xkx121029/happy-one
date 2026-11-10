"""桶常量、命名空间与对象 key 命名规范。

命名规范（写入 hub 的 key 一律小写、只用 `/` 分段）::

    <namespace>/<sha256>                         内容寻址，去重与秒传使用
    <namespace>/<entity_type>/<entity_id>/<file> 业务实体归档，保留可读文件名

namespace 与桶一一对应，不允许调用方随意指定桶：

    relay              -> relay               中继/转发类业务文件
    license            -> license-artifacts   授权文件与签发产物
    backup             -> backup              备份归档
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

BUCKET_RELAY = "relay"
BUCKET_LICENSE_ARTIFACTS = "license-artifacts"
BUCKET_BACKUP = "backup"

NAMESPACE_BUCKETS: dict[str, str] = {
    "relay": BUCKET_RELAY,
    "license": BUCKET_LICENSE_ARTIFACTS,
    "backup": BUCKET_BACKUP,
}

MAX_KEY_LENGTH = 512
MAX_FILENAME_LENGTH = 128
MAX_SEGMENT_LENGTH = 128

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_FILENAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def bucket_for(namespace: str) -> str:
    """返回命名空间固定对应的桶名。"""

    normalized = normalize_namespace(namespace)
    return NAMESPACE_BUCKETS[normalized]


def normalize_namespace(namespace: str) -> str:
    value = str(namespace or "").strip().lower()
    allowed = "、".join(sorted(NAMESPACE_BUCKETS))
    if not value:
        raise ValueError(f"存储命名空间不能为空，可选值为 {allowed}")
    if value not in NAMESPACE_BUCKETS:
        raise ValueError(f"未知的存储命名空间：{namespace!r}，可选值为 {allowed}")
    return value


def validate_sha256(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.match(normalized):
        raise ValueError("sha256 必须是 64 位小写十六进制字符串")
    return normalized


def content_addressed_key(namespace: str, sha256: str) -> str:
    """内容寻址 key：``<namespace>/<sha256>``，同一份内容天然落在同一个 key。"""

    ns = normalize_namespace(namespace)
    digest = validate_sha256(sha256)
    return f"{ns}/{digest}"


def entity_key(namespace: str, entity_type: str, entity_id: str, filename: str) -> str:
    """业务实体 key：``<namespace>/<entity_type>/<entity_id>/<filename>``。"""

    ns = normalize_namespace(namespace)
    type_seg = _normalize_segment(entity_type, "entity_type")
    id_seg = _normalize_segment(entity_id, "entity_id")
    name = sanitize_filename(filename)
    key = f"{ns}/{type_seg}/{id_seg}/{name}"
    return validate_key(key)


def sanitize_filename(filename: str) -> str:
    """把任意文件名压成 key 里安全的一段，保留扩展名。

    原名含有非 ASCII 或路径不安全字符时，追加原名的短摘要，避免两个不同
    文件名清洗后撞到同一个 key（例如两个纯中文文件名）。
    """

    raw = str(filename or "").strip().replace("\\", "/")
    base = PurePosixPath(raw).name or "file"
    stem, dot, suffix = base.rpartition(".")
    if not dot:
        stem, suffix = base, ""
    stem = stem.lower()
    suffix = _FILENAME_UNSAFE_RE.sub("", suffix).lower()[:16]
    safe_stem = _FILENAME_UNSAFE_RE.sub("_", stem).strip("._-")[:MAX_FILENAME_LENGTH] or "file"
    changed = safe_stem != stem or bool(_FILENAME_UNSAFE_RE.search(stem))
    if changed:
        digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
        safe_stem = f"{safe_stem}-{digest}"
    if suffix:
        return f"{safe_stem}.{suffix}"
    return safe_stem


def validate_key(key: str) -> str:
    """校验对象 key：小写、无空段、无 ``..``、长度受限。"""

    value = str(key or "").strip()
    if not value:
        raise ValueError("对象 key 不能为空")
    if value.startswith("/") or value.endswith("/"):
        raise ValueError("对象 key 不能以 / 开头或结尾")
    if "\\" in value or "?" in value or "#" in value:
        raise ValueError("对象 key 不能包含反斜杠、问号或井号")
    if len(value) > MAX_KEY_LENGTH:
        raise ValueError(f"对象 key 长度不能超过 {MAX_KEY_LENGTH} 个字符")
    for segment in value.split("/"):
        if not segment:
            raise ValueError("对象 key 不能包含空的路径段")
        if segment in (".", ".."):
            raise ValueError("对象 key 不能包含 . 或 .. 路径段")
        if not _SEGMENT_RE.match(segment) or len(segment) > MAX_SEGMENT_LENGTH:
            raise ValueError(f"对象 key 的路径段不合法：{segment!r}")
    return value


def is_content_addressed(key: str) -> bool:
    """判断 key 是否为 ``<namespace>/<sha256>`` 形式。"""

    parts = str(key or "").split("/")
    return len(parts) == 2 and parts[0] in NAMESPACE_BUCKETS and bool(_SHA256_RE.match(parts[1]))


def _normalize_segment(value: str, field: str) -> str:
    normalized = str(value or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9._-]+", "-", normalized).strip(".-")
    if not normalized:
        raise ValueError(f"{field} 不能为空，且只能包含字母、数字、点、下划线和中划线")
    return normalized[:MAX_SEGMENT_LENGTH]
