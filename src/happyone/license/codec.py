"""许可证令牌的紧凑签名格式（自研格式，非私有二进制）。

格式::

    hlic1.<base64url(payload_json)>.<base64url(ed25519_signature)>

- payload_json：UTF-8、键排序、无多余空白的 JSON（canonical JSON）。
- 签名输入（signing input）：ASCII 字节 ``"hlic1." + base64url(payload_json)``，
  前缀同时充当域分隔符，防止把其它用途的签名串当作许可证复用。
- base64url 一律去掉 ``=`` 填充（RFC 7515 的 base64url 无填充形式）。
- 签名算法：Ed25519（``cryptography`` 库即可验签，无需任何私有实现）。

许可证令牌与吊销快照共用该格式，靠 payload 中的 ``typ`` 字段区分。
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

TOKEN_PREFIX = "hlic1"
_SEPARATOR = "."


class TokenFormatError(ValueError):
    """令牌结构不合法（前缀、分段、base64url、JSON 任一环节出错）。"""


class SignatureError(ValueError):
    """签名验证失败。"""


def canonical_json(payload: Mapping[str, Any]) -> bytes:
    """生成稳定的 JSON 字节串：键排序、紧凑分隔符、UTF-8 直出。"""
    try:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:  # 例如 payload 里混入了不可序列化对象
        raise TokenFormatError(f"许可证内容无法序列化为 JSON：{exc}") from exc
    return text.encode("utf-8")


def b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64u_decode(text: str) -> bytes:
    if not isinstance(text, str) or not text:
        raise TokenFormatError("令牌分段为空")
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (binascii.Error, ValueError) as exc:
        raise TokenFormatError("令牌中的 base64url 内容无法解码") from exc


def sign_payload(payload: Mapping[str, Any], private_key: Ed25519PrivateKey) -> str:
    """对 payload 签名，返回完整令牌字符串。"""
    body = b64u_encode(canonical_json(payload))
    signing_input = f"{TOKEN_PREFIX}{_SEPARATOR}{body}".encode("ascii")
    signature = private_key.sign(signing_input)
    return f"{TOKEN_PREFIX}{_SEPARATOR}{body}{_SEPARATOR}{b64u_encode(signature)}"


def split_token(token: str) -> tuple[bytes, bytes, bytes]:
    """拆出 (payload 字节, 签名字节, 签名输入字节)。

    签名输入直接由令牌原文拼出，保证「对原始字节验签」，
    任何对 payload 的改动都会导致验签失败，而不是被重新序列化掩盖。
    """
    if not isinstance(token, str):
        raise TokenFormatError("许可证必须是字符串")
    parts = token.strip().split(_SEPARATOR)
    if len(parts) != 3:
        raise TokenFormatError("许可证格式不正确：应由三段组成，以 . 分隔")
    prefix, body, signature = parts
    if prefix != TOKEN_PREFIX:
        raise TokenFormatError(f"许可证格式不正确：缺少 {TOKEN_PREFIX} 前缀")
    if not body or not signature:
        raise TokenFormatError("许可证格式不正确：payload 或签名为空")
    payload_bytes = b64u_decode(body)
    signature_bytes = b64u_decode(signature)
    signing_input = f"{prefix}{_SEPARATOR}{body}".encode("ascii")
    return payload_bytes, signature_bytes, signing_input


def decode_payload(token: str) -> tuple[dict[str, Any], bytes, bytes]:
    """解析出 payload 字典（尚未验签），并返回签名字节与签名输入。"""
    payload_bytes, signature_bytes, signing_input = split_token(token)
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TokenFormatError("许可证内容不是合法的 UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise TokenFormatError("许可证内容必须是 JSON 对象")
    return payload, signature_bytes, signing_input


def verify_signature(signing_input: bytes, signature_bytes: bytes, public_key: Ed25519PublicKey) -> None:
    """对签名输入做 Ed25519 验签，失败抛 SignatureError。"""
    try:
        public_key.verify(signature_bytes, signing_input)
    except InvalidSignature as exc:
        raise SignatureError("许可证签名验证失败，内容可能被篡改") from exc


def verify_token(token: str, public_key: Ed25519PublicKey) -> dict[str, Any]:
    """验签成功返回 payload 字典，失败抛 TokenFormatError / SignatureError。"""
    payload, signature_bytes, signing_input = decode_payload(token)
    verify_signature(signing_input, signature_bytes, public_key)
    return payload

