"""密码学与令牌工具。

- 密码：scrypt（n=2^15, r=8, p=1），格式自描述，便于后续调参。
- 令牌：随机不可预测串，数据库中只保存 sha256 摘要。
- OIDC：Ed25519 密钥（PKCS8 PEM），compact JWS（alg=EdDSA），kid 由公钥指纹派生。

本模块不依赖任何业务线；许可证线使用自己的密钥与算法参数。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature

_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    """生成口令摘要，格式 scrypt$n$r$p$salt_b64$hash_b64。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return "$".join(
        [
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(stored: str, password: str) -> bool:
    """常量时间校验口令；格式异常一律视为不匹配。"""
    try:
        scheme, n_raw, r_raw, p_raw, salt_raw, hash_raw = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_raw)
        expected = base64.b64decode(hash_raw)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_raw),
            r=int(r_raw),
            p=int(p_raw),
            dklen=len(expected),
            maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def random_token(prefix: str, nbytes: int = 32) -> str:
    """生成前缀明示类型的随机令牌，例如 ho_sess_xxx。"""
    return f"{prefix}{secrets.token_urlsafe(nbytes)}"


def token_fingerprint(token: str) -> str:
    """令牌入库前的摘要。"""
    return sha256_hex(token)


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class Ed25519Signer:
    """OIDC id_token 签名器，同时提供 JWKS 公钥。"""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key
        self._public_key: Ed25519PublicKey = private_key.public_key()
        self.public_bytes = self._public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.kid = hashlib.sha256(self.public_bytes).hexdigest()[:16]

    @classmethod
    def load_or_create(cls, path: Path) -> "Ed25519Signer":
        """从 PEM 加载；不存在则生成并落盘（权限尽力设为 0600）。"""
        if path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise RuntimeError(f"OIDC 私钥类型不匹配：{path}")
            return cls(key)

        path.parent.mkdir(parents=True, exist_ok=True)
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        path.write_bytes(pem)
        try:
            path.chmod(0o600)
        except OSError:
            # Windows 上 chmod 语义有限，忽略即可；不得因此阻断启动
            pass
        return cls(key)

    def sign_jwt(self, claims: dict[str, Any]) -> str:
        header = {"alg": "EdDSA", "typ": "JWT", "kid": self.kid}
        segments = [
            b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            b64url_encode(json.dumps(claims, separators=(",", ":")).encode("utf-8")),
        ]
        signing_input = ".".join(segments).encode("ascii")
        signature = self._private_key.sign(signing_input)
        segments.append(b64url_encode(signature))
        return ".".join(segments)

    def verify_jwt(self, token: str) -> dict[str, Any]:
        """校验签名并返回声明；仅用于自检与测试。"""
        try:
            header_raw, payload_raw, signature_raw = token.split(".")
            signing_input = f"{header_raw}.{payload_raw}".encode("ascii")
            self._public_key.verify(
                b64url_decode(signature_raw), signing_input
            )
        except (ValueError, InvalidSignature) as exc:
            raise ValueError("JWT 签名校验失败") from exc
        header = json.loads(b64url_decode(header_raw))
        if header.get("alg") != "EdDSA":
            raise ValueError("不支持的 JWT 算法")
        return json.loads(b64url_decode(payload_raw))

    def jwks(self) -> dict[str, Any]:
        return {
            "keys": [
                {
                    "kty": "OKP",
                    "crv": "Ed25519",
                    "x": b64url_encode(self.public_bytes),
                    "kid": self.kid,
                    "use": "sig",
                    "alg": "EdDSA",
                }
            ]
        }
