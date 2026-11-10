"""许可证核心逻辑（纯逻辑，不 import fastapi）。

职责：签发、校验（离线验签 → 过期 → 吊销 → 设备绑定 → 宽限期）、吊销、吊销快照。
数据库通过构造参数注入，默认 ``None`` 时即为纯离线模式（仅验签/过期/宽限期/单设备指纹）。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
import uuid
from typing import Any, Callable, Mapping, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import codec
from .codec import SignatureError, TokenFormatError
from .keys import KeyPair, SingleKeyProvider, public_key_info
from .models import (
    DEFAULT_GRACE_DAYS,
    PAYLOAD_TYPE_LICENSE,
    PAYLOAD_TYPE_REVOCATION,
    SNAPSHOT_TOKEN_VERSION,
    STATUS_IN_GRACE,
    STATUS_VALID,
    DeviceBinding,
    ErrorCode,
    IssueRequest,
    IssuedLicense,
    LicensePayload,
    LicenseRecord,
    PayloadFormatError,
    RevocationEntry,
    SnapshotResult,
    VerifyResult,
    format_utc,
    normalize_features,
)
from .store import DeviceLimitError, StoreProtocol

_LICENSE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


class LicenseError(Exception):
    """带错误码与 HTTP 状态的业务异常（router 直接映射为包络）。"""

    code: str = ErrorCode.INVALID_REQUEST
    status_code: int = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class LicenseValidationError(LicenseError):
    code = ErrorCode.INVALID_REQUEST
    status_code = 400


class LicenseNotFoundError(LicenseError):
    code = ErrorCode.NOT_FOUND
    status_code = 404


class LicenseAlreadyRevokedError(LicenseError):
    code = ErrorCode.ALREADY_REVOKED
    status_code = 409


class LicenseServiceUnavailable(LicenseError):
    code = ErrorCode.SERVICE_UNAVAILABLE
    status_code = 503


class PublicKeyProvider(Protocol):
    def __call__(self, kid: str) -> Ed25519PublicKey | None: ...


def hash_device_fingerprint(fingerprint: str) -> str:
    """设备指纹 → SHA-256 十六进制。

    客户端必须使用完全相同的规则：对 ``fingerprint.strip()`` 的 UTF-8 字节做 SHA-256。
    许可证中只保存该哈希，不保存指纹原文。
    """
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        raise LicenseValidationError("设备指纹不能为空")
    return hashlib.sha256(fingerprint.strip().encode("utf-8")).hexdigest()


def verify_revocation_snapshot(
    token: str,
    public_key_provider: PublicKeyProvider,
) -> SnapshotResult:
    """客户端离线校验吊销快照：验签 + 类型检查，返回快照内容与吊销清单。"""
    try:
        raw, signature_bytes, signing_input = codec.decode_payload(token)
    except TokenFormatError as exc:
        return SnapshotResult.failure(ErrorCode.SNAPSHOT_MALFORMED, f"吊销快照格式不正确：{exc}")

    kid = raw.get("kid")
    if not isinstance(kid, str) or not kid:
        return SnapshotResult.failure(ErrorCode.SNAPSHOT_MALFORMED, "吊销快照格式不正确：缺少 kid 字段")

    public_key = public_key_provider(kid)
    if public_key is None:
        return SnapshotResult.failure(
            ErrorCode.SNAPSHOT_UNKNOWN_KEY, f"吊销快照签名公钥未知（kid={kid}），无法验签"
        )
    try:
        codec.verify_signature(signing_input, signature_bytes, public_key)
    except SignatureError as exc:
        return SnapshotResult.failure(ErrorCode.SNAPSHOT_INVALID_SIGNATURE, f"吊销快照{exc}")

    if raw.get("typ") != PAYLOAD_TYPE_REVOCATION:
        return SnapshotResult.failure(ErrorCode.SNAPSHOT_WRONG_TYPE, "该令牌不是吊销快照，无法按快照校验")

    ver = raw.get("ver")
    if ver != SNAPSHOT_TOKEN_VERSION:
        return SnapshotResult.failure(
            ErrorCode.SNAPSHOT_MALFORMED, f"吊销快照版本 {ver!r} 不受支持（当前支持 {SNAPSHOT_TOKEN_VERSION}）"
        )

    issued_at = raw.get("issued_at")
    if isinstance(issued_at, bool) or not isinstance(issued_at, int):
        return SnapshotResult.failure(ErrorCode.SNAPSHOT_MALFORMED, "吊销快照缺少合法的 issued_at")

    entries_raw = raw.get("entries")
    if not isinstance(entries_raw, list):
        return SnapshotResult.failure(ErrorCode.SNAPSHOT_MALFORMED, "吊销快照缺少 entries 数组")

    entries: list[RevocationEntry] = []
    for item in entries_raw:
        if not isinstance(item, Mapping):
            return SnapshotResult.failure(ErrorCode.SNAPSHOT_MALFORMED, "吊销快照 entries 中存在非法条目")
        license_id = item.get("license_id")
        revoked_at = item.get("revoked_at")
        reason = item.get("reason", "")
        if not isinstance(license_id, str) or not license_id:
            return SnapshotResult.failure(ErrorCode.SNAPSHOT_MALFORMED, "吊销快照 entries 缺少 license_id")
        if isinstance(revoked_at, bool) or not isinstance(revoked_at, int):
            return SnapshotResult.failure(ErrorCode.SNAPSHOT_MALFORMED, "吊销快照 entries 缺少合法的 revoked_at")
        entries.append(
            RevocationEntry(license_id=license_id, revoked_at=revoked_at, reason=str(reason or ""))
        )

    return SnapshotResult(
        ok=True,
        token=token,
        payload=dict(raw),
        entries=tuple(entries),
        kid=kid,
        issued_at=issued_at,
    )


class LicenseService:
    """签发 / 校验 / 吊销 / 快照的统一入口。"""

    def __init__(
        self,
        key_pair: KeyPair,
        *,
        store: StoreProtocol | None = None,
        grace_days: int = DEFAULT_GRACE_DAYS,
        clock: Callable[[], float] = time.time,
        public_key_provider: PublicKeyProvider | None = None,
    ) -> None:
        self._key_pair = key_pair
        self._store = store
        self._grace_days = max(0, int(grace_days))
        self._clock = clock
        self._provider: PublicKeyProvider = public_key_provider or SingleKeyProvider(key_pair)

    # ---------- 基本信息 ----------

    @property
    def key_pair(self) -> KeyPair:
        return self._key_pair

    @property
    def grace_days(self) -> int:
        return self._grace_days

    def _now(self) -> int:
        return int(self._clock())

    def public_key_info(self) -> dict[str, Any]:
        info = public_key_info(self._key_pair)
        info["grace_days"] = self._grace_days
        return info

    # ---------- 签发 ----------

    def issue(self, request: IssueRequest, *, now: int | None = None) -> IssuedLicense:
        issued_at = self._now() if now is None else int(now)

        user_id = _clean_text(request.user_id, "user_id", 128)
        product_slug = _clean_text(request.product_slug, "product_slug", 64)
        features = tuple(_clean_text(item, "features 条目", 64) for item in request.features)
        if len(features) > 64:
            raise LicenseValidationError("features 最多 64 项")

        expires_at = self._resolve_expiry(request, issued_at)
        device = self._resolve_device(request)

        if request.license_id is not None:
            license_id = request.license_id.strip()
            if not _LICENSE_ID_PATTERN.match(license_id):
                raise LicenseValidationError("license_id 只能由字母、数字与 . _ : - 组成，长度 1-64")
        else:
            license_id = f"lic_{uuid.uuid4().hex}"

        if self._store is not None and self._store.get_license(license_id) is not None:
            raise LicenseValidationError(f"许可证编号已存在：{license_id}")

        payload = LicensePayload(
            license_id=license_id,
            user_id=user_id,
            product_slug=product_slug,
            features=normalize_features(features),
            issued_at=issued_at,
            expires_at=expires_at,
            kid=self._key_pair.kid,
            device=device,
        )
        token = codec.sign_payload(payload.to_dict(), self._key_pair.private_key)

        if self._store is not None:
            self._store.save_license(
                LicenseRecord(
                    license_id=payload.license_id,
                    user_id=payload.user_id,
                    product_slug=payload.product_slug,
                    features=payload.features,
                    issued_at=payload.issued_at,
                    expires_at=payload.expires_at,
                    kid=payload.kid,
                    token=token,
                    device_max=device.max_devices if device is not None else None,
                    device_fp_hash=device.fp_hash if device is not None else None,
                    created_at=issued_at,
                )
            )

        return IssuedLicense(payload=payload, token=token)

    def _resolve_expiry(self, request: IssueRequest, issued_at: int) -> int:
        if request.expires_at is not None and request.expires_in_days is not None:
            raise LicenseValidationError("expires_at 与 expires_in_days 只能二选一")
        if request.expires_at is not None:
            value = request.expires_at
            if isinstance(value, bool) or not isinstance(value, int):
                raise LicenseValidationError("expires_at 必须是 Unix 秒整数")
            expires_at = value
        elif request.expires_in_days is not None:
            days = request.expires_in_days
            if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650:
                raise LicenseValidationError("expires_in_days 必须是 1 到 3650 之间的整数")
            expires_at = issued_at + days * 86400
        else:
            raise LicenseValidationError("必须提供 expires_at 或 expires_in_days 之一")

        if expires_at <= issued_at:
            raise LicenseValidationError(
                f"expires_at 必须晚于签发时间（签发 {format_utc(issued_at)}，过期 {format_utc(expires_at)}）"
            )
        return expires_at

    def _resolve_device(self, request: IssueRequest) -> DeviceBinding | None:
        fingerprint = (request.device_fingerprint or "").strip()
        max_devices = request.max_devices

        if max_devices is not None:
            if isinstance(max_devices, bool) or not isinstance(max_devices, int) or not 1 <= max_devices <= 1000:
                raise LicenseValidationError("max_devices 必须是 1 到 1000 之间的整数")

        if fingerprint and len(fingerprint) > 512:
            raise LicenseValidationError("device_fingerprint 长度不能超过 512")

        if fingerprint:
            if max_devices is not None and max_devices != 1:
                raise LicenseValidationError(
                    "绑定具体设备指纹时 max_devices 只能为 1；如需多设备许可请只设置 max_devices"
                )
            return DeviceBinding(max_devices=1, fp_hash=hash_device_fingerprint(fingerprint))

        if max_devices is not None:
            return DeviceBinding(max_devices=max_devices, fp_hash=None)

        return None

    # ---------- 校验 ----------

    def verify(
        self,
        token: str,
        *,
        device_fingerprint: str | None = None,
        now: int | None = None,
    ) -> VerifyResult:
        current = self._now() if now is None else int(now)

        # 1. 结构解析
        try:
            raw, signature_bytes, signing_input = codec.decode_payload(token)
        except TokenFormatError as exc:
            return VerifyResult.failure(ErrorCode.MALFORMED, f"许可证格式不正确：{exc}")

        kid = raw.get("kid")
        if not isinstance(kid, str) or not kid:
            return VerifyResult.failure(ErrorCode.MALFORMED, "许可证格式不正确：缺少 kid 字段")

        # 2. 验签（签名输入来自令牌原文，任何改动都会失败）
        public_key = self._provider(kid)
        if public_key is None:
            return VerifyResult.failure(
                ErrorCode.UNKNOWN_KEY, f"许可证签名公钥未知（kid={kid}），无法完成验签"
            )
        try:
            codec.verify_signature(signing_input, signature_bytes, public_key)
        except SignatureError as exc:
            return VerifyResult.failure(ErrorCode.INVALID_SIGNATURE, str(exc))

        # 3. 类型与版本（验签通过后才信任字段）
        if raw.get("typ") != PAYLOAD_TYPE_LICENSE:
            return VerifyResult.failure(ErrorCode.WRONG_TYPE, "该令牌不是许可证，无法按许可证校验")
        if raw.get("ver") != 1:
            return VerifyResult.failure(
                ErrorCode.UNSUPPORTED_VERSION,
                f"许可证版本 {raw.get('ver')!r} 不受支持（当前支持 1）",
            )

        try:
            payload = LicensePayload.from_dict(raw)
        except PayloadFormatError as exc:
            return VerifyResult.failure(ErrorCode.MALFORMED, f"许可证内容不完整：{exc}")

        # 4. 过期（超出宽限期才判定失败）
        grace_seconds = self._grace_days * 86400
        if current > payload.expires_at + grace_seconds:
            return VerifyResult.failure(
                ErrorCode.EXPIRED,
                f"许可证已于 {format_utc(payload.expires_at)} 过期，"
                f"{self._grace_days} 天宽限期也已结束（当前时间 {format_utc(current)}）",
            )

        # 5. 吊销
        revoked = self._lookup_revocation(payload.license_id)
        if revoked is not None:
            reason = f"：{revoked.reason}" if revoked.reason else ""
            return VerifyResult.failure(
                ErrorCode.REVOKED,
                f"许可证已被吊销{reason}（吊销时间 {format_utc(revoked.revoked_at)}）",
            )

        # 6. 设备绑定
        warnings: list[str] = []
        device_failure = self._check_device(payload, device_fingerprint, current, warnings)
        if device_failure is not None:
            return device_failure

        # 7. 宽限期
        status = STATUS_VALID
        grace_days_left: float | None = None
        if current > payload.expires_at:
            status = STATUS_IN_GRACE
            grace_days_left = max(0.0, (payload.expires_at + grace_seconds - current) / 86400)
            warnings.append(
                f"许可证已于 {format_utc(payload.expires_at)} 过期，当前处于 {self._grace_days} 天宽限期，"
                f"剩余约 {grace_days_left:.1f} 天"
            )
        if payload.device is None:
            warnings.append("该许可证未绑定设备，任何设备均可使用。")

        return VerifyResult(
            ok=True,
            payload=payload,
            status=status,
            warnings=tuple(warnings),
            grace_days_left=grace_days_left,
        )

    def _lookup_revocation(self, license_id: str) -> RevocationEntry | None:
        if self._store is None:
            return None
        return self._store.get_revocation(license_id)

    def _check_device(
        self,
        payload: LicensePayload,
        device_fingerprint: str | None,
        now: int,
        warnings: list[str],
    ) -> VerifyResult | None:
        binding = payload.device
        if binding is None:
            return None

        if binding.fp_hash is not None:
            if not device_fingerprint or not device_fingerprint.strip():
                return VerifyResult.failure(
                    ErrorCode.DEVICE_REQUIRED, "该许可证已绑定设备，请提供设备指纹后再校验"
                )
            actual = hash_device_fingerprint(device_fingerprint)
            if not hmac.compare_digest(actual, binding.fp_hash):
                return VerifyResult.failure(ErrorCode.DEVICE_MISMATCH, "设备指纹与许可证绑定的设备不一致")
            return None

        # 多设备许可：需要服务端激活记录配合
        if self._store is None:
            warnings.append(
                f"该许可证最多允许 {binding.max_devices} 台设备，离线模式无法校验设备数量，请联网确认。"
            )
            return None

        if not device_fingerprint or not device_fingerprint.strip():
            return VerifyResult.failure(
                ErrorCode.DEVICE_REQUIRED,
                f"该许可证最多允许 {binding.max_devices} 台设备，请提供设备指纹以完成激活",
            )
        actual = hash_device_fingerprint(device_fingerprint)
        try:
            self._store.ensure_device(payload.license_id, actual, binding.max_devices, now)
        except DeviceLimitError as exc:
            return VerifyResult.failure(
                ErrorCode.DEVICE_LIMIT,
                f"设备数已超限：该许可证最多允许 {exc.limit} 台设备，当前已激活 {exc.current} 台",
            )
        return None

    # ---------- 查询 ----------

    def get(self, license_id: str) -> LicenseRecord | None:
        if self._store is None:
            raise LicenseServiceUnavailable("当前服务未配置许可证数据库，无法查询签发记录")
        return self._store.get_license(license_id)

    def describe(self, license_id: str) -> dict[str, Any]:
        """查询接口的完整信息：签发记录 + 当前吊销状态。"""
        record = self.get(license_id)
        if record is None:
            raise LicenseNotFoundError(f"未找到许可证：{license_id}")
        assert self._store is not None  # get() 已保证
        revocation = self._store.get_revocation(license_id)
        data = record.to_dict()
        data["issued_at_text"] = format_utc(record.issued_at)
        data["expires_at_text"] = format_utc(record.expires_at)
        data["revoked"] = revocation is not None
        data["revoked_at"] = revocation.revoked_at if revocation is not None else None
        data["revoked_reason"] = revocation.reason if revocation is not None else None
        return data

    # ---------- 吊销 ----------

    def revoke(
        self,
        license_id: str,
        *,
        reason: str = "",
        revoked_by: str | None = None,
        now: int | None = None,
    ) -> RevocationEntry:
        if self._store is None:
            raise LicenseServiceUnavailable("当前服务未配置许可证数据库，无法吊销许可证")
        license_id = _clean_text(license_id, "license_id", 64)
        if self._store.get_license(license_id) is None:
            raise LicenseNotFoundError(f"未找到许可证：{license_id}")

        entry = RevocationEntry(
            license_id=license_id,
            revoked_at=self._now() if now is None else int(now),
            reason=(reason or "").strip()[:200],
        )
        if not self._store.add_revocation(entry, revoked_by):
            raise LicenseAlreadyRevokedError(f"许可证 {license_id} 已被吊销，无需重复操作")
        return entry

    # ---------- 吊销快照 ----------

    def revocation_snapshot(self, *, now: int | None = None) -> SnapshotResult:
        if self._store is None:
            raise LicenseServiceUnavailable("当前服务未配置许可证数据库，无法导出吊销快照")

        issued_at = self._now() if now is None else int(now)
        entries = tuple(self._store.list_revocations())
        payload: dict[str, Any] = {
            "typ": PAYLOAD_TYPE_REVOCATION,
            "ver": SNAPSHOT_TOKEN_VERSION,
            "kid": self._key_pair.kid,
            "issued_at": issued_at,
            "count": len(entries),
            "entries": [entry.to_dict() for entry in entries],
        }
        token = codec.sign_payload(payload, self._key_pair.private_key)
        return SnapshotResult(
            ok=True,
            token=token,
            payload=payload,
            entries=entries,
            kid=self._key_pair.kid,
            issued_at=issued_at,
        )

    def snapshot_contains(self, snapshot: SnapshotResult, license_id: str) -> bool:
        """在已验签的快照里判断某个许可证是否被吊销。"""
        return any(entry.license_id == license_id for entry in snapshot.entries)


def _clean_text(value: Any, field: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise LicenseValidationError(f"{field} 必须是字符串")
    text = value.strip()
    if not text:
        raise LicenseValidationError(f"{field} 不能为空")
    if len(text) > max_len:
        raise LicenseValidationError(f"{field} 长度不能超过 {max_len}")
    return text


def verify_snapshot_offline(token: str, public_key_provider: PublicKeyProvider) -> SnapshotResult:
    """便于客户端复用的别名。"""
    return verify_revocation_snapshot(token, public_key_provider)


__all__ = [
    "LicenseService",
    "LicenseError",
    "LicenseValidationError",
    "LicenseNotFoundError",
    "LicenseAlreadyRevokedError",
    "LicenseServiceUnavailable",
    "PublicKeyProvider",
    "hash_device_fingerprint",
    "verify_revocation_snapshot",
    "verify_snapshot_offline",
]
