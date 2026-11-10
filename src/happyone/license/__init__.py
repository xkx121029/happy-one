"""Happy One 许可证模块。

对外契约（全项目统一）：
- ``happyone.license.router:router`` —— APIRouter，由骨架线挂载；
- ``happyone.license.service`` —— 纯逻辑（不 import fastapi），可单独单测；
- ``happyone.license.models`` —— 数据结构。

令牌格式：``hlic1.<base64url(payload_json)>.<base64url(ed25519_sig)>``，
签名输入为 ``"hlic1." + base64url(payload_json)`` 的 ASCII 字节，
使用 Ed25519 签名，任何持有公钥的客户端都可离线验签。

密钥与 happy-hub 的直链签名密钥物理分离，任一方轮换互不影响。
"""

from __future__ import annotations

from .codec import TOKEN_PREFIX, TokenFormatError, SignatureError, sign_payload, verify_token
from .keys import (
    KeyPair,
    KeyPathError,
    SingleKeyProvider,
    compute_kid,
    ensure_isolated_path,
    generate_keypair,
    load_or_create,
    public_key_info,
)
from .models import (
    DEFAULT_GRACE_DAYS,
    ErrorCode,
    IssueRequest,
    IssuedLicense,
    LicensePayload,
    LicenseRecord,
    RevocationEntry,
    SnapshotResult,
    VerifyResult,
)
from .service import (
    LicenseAlreadyRevokedError,
    LicenseError,
    LicenseNotFoundError,
    LicenseService,
    LicenseServiceUnavailable,
    LicenseValidationError,
    hash_device_fingerprint,
    verify_revocation_snapshot,
)

__all__ = [
    "TOKEN_PREFIX",
    "TokenFormatError",
    "SignatureError",
    "sign_payload",
    "verify_token",
    "KeyPair",
    "KeyPathError",
    "SingleKeyProvider",
    "compute_kid",
    "ensure_isolated_path",
    "generate_keypair",
    "load_or_create",
    "public_key_info",
    "DEFAULT_GRACE_DAYS",
    "ErrorCode",
    "IssueRequest",
    "IssuedLicense",
    "LicensePayload",
    "LicenseRecord",
    "RevocationEntry",
    "SnapshotResult",
    "VerifyResult",
    "LicenseAlreadyRevokedError",
    "LicenseError",
    "LicenseNotFoundError",
    "LicenseService",
    "LicenseServiceUnavailable",
    "LicenseValidationError",
    "hash_device_fingerprint",
    "verify_revocation_snapshot",
]
