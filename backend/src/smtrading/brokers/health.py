"""Health of a paper/demo broker link (Alpaca paper, eToro demo, Trading 212 demo).

The real-money links have the LiveMonitor (`live.unreachable`); the others are watched by
what already talks to them: BrokerSync (every pass, for the links with open orders) and
`reconcile_all` (every enabled link, every 5 minutes). An observation is a broker answer
(success), an unanswered call (`unreachable`: a network failure or a gateway 502/503/504)
or an answered failure (401/403, another HTTP error, a bug: status `error`, `last_error`,
nothing counted).

Same rule as the LiveMonitor, at its cadence: an unanswered call counts one failure in
`health_failures`, at most one every FAILURE_EVERY (so the alert means some minutes
without an answer, never a few fast passes); a success resets it (status `ok`). At the
HEALTH_FAILURES_ALERT-th failure one Telegram message (category `system`) says the link
does not answer, and at the first success after it one says it answers again. The outage
is kept on the link (`outage_since` at its first unanswered call, `outage_until` at the
first answer after it): BrokerSync refuses a never-sent market order that waited through
more than MATCH_WINDOW of it. Everything lives on the link row (locked FOR NO KEY UPDATE),
so the two writers and a restart never send a message twice.
"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.brokers.live.monitor import HEALTH_FAILURES_ALERT, UNREACHABLE_SUMMARY
from smtrading.portfolios.locks import lock_broker_link
from smtrading.telegram.outbox import enqueue

FAILURE_EVERY = timedelta(seconds=60)


def unreachable_text(name: str, error: str) -> str:
    return (f"🟠 Collegamento «{name}»: {UNREACHABLE_SUMMARY} ({error[:200]}). "
            "Gli ordini restano in attesa e vengono verificati quando risponde.")


def recovered_text(name: str) -> str:
    return f"🟢 Collegamento «{name}»: broker di nuovo raggiungibile."


async def record_link_health(session: AsyncSession, link_id: UUID, error: str | None, *,
                             now: datetime, unreachable: bool = True) -> None:
    """One observation of a paper/demo link at `now`: `error` None for a broker answer,
    else unanswered (`unreachable`) or answered with a failure. Real-money links are left
    alone (the LiveMonitor's). The caller commits."""
    link = await lock_broker_link(session, link_id)
    if link is None or link.live_state is not None:
        return
    failures = link.health_failures or 0
    if error is None:
        if failures >= HEALTH_FAILURES_ALERT:
            await enqueue(session, source="system", category="system", level="info",
                          text=recovered_text(link.name))
        if link.outage_since is not None and link.outage_until is None:
            link.outage_until = now
        link.health_failures, link.status, link.last_error = 0, "ok", None
        return
    link.status, link.last_error = "error", error[:500]
    if not unreachable:
        return  # the broker answered: not an outage
    if link.outage_since is None or link.outage_until is not None:
        link.outage_since, link.outage_until = now, None  # a new outage
    # at most one failure every FAILURE_EVERY since the outage began
    if failures >= 1 + int((now - link.outage_since) / FAILURE_EVERY):
        return
    link.health_failures = failures + 1
    if link.health_failures == HEALTH_FAILURES_ALERT:
        await enqueue(session, source="system", category="system", level="warning",
                      text=unreachable_text(link.name, error))


def outage_wait(outage_since: datetime | None, outage_until: datetime | None,
                created_at: datetime, now: datetime) -> tuple[datetime, datetime] | None:
    """The part of an order's life (from `created_at` to `now`) spent in the link's last
    outage, as (start, end), or None when it did not overlap it."""
    if outage_since is None:
        return None
    start, end = max(created_at, outage_since), min(outage_until or now, now)
    return (start, end) if end > start else None
