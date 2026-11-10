"""审计线业务逻辑。

审计写入失败只记日志、不向上抛：审计不能反向阻断登录等核心流程
（见 docs/ARCHITECTURE.md 第 4 节）。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from happyone.audit.models import AuditEvent, decode_detail
from happyone.audit.repository import AuditRepository
from happyone.core import errors
from happyone.core.timeutil import now_iso
from happyone.db import Database

_MAX_PAGE_SIZE = 200
_log = logging.getLogger("happyone.audit")


def _to_event(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        id=int(row["id"]),
        ts=row["ts"],
        actor_user_id=row["actor_user_id"],
        action=row["action"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        outcome=row["outcome"],
        request_id=row["request_id"],
        ip=row["ip"],
        detail=decode_detail(row["detail"]),
    )


class AuditService:
    def __init__(self, db: Database) -> None:
        self._repo = AuditRepository(db)

    def record(
        self,
        *,
        action: str,
        actor_user_id: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        outcome: str = "success",
        request_id: str | None = None,
        ip: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        try:
            self._repo.insert(
                ts=now_iso(),
                actor_user_id=actor_user_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                outcome=outcome,
                request_id=request_id,
                ip=ip,
                detail=detail,
            )
        except Exception:  # noqa: BLE001 - 审计失败不得影响主流程
            _log.error("审计写入失败：action=%s", action, exc_info=True)

    def list_events(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        actor_id: str | None = None,
        action: str | None = None,
        outcome: str | None = None,
        from_ts: str | None = None,
        to_ts: str | None = None,
    ) -> tuple[list[AuditEvent], str | None]:
        limit = max(1, min(limit, _MAX_PAGE_SIZE))
        before_id = _parse_cursor(cursor)
        rows = self._repo.list_events(
            limit=limit + 1,
            before_id=before_id,
            actor_id=actor_id,
            action=action,
            outcome=outcome,
            from_ts=from_ts,
            to_ts=to_ts,
        )
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = str(rows[-1]["id"])
        return [_to_event(row) for row in rows], next_cursor

    def get_event(self, event_id: int) -> AuditEvent:
        row = self._repo.get_event(event_id)
        if row is None:
            raise errors.not_found("审计事件不存在")
        return _to_event(row)


def _parse_cursor(cursor: str | None) -> int | None:
    if not cursor:
        return None
    try:
        return int(cursor)
    except (TypeError, ValueError) as exc:
        raise errors.validation("分页游标不合法") from exc
