"""审计线数据结构。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AuditEvent:
    id: int
    ts: str
    actor_user_id: str | None
    action: str
    target_type: str | None
    target_id: str | None
    outcome: str
    request_id: str | None
    ip: str | None
    detail: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "actor_user_id": self.actor_user_id,
            "action": self.action,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "outcome": self.outcome,
            "request_id": self.request_id,
            "ip": self.ip,
            "detail": self.detail,
        }


def decode_detail(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {"_raw": raw}
    return value if isinstance(value, dict) else {"_value": value}
