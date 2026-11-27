"""Health of a paper/demo broker link (Alpaca paper, eToro demo, Trading 212 demo).

The real-money links have the LiveMonitor (`live.unreachable`); the others are watched by
what already talks to them: BrokerSync (every pass, for the links with open orders: a
connection failure, or a broker answer) and `reconcile_all` (every enabled link, every 5
minutes: any failure, or a success). Same rule as the LiveMonitor: a failure increments
`health_failures` (status `error`, `last_error`), a success resets it (status `ok`); at
the HEALTH_FAILURES_ALERT-th consecutive failure one Telegram message (category `system`)
says the link does not answer, and at the first success after it one says it answers
again. The counter lives on the link row (locked FOR NO KEY UPDATE), so the two writers
and a restart never send it twice.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.brokers.live.monitor import HEALTH_FAILURES_ALERT, UNREACHABLE_SUMMARY
from smtrading.portfolios.locks import lock_broker_link
from smtrading.telegram.outbox import enqueue


def unreachable_text(name: str, error: str) -> str:
    return (f"🟠 Collegamento «{name}»: {UNREACHABLE_SUMMARY} ({error[:200]}). "
            "Gli ordini restano in attesa e vengono verificati quando risponde.")


def recovered_text(name: str) -> str:
    return f"🟢 Collegamento «{name}»: broker di nuovo raggiungibile."


async def record_link_health(session: AsyncSession, link_id: UUID, error: str | None) -> None:
    """One observation of a paper/demo link: `error` None for a broker answer. Real-money
    links are left alone (the LiveMonitor's). The caller commits."""
    link = await lock_broker_link(session, link_id)
    if link is None or link.live_state is not None:
        return
    failures = link.health_failures or 0
    if error is None:
        if failures >= HEALTH_FAILURES_ALERT:
            await enqueue(session, source="system", category="system", level="info",
                          text=recovered_text(link.name))
        link.health_failures, link.status, link.last_error = 0, "ok", None
        return
    link.health_failures = failures + 1
    link.status, link.last_error = "error", error[:500]
    if link.health_failures == HEALTH_FAILURES_ALERT:
        await enqueue(session, source="system", category="system", level="warning",
                      text=unreachable_text(link.name, error))
