"""审计线仓储。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from happyone.db import Database


class AuditRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def insert(
        self,
        *,
        ts: str,
        actor_user_id: str | None,
        action: str,
        target_type: str | None,
        target_id: str | None,
        outcome: str,
        request_id: str | None,
        ip: str | None,
        detail: dict[str, Any] | None,
    ) -> int:
        with self._db.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO audit_events (ts, actor_user_id, action, target_type,
                                          target_id, outcome, request_id, ip, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    actor_user_id,
                    action,
                    target_type,
                    target_id,
                    outcome,
                    request_id,
                    ip,
                    json.dumps(detail, ensure_ascii=False) if detail is not None else None,
                ),
            )
            return int(cursor.lastrowid or 0)

    def list_events(
        self,
        *,
        limit: int,
        before_id: int | None,
        actor_id: str | None,
        action: str | None,
        outcome: str | None,
        from_ts: str | None,
        to_ts: str | None,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM audit_events"
        clauses: list[str] = []
        params: list[Any] = []
        if before_id is not None:
            clauses.append("id < ?")
            params.append(before_id)
        if actor_id:
            clauses.append("actor_user_id = ?")
            params.append(actor_id)
        if action:
            clauses.append("action = ?")
            params.append(action)
        if outcome:
            clauses.append("outcome = ?")
            params.append(outcome)
        if from_ts:
            clauses.append("ts >= ?")
            params.append(from_ts)
        if to_ts:
            clauses.append("ts <= ?")
            params.append(to_ts)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self._db.query_all(sql, params)

    def get_event(self, event_id: int) -> sqlite3.Row | None:
        return self._db.query_one("SELECT * FROM audit_events WHERE id = ?", (event_id,))
