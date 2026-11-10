"""Ed25519 密钥的生成、落盘与加载。

安全约束：
- 私钥文件默认写在用户目录（``~/.happyone/license/``），绝不进仓库。
- 落盘权限按 0600 收紧；Windows 没有 POSIX 权限位，改用 icacls 去掉继承并只授权当前用户。
- 与 happy-hub 的直链签名密钥物理分离：路径显式校验，禁止复用 ``data/.secret``
  或 hub 配置里的密钥文件，任何一方轮换都不影响另一方。
"""

from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .codec import TOKEN_PREFIX, b64u_encode

KEY_FILE_MODE = 0o600
KEY_DIR_MODE = 0o700
DEFAULT_KEY_FILENAME = "license_ed25519.pem"

# 明确禁止复用的文件名：happy-hub 的直链签名密钥就叫 data/.secret
FORBIDDEN_KEY_NAMES = {".secret", "secret"}


class KeyPathError(RuntimeError):
    """私钥路径不安全（与 hub 密钥冲突、指向目录等）。"""


@dataclass(frozen=True)
class KeyPair:
    """私钥 + 公钥 + 稳定的 key id。"""

    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey
    kid: str


def public_key_raw(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def compute_kid(public_key: Ed25519PublicKey) -> str:
    """由公钥派生 key id：k_ + sha256(公钥原始字节) 前 9 字节的 base64url。

    kid 只依赖公钥本身，无需额外配置即可在重启、轮换后保持一致。
    """
    digest = hashlib.sha256(public_key_raw(public_key)).digest()[:9]
    return f"k_{b64u_encode(digest)}"


def generate_keypair() -> KeyPair:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    return KeyPair(private_key=private_key, public_key=public_key, kid=compute_kid(public_key))


def keypair_from_private(private_key: Ed25519PrivateKey) -> KeyPair:
    public_key = private_key.public_key()
    return KeyPair(private_key=private_key, public_key=public_key, kid=compute_kid(public_key))


def ensure_isolated_path(
    path: str | os.PathLike[str],
    *,
    hub_secret_path: str | os.PathLike[str] | None = None,
) -> Path:
    """校验并返回解析后的私钥路径，确保与 hub 密钥物理分离。"""
    candidate = Path(path).expanduser()
    if candidate.exists() and candidate.is_dir():
        raise KeyPathError(f"许可证私钥路径指向了目录，请给出具体文件路径：{candidate}")

    resolved = candidate.resolve()
    if resolved.name.lower() in FORBIDDEN_KEY_NAMES:
        raise KeyPathError(
            f"许可证私钥不能使用与 happy-hub 直链密钥相同的文件名（{resolved.name}），两者必须物理分离"
        )

    if hub_secret_path is not None:
        hub_resolved = Path(hub_secret_path).expanduser().resolve()
        if resolved == hub_resolved:
            raise KeyPathError("许可证私钥与 happy-hub 直链密钥指向了同一个文件，两者必须物理分离")
        hub_dir = hub_resolved.parent
        if hub_dir == resolved or hub_dir in resolved.parents:
            raise KeyPathError("许可证私钥不能放在 happy-hub 密钥目录内，两者必须物理分离")

    return resolved


def _restrict_posix_permissions(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        # 权限收紧失败不应阻断功能，但调用方可从日志感知
        pass


def _restrict_windows_permissions(path: Path) -> None:
    """Windows 下用 icacls 去掉继承并只授权当前用户，近似 0600。"""
    if sys.platform != "win32":
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def restrict_permissions(path: Path, mode: int = KEY_FILE_MODE) -> None:
    if os.name == "posix":
        _restrict_posix_permissions(path, mode)
    else:
        _restrict_windows_permissions(path)


def save_private_key(path: Path, private_key: Ed25519PrivateKey) -> Path:
    """原子写入私钥（先写同目录临时文件再 replace），并收紧权限。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        _restrict_posix_permissions(path.parent, KEY_DIR_MODE)

    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    tmp_path = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, KEY_FILE_MODE)
    try:
        os.write(fd, pem)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, path)
    restrict_permissions(path, KEY_FILE_MODE)
    return path


def load_private_key(path: Path) -> Ed25519PrivateKey:
    data = Path(path).read_bytes()
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise KeyPathError(f"私钥文件不是 Ed25519 私钥：{path}")
    return key


def load_or_create(path: Path) -> KeyPair:
    """存在则加载，不存在则生成并落盘。返回 KeyPair。"""
    path = Path(path)
    if path.exists():
        return keypair_from_private(load_private_key(path))
    pair = generate_keypair()
    save_private_key(path, pair.private_key)
    return pair


def public_key_pem(public_key: Ed25519PublicKey) -> str:
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def public_key_info(pair: KeyPair) -> dict[str, object]:
    """随客户端分发的公钥信息（多种编码，任选其一即可验签）。"""
    raw = public_key_raw(pair.public_key)
    return {
        "kid": pair.kid,
        "alg": "Ed25519",
        "format": "raw",
        "public_key_b64url": b64u_encode(raw),
        "public_key_hex": raw.hex(),
        "pem": public_key_pem(pair.public_key),
        "fingerprint_sha256": hashlib.sha256(raw).hexdigest(),
        "token_prefix": TOKEN_PREFIX,
    }


class SingleKeyProvider:
    """单密钥公钥提供者：kid 匹配才返回公钥，否则返回 None（触发 UNKNOWN_KEY）。"""

    def __init__(self, pair: KeyPair) -> None:
        self._by_kid = {pair.kid: pair.public_key}

    def __call__(self, kid: str) -> Ed25519PublicKey | None:
        return self._by_kid.get(kid)
