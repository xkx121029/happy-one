"""时间工具：统一 UTC、统一 ISO 8601 文本格式。

数据库中的时间一律存 `YYYY-MM-DDTHH:MM:SSZ`，可直接做字符串比较与排序。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime(_FORMAT)


def now_iso() -> str:
    return to_iso(now_utc())


def iso_after(seconds: int) -> str:
    return to_iso(now_utc() + timedelta(seconds=seconds))


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, _FORMAT).replace(tzinfo=timezone.utc)


def is_expired(value: str) -> bool:
    return parse_iso(value) <= now_utc()
