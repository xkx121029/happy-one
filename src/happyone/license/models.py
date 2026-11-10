"""许可证模块的数据结构与常量。

本文件只放纯数据，不依赖 FastAPI / SQLite / cryptography，
便于 service、router、store 与测试共用同一套契约。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

# 令牌格式版本，出现在授权令牌与吊销快照的 payload 中
LICENSE_TOKEN_VERSION = 1
SNAPSHOT_TOKEN_VERSION = 1

# payload 类型标记，用于区分「许可证」与「吊销快照」，两者共用同一套签名格式
PAYLOAD_TYPE_LICENSE = "license"
PAYLOAD_TYPE_REVOCATION = "revocation_snapshot"

# 宽限期默认天数（许可证过期后仍可使用的天数）
DEFAULT_GRACE_DAYS = 7

# 校验状态
STATUS_VALID = "valid"
STATUS_IN_GRACE = "grace"


class ErrorCode:
    """对外错误码。成功响应不带 code，失败响应统一带 code + 中文 error。"""

    INVALID_REQUEST = "LICENSE_INVALID_REQUEST"
    MALFORMED = "LICENSE_MALFORMED"
    UNSUPPORTED_VERSION = "LICENSE_UNSUPPORTED_VERSION"
    WRONG_TYPE = "LICENSE_WRONG_TYPE"
    UNKNOWN_KEY = "LICENSE_UNKNOWN_KEY"
    INVALID_SIGNATURE = "LICENSE_INVALID_SIGNATURE"
    EXPIRED = "LICENSE_EXPIRED"
    REVOKED = "LICENSE_REVOKED"
    DEVICE_REQUIRED = "LICENSE_DEVICE_REQUIRED"
    DEVICE_MISMATCH = "LICENSE_DEVICE_MISMATCH"
    DEVICE_LIMIT = "LICENSE_DEVICE_LIMIT"
    NOT_FOUND = "LICENSE_NOT_FOUND"
    ALREADY_REVOKED = "LICENSE_ALREADY_REVOKED"
    SNAPSHOT_MALFORMED = "SNAPSHOT_MALFORMED"
    SNAPSHOT_WRONG_TYPE = "SNAPSHOT_WRONG_TYPE"
    SNAPSHOT_UNKNOWN_KEY = "SNAPSHOT_UNKNOWN_KEY"
    SNAPSHOT_INVALID_SIGNATURE = "SNAPSHOT_INVALID_SIGNATURE"
    SERVICE_UNAVAILABLE = "LICENSE_SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "LICENSE_INTERNAL_ERROR"


class PayloadFormatError(ValueError):
    """payload 结构不合法（字段缺失、类型错误等）。"""


def format_utc(timestamp: int) -> str:
    """把 Unix 秒格式化为便于阅读的 UTC 时间文本。"""
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _require_text(data: Mapping[str, Any], key: str, *, max_len: int = 512) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise PayloadFormatError(f"字段 {key} 必须是字符串")
    value = value.strip()
    if not value:
        raise PayloadFormatError(f"字段 {key} 不能为空")
    if len(value) > max_len:
        raise PayloadFormatError(f"字段 {key} 长度超过 {max_len}")
    return value


def _require_int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PayloadFormatError(f"字段 {key} 必须是整数")
    return value


@dataclass(frozen=True)
class DeviceBinding:
    """设备绑定信息。

    fp_hash 非空表示单设备绑定（离线即可判定）；
    fp_hash 为空且 max_devices > 1 表示多设备许可，设备数量由服务端激活记录约束。
    """

    max_devices: int = 1
    fp_hash: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.max_devices, int) or isinstance(self.max_devices, bool) or self.max_devices < 1:
            raise PayloadFormatError("device.max_devices 必须是大于 0 的整数")
        if self.max_devices > 1000:
            raise PayloadFormatError("device.max_devices 不能超过 1000")
        if self.fp_hash is not None:
            if not isinstance(self.fp_hash, str) or not self.fp_hash:
                raise PayloadFormatError("device.fp_hash 必须是非空字符串")
            if len(self.fp_hash) > 128:
                raise PayloadFormatError("device.fp_hash 长度超过 128")

    def to_dict(self) -> dict[str, Any]:
        return {"max_devices": self.max_devices, "fp_hash": self.fp_hash}

    @classmethod
    def from_dict(cls, data: Any) -> "DeviceBinding":
        if not isinstance(data, Mapping):
            raise PayloadFormatError("device 必须是对象或 null")
        return cls(
            max_devices=_require_int(data, "max_devices"),
            fp_hash=data.get("fp_hash"),
        )


@dataclass(frozen=True)
class LicensePayload:
    """许可证令牌中承载的、被签名的完整内容。"""

    license_id: str
    user_id: str
    product_slug: str
    features: tuple[str, ...]
    issued_at: int
    expires_at: int
    kid: str
    device: DeviceBinding | None = None
    ver: int = LICENSE_TOKEN_VERSION
    typ: str = PAYLOAD_TYPE_LICENSE

    def to_dict(self) -> dict[str, Any]:
        return {
            "typ": self.typ,
            "ver": self.ver,
            "kid": self.kid,
            "license_id": self.license_id,
            "user_id": self.user_id,
            "product_slug": self.product_slug,
            "features": list(self.features),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "device": self.device.to_dict() if self.device is not None else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LicensePayload":
        if not isinstance(data, Mapping):
            raise PayloadFormatError("许可证内容必须是 JSON 对象")

        typ = data.get("typ", PAYLOAD_TYPE_LICENSE)
        if not isinstance(typ, str):
            raise PayloadFormatError("字段 typ 必须是字符串")
        ver = data.get("ver", LICENSE_TOKEN_VERSION)
        if isinstance(ver, bool) or not isinstance(ver, int):
            raise PayloadFormatError("字段 ver 必须是整数")

        features_raw = data.get("features", [])
        if not isinstance(features_raw, (list, tuple)):
            raise PayloadFormatError("字段 features 必须是字符串数组")
        features: list[str] = []
        for item in features_raw:
            if not isinstance(item, str) or not item.strip():
                raise PayloadFormatError("features 中包含无效条目：必须是非空字符串")
            features.append(item.strip())

        device_raw = data.get("device")
        device = DeviceBinding.from_dict(device_raw) if device_raw is not None else None

        return cls(
            license_id=_require_text(data, "license_id", max_len=64),
            user_id=_require_text(data, "user_id", max_len=128),
            product_slug=_require_text(data, "product_slug", max_len=64),
            features=tuple(features),
            issued_at=_require_int(data, "issued_at"),
            expires_at=_require_int(data, "expires_at"),
            kid=_require_text(data, "kid", max_len=64),
            device=device,
            ver=ver,
            typ=typ,
        )


@dataclass(frozen=True)
class IssueRequest:
    """签发请求（由 router 从 HTTP 请求体解析后传入 service）。"""

    user_id: str
    product_slug: str
    features: tuple[str, ...] = ()
    expires_at: int | None = None
    expires_in_days: int | None = None
    device_fingerprint: str | None = None
    max_devices: int | None = None
    license_id: str | None = None


@dataclass(frozen=True)
class IssuedLicense:
    payload: LicensePayload
    token: str


@dataclass(frozen=True)
class RevocationEntry:
    """一条吊销记录，也是吊销快照中的最小单元。"""

    license_id: str
    revoked_at: int
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"license_id": self.license_id, "revoked_at": self.revoked_at, "reason": self.reason}

    @classmethod
    def from_row(cls, row: Any) -> "RevocationEntry":
        license_id, revoked_at, reason = row[0], row[1], row[2]
        return cls(license_id=str(license_id), revoked_at=int(revoked_at), reason=str(reason or ""))


@dataclass(frozen=True)
class LicenseRecord:
    """数据库中的签发记录（供查询与吊销使用）。"""

    license_id: str
    user_id: str
    product_slug: str
    features: tuple[str, ...]
    issued_at: int
    expires_at: int
    kid: str
    token: str
    device_max: int | None = None
    device_fp_hash: str | None = None
    created_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "license_id": self.license_id,
            "user_id": self.user_id,
            "product_slug": self.product_slug,
            "features": list(self.features),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "kid": self.kid,
            "device_max": self.device_max,
            "device_fp_hash": self.device_fp_hash,
            "created_at": self.created_at,
            "token": self.token,
        }


@dataclass(frozen=True)
class VerifyResult:
    """校验结果。业务失败不抛异常，统一走 ok=False + code + 中文原因。"""

    ok: bool
    code: str = ""
    error: str = ""
    payload: LicensePayload | None = None
    status: str = ""
    warnings: tuple[str, ...] = ()
    grace_days_left: float | None = None

    @classmethod
    def failure(cls, code: str, error: str) -> "VerifyResult":
        return cls(ok=False, code=code, error=error)

    def to_envelope(self) -> dict[str, Any]:
        if not self.ok or self.payload is None:
            return {"ok": False, "code": self.code, "error": self.error}

        payload = self.payload
        data: dict[str, Any] = {
            "status": self.status,
            "license_id": payload.license_id,
            "user_id": payload.user_id,
            "product_slug": payload.product_slug,
            "features": list(payload.features),
            "issued_at": payload.issued_at,
            "issued_at_text": format_utc(payload.issued_at),
            "expires_at": payload.expires_at,
            "expires_at_text": format_utc(payload.expires_at),
            "device": payload.device.to_dict() if payload.device is not None else None,
            "grace_days_left": round(self.grace_days_left, 2) if self.grace_days_left is not None else None,
            "warnings": list(self.warnings),
            "payload": payload.to_dict(),
        }
        return {"ok": True, "data": data}


@dataclass(frozen=True)
class SnapshotResult:
    """吊销快照结果：token 为已签名的快照字符串，客户端可离线验签。"""

    ok: bool
    code: str = ""
    error: str = ""
    token: str = ""
    payload: dict[str, Any] | None = None
    entries: tuple[RevocationEntry, ...] = ()
    kid: str = ""
    issued_at: int = 0

    @classmethod
    def failure(cls, code: str, error: str) -> "SnapshotResult":
        return cls(ok=False, code=code, error=error)

    def to_envelope(self) -> dict[str, Any]:
        if not self.ok or self.payload is None:
            return {"ok": False, "code": self.code, "error": self.error}
        return {
            "ok": True,
            "data": {
                "token": self.token,
                "kid": self.kid,
                "issued_at": self.issued_at,
                "issued_at_text": format_utc(self.issued_at),
                "count": len(self.entries),
                "entries": [entry.to_dict() for entry in self.entries],
                "payload": self.payload,
            },
        }


def normalize_features(features: Iterable[str]) -> tuple[str, ...]:
    """去重并保持首次出现顺序，同时去掉首尾空白。"""
    seen: set[str] = set()
    result: list[str] = []
    for item in features:
        text = item.strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return tuple(result)
