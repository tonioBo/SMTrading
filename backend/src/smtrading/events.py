"""Domain events (Redis pub/sub channel `events`) and persisted notifications.

Redis side effects of a transaction (events, track/untrack) are queued on the session
with `queue_*` and executed only after commit with `dispatch_post_commit`; on rollback
the caller drops them with `discard_post_commit`. Notifications are DB rows and stay
inside the transaction. Both also record the fact in the activity feed
(`activity.service.record_activity`), in the same transaction.
"""

import json
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.activity.mapping import activity_for_event, activity_for_notification
from smtrading.activity.service import clear_activity_keys, jsonb_safe, record_activity
from smtrading.marketdata.streaming import track, untrack
from smtrading.risk.models import Notification

log = logging.getLogger("smtrading.events")

EVENTS_CHANNEL = "events"
POST_COMMIT_KEY = "smt_post_commit"


def json_safe(obj: Any) -> Any:
    """Decimal -> str, UUID -> str, datetime/date -> ISO 8601 (datetimes in UTC)."""
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (Decimal, UUID)):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.astimezone(UTC).isoformat() if obj.tzinfo else obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [json_safe(v) for v in obj]
    return obj


async def publish_event(redis, type: str, payload: dict) -> None:
    if redis is None:
        return
    message = {"type": type, "ts": datetime.now(UTC), **payload}
    await redis.publish(EVENTS_CHANNEL, json.dumps(json_safe(message)))


async def notify(
    session: AsyncSession,
    *,
    portfolio_id: UUID | None,
    kind: str,
    payload: dict,
    agent_id: str | None = None,
) -> Notification:
    n = Notification(
        portfolio_id=portfolio_id, agent_id=agent_id, kind=kind, payload=jsonb_safe(payload)
    )
    session.add(n)
    record_activity(
        session, activity_for_notification(kind, payload),
        portfolio_id=portfolio_id, agent_id=agent_id,
    )
    await session.flush()
    # the web app refreshes on it (alerts, broker, risk); the payload stays in the table
    queue_event(session, "notification",
                {"kind": kind, "portfolio_id": portfolio_id, "agent_id": agent_id})
    return n


def _queue(session: AsyncSession) -> list[tuple]:
    return session.info.setdefault(POST_COMMIT_KEY, [])


def queue_event(session: AsyncSession, type: str, payload: dict) -> None:
    _queue(session).append(("event", type, payload))
    draft = activity_for_event(type, payload)
    if draft is not None:
        record_activity(
            session, draft,
            portfolio_id=payload.get("portfolio_id"), agent_id=payload.get("agent_id"),
        )


def queue_track(session: AsyncSession, symbol: str, source: str) -> None:
    _queue(session).append(("track", symbol, source))


def queue_untrack(session: AsyncSession, symbol: str, source: str) -> None:
    _queue(session).append(("untrack", symbol, source))


def queue_redis_delete(session: AsyncSession, key: str) -> None:
    """After commit, delete a Redis key (a cooldown the committed change ends)."""
    _queue(session).append(("delete", key))


def queue_live_trip(session: AsyncSession, link_id, exclude, cause: str,
                    details: dict) -> None:
    """After commit, put the other portfolios of a real-money link in reduce-only (a fuse
    trip, risk.live_checks) in a transaction of their own that locks the link first
    (R8.9): the caller holds a portfolio lock and must not lock another portfolio."""
    _queue(session).append(("live_trip", link_id, exclude, cause, details))


def discard_post_commit(session: AsyncSession) -> None:
    session.info.pop(POST_COMMIT_KEY, None)
    clear_activity_keys(session)


async def dispatch_post_commit(session: AsyncSession, redis) -> int:
    """Run (and clear) the queued effects after commit; returns how many succeeded.

    Errors are logged and skipped. With redis None the queue is dropped, except the
    `live_trip` effects (database work, run first in their own transaction on `session`).
    """
    effects = session.info.pop(POST_COMMIT_KEY, [])
    clear_activity_keys(session)
    trips = [e for e in effects if e[0] == "live_trip"]
    effects = [e for e in effects if e[0] != "live_trip"]
    if trips:
        if session.in_transaction():  # not after a commit: left for the next dispatch
            _queue(session).extend(trips)
        else:
            await _run_live_trips(session, trips)
            effects += session.info.pop(POST_COMMIT_KEY, [])
            clear_activity_keys(session)
    if redis is None:
        return 0
    done = 0
    for effect in effects:
        try:
            kind, *args = effect
            if kind == "event":
                await publish_event(redis, args[0], args[1])
            elif kind == "track":
                await track(redis, [args[0]], source=args[1])
            elif kind == "untrack":
                await untrack(redis, [args[0]], source=args[1])
            elif kind == "delete":
                await redis.delete(args[0])
            else:
                raise ValueError(f"unknown post-commit effect {kind!r}")
            done += 1
        except Exception:
            log.exception("post-commit effect failed: %r", effect)
    return done


async def _run_live_trips(session: AsyncSession, trips: list[tuple]) -> None:
    from smtrading.brokers.live.protect import trip_link_portfolios

    for _, link_id, exclude, cause, details in trips:
        try:
            await trip_link_portfolios(session, link_id, exclude=exclude, cause=cause,
                                       details=details)
            await session.commit()
        except Exception:
            await session.rollback()
            log.exception("live trip of link %s failed", link_id)
