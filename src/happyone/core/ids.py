"""ID 生成。

所有实体 ID 带类型前缀，便于日志检索与人工排查；随机部分使用
secrets.token_hex，不依赖时间，避免可预测。
"""

from __future__ import annotations

import secrets

# 用户码去掉了易混淆字符（0/O、1/I/L 等）
_USER_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def new_id(prefix: str) -> str:
    """生成形如 usr_9f3c... 的 ID。"""
    return f"{prefix}_{secrets.token_hex(12)}"


def new_user_code(length: int = 8) -> str:
    """生成设备授权用的用户码，格式 XXXX-XXXX。"""
    raw = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(length))
    half = length // 2
    return f"{raw[:half]}-{raw[half:]}"


def normalize_user_code(value: str) -> str:
    """规范化用户码：去空格、转大写、兼容无连字符输入。"""
    compact = "".join(ch for ch in value.upper() if ch.isalnum())
    if len(compact) == 8:
        return f"{compact[:4]}-{compact[4:]}"
    return compact
