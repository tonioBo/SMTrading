"""The rate limit of anonymous failures in the activity log (rows with no user whose
outcome is not ok: requests without a session, failed logins, unknown routes).

Per (IP, action) and per minute (Redis HINCRBY on `ua:anon:{minute}:{ip}:{action}`): the
first ANON_LIMIT rows are written in full, the others are only counted. A window that
went over the limit is listed in `ua:anon:over`; once the minute is over,
`flush_suppressed` writes ONE row for it (summary `{"_soppressi": n}`) and drops the
window. It runs on every throttled request (closed windows only) and every minute in
the worker's job scheduler. Rows of a logged-in user and successful requests are never
limited. Redis unreachable: the row is written (the log fails open)."""

import logging
import time
import uuid

from smtrading.useractivity.models import UserActivity

log = logging.getLogger(__name__)

ANON_LIMIT = 20
WINDOW_S = 60
KEY_TTL_S = 30 * 60  # a window no flush reached is dropped eventually
_PREFIX = "ua:anon:"
OVER_SET = "ua:anon:over"


def is_limited(row: UserActivity) -> bool:
    return row.user_id is None and row.outcome != "ok" and not row.provisional


def _minute(now: float) -> int:
    return int(now // WINDOW_S)


def _key(minute: int, row: UserActivity) -> str:
    return f"{_PREFIX}{minute}:{row.ip or '-'}:{row.action}"


async def admit(redis, row: UserActivity, now: float | None = None) -> bool:
    """Count this anonymous failure; True when it is within the limit (write it)."""
    key = _key(_minute(time.time() if now is None else now), row)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hincrby(key, "n", 1)
        pipe.expire(key, KEY_TTL_S, nx=True)
        count, _ = await pipe.execute()
    count = int(count)
    if count <= ANON_LIMIT:
        return True
    if count == ANON_LIMIT + 1:  # what the coalesced row will say
        await redis.hset(key, mapping={
            "method": row.method, "route": row.route, "outcome": row.outcome,
            "status": str(row.status or ""), "error_code": row.error_code or "",
            "ip": row.ip or "", "action": row.action,
        })
        await redis.sadd(OVER_SET, key)
    return False


def _text(raw: dict, name: str) -> str:
    value = raw.get(name.encode(), b"")
    return value.decode() if isinstance(value, bytes) else str(value)


async def flush_suppressed(sessionmaker, redis, now: float | None = None) -> int:
    """One coalesced row per closed window that went over the limit; how many."""
    current = _minute(time.time() if now is None else now)
    rows = []
    for member in await redis.smembers(OVER_SET):
        key = member.decode() if isinstance(member, bytes) else member
        try:
            minute = int(key[len(_PREFIX):].split(":", 1)[0])
        except ValueError:
            await redis.srem(OVER_SET, key)
            continue
        if minute >= current:
            continue
        async with redis.pipeline(transaction=True) as pipe:
            pipe.hgetall(key)
            pipe.delete(key)
            pipe.srem(OVER_SET, key)
            raw, _, _ = await pipe.execute()
        suppressed = int(_text(raw, "n") or 0) - ANON_LIMIT
        if suppressed <= 0:
            continue
        action = _text(raw, "action")
        status = _text(raw, "status")
        rows.append(UserActivity(
            request_id=uuid.uuid4(), action=action, category=action.split(".", 1)[0],
            method=_text(raw, "method"), route=_text(raw, "route"), target={},
            outcome=_text(raw, "outcome"), status=int(status) if status else None,
            error_code=_text(raw, "error_code") or None, ip=_text(raw, "ip") or None,
            summary={"_soppressi": suppressed},
        ))
    if rows:
        async with sessionmaker() as session:
            session.add_all(rows)
            await session.commit()
    return len(rows)
