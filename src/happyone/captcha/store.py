"""人机验证持久层（SQLite）。

只做数据读写，不含判定逻辑。所有写操作都依赖单条 SQL 的原子性，
避免并发下「一题多票」「一票多次」。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import deps

_CHALLENGE_COLUMNS = (
    "id, payload, created_at, expires_at, attempts, solved_at, used"
)
_TICKET_COLUMNS = "nonce, purpose, issued_at, expires_at, ip_prefix, sig, used, used_at"


@dataclass(frozen=True)
class ChallengeRecord:
    id: str
    payload: str
    created_at: float
    expires_at: float
    attempts: int
    solved_at: float | None
    used: int


@dataclass(frozen=True)
class TicketRecord:
    nonce: str
    purpose: str
    issued_at: float
    expires_at: float
    ip_prefix: str | None
    sig: str
    used: int
    used_at: float | None


def insert_challenge(
    challenge_id: str, payload: str, created_at: float, expires_at: float, kind: str = "sequence"
) -> None:
    with deps.connection() as conn:
        conn.execute(
            "INSERT INTO captcha_challenge (id, kind, payload, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (challenge_id, kind, payload, created_at, expires_at),
        )
        deps.commit(conn)


def get_challenge(challenge_id: str) -> ChallengeRecord | None:
    with deps.connection() as conn:
        row = conn.execute(
            f"SELECT {_CHALLENGE_COLUMNS} FROM captcha_challenge WHERE id = ?",
            (challenge_id,),
        ).fetchone()
    if row is None:
        return None
    return ChallengeRecord(
        id=row[0],
        payload=row[1],
        created_at=row[2],
        expires_at=row[3],
        attempts=row[4],
        solved_at=row[5],
        used=row[6],
    )


def bump_attempts(challenge_id: str) -> int:
    """原子自增失败次数并返回最新值。"""
    with deps.connection() as conn:
        row = conn.execute(
            "UPDATE captcha_challenge SET attempts = attempts + 1 "
            "WHERE id = ? RETURNING attempts",
            (challenge_id,),
        ).fetchone()
        deps.commit(conn)
    return int(row[0]) if row else 0


def mark_solved(challenge_id: str, now: float) -> bool:
    """把挑战标记为已消耗；返回 False 表示已被别的请求抢先消耗。"""
    with deps.connection() as conn:
        cursor = conn.execute(
            "UPDATE captcha_challenge SET used = 1, solved_at = ? "
            "WHERE id = ? AND used = 0",
            (now, challenge_id),
        )
        deps.commit(conn)
    return cursor.rowcount == 1


def purge_expired(now: float, grace: float = 3600.0) -> None:
    with deps.connection() as conn:
        conn.execute(
            "DELETE FROM captcha_challenge WHERE expires_at < ?", (now - grace,)
        )
        conn.execute(
            "DELETE FROM captcha_ticket WHERE expires_at < ?", (now - grace,)
        )
        deps.commit(conn)


def insert_ticket(
    nonce: str,
    purpose: str,
    issued_at: float,
    expires_at: float,
    ip_prefix: str | None,
    sig: str,
) -> None:
    with deps.connection() as conn:
        conn.execute(
            "INSERT INTO captcha_ticket (nonce, purpose, issued_at, expires_at, ip_prefix, sig) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (nonce, purpose, issued_at, expires_at, ip_prefix, sig),
        )
        deps.commit(conn)


def get_ticket(nonce: str) -> TicketRecord | None:
    with deps.connection() as conn:
        row = conn.execute(
            f"SELECT {_TICKET_COLUMNS} FROM captcha_ticket WHERE nonce = ?",
            (nonce,),
        ).fetchone()
    if row is None:
        return None
    return TicketRecord(
        nonce=row[0],
        purpose=row[1],
        issued_at=row[2],
        expires_at=row[3],
        ip_prefix=row[4],
        sig=row[5],
        used=row[6],
        used_at=row[7],
    )


def consume_ticket(nonce: str, now: float) -> bool:
    """单次消耗：返回 False 表示票据已被使用过。"""
    with deps.connection() as conn:
        cursor = conn.execute(
            "UPDATE captcha_ticket SET used = 1, used_at = ? "
            "WHERE nonce = ? AND used = 0",
            (now, nonce),
        )
        deps.commit(conn)
    return cursor.rowcount == 1
