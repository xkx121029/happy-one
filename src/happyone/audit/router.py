"""审计线 HTTP 路由：只读查询。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from happyone.audit.service import AuditService
from happyone.deps import require_admin, get_audit

router = APIRouter(prefix="/api/v1", tags=["audit"])


@router.get("/audit/events")
def list_events(
    _: Any = Depends(require_admin),
    audit: AuditService = Depends(get_audit),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    actor_id: str | None = None,
    action: str | None = None,
    outcome: str | None = None,
    from_ts: str | None = Query(default=None, alias="from"),
    to_ts: str | None = Query(default=None, alias="to"),
) -> dict[str, Any]:
    events, next_cursor = audit.list_events(
        limit=limit,
        cursor=cursor,
        actor_id=actor_id,
        action=action,
        outcome=outcome,
        from_ts=from_ts,
        to_ts=to_ts,
    )
    return {
        "items": [event.to_dict() for event in events],
        "next_cursor": next_cursor,
    }


@router.get("/audit/events/{event_id}")
def get_event(
    event_id: int,
    _: Any = Depends(require_admin),
    audit: AuditService = Depends(get_audit),
) -> dict[str, Any]:
    return audit.get_event(event_id).to_dict()
