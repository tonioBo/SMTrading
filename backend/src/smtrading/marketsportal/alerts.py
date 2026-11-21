"""The owner's personal price alerts (plan 30): «sopra / sotto X» on a catalog instrument
or a «Mercati oggi» symbol, sent ONCE to the owner's Telegram group when hit.

Completely apart from the agents' alerts (`alerts`, MCP `create_alert`): another table
(`portal_price_alerts`), never readable over MCP, never tracked by the stream.

`PersonalAlerts` (worker, behind the startup gate) checks the active alerts every
INTERVAL_S with prices it does not have to ask for one by one: a catalog symbol's
cached quote (`quote:<symbol>`, written by the stream and every quote request) when it
is at most QUOTE_MAX_AGE_S old, else the router's batch day changes (cached 5 minutes
per symbol); an overview symbol the «Mercati oggi» snapshot (cached 60 s / 5 min). The
alert row is locked and flipped to `triggered` in the same transaction that queues the
Telegram message, so a hit is announced once whatever the restarts.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.marketdata.cache import read_quote_meta
from smtrading.marketsportal.models import (
    ABOVE,
    ALERT_ACTIVE,
    ALERT_TRIGGERED,
    SOURCE_OVERVIEW,
    PortalPriceAlert,
)
from smtrading.marketsportal.overview import BY_CODE
from smtrading.telegram.outbox import enqueue
from smtrading.worker.jobstate import job_tick

log = logging.getLogger(__name__)

INTERVAL_S = 60
QUOTE_MAX_AGE_S = 120
CATEGORY = "price_alert"
MAX_ACTIVE = 200


def hit(direction: str, threshold: Decimal, price: Decimal) -> bool:
    return price >= threshold if direction == ABOVE else price <= threshold


def _fmt(v: Decimal) -> str:
    q = v.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN).normalize()
    return f"{q:,f}".replace(",", "_").replace(".", ",").replace("_", ".")


def label(a: PortalPriceAlert) -> str:
    if a.source == SOURCE_OVERVIEW and a.symbol in BY_CODE:
        return BY_CODE[a.symbol].label
    return a.symbol


def text(a: PortalPriceAlert, price: Decimal) -> str:
    where = "sopra" if a.direction == ABOVE else "sotto"
    arrow = "📈" if a.direction == ABOVE else "📉"
    note = f"\nNota: {a.note}" if a.note else ""
    return (f"{arrow} Avviso di prezzo: {label(a)} {where} {_fmt(a.threshold)} "
            f"(ora {_fmt(price)}){note}")


class PersonalAlerts:
    def __init__(self, sessionmaker: async_sessionmaker, router, redis, overview=None, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 interval_s: float = INTERVAL_S) -> None:
        self._sm = sessionmaker
        self._router = router
        self._redis = redis
        self._overview = overview  # OverviewService | None
        self._clock = clock
        self._interval_s = interval_s

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
                await job_tick()
            except Exception:
                log.warning("personal alerts failed; retrying in %ss", self._interval_s,
                            exc_info=True)
            await asyncio.sleep(self._interval_s)

    async def _catalog_prices(self, symbols: list[str]) -> dict[str, Decimal]:
        now = self._clock().astimezone(UTC)
        out: dict[str, Decimal] = {}
        for s in symbols:
            try:
                meta = await read_quote_meta(self._redis, s)
            except Exception:  # noqa: BLE001
                meta = None
            if meta is not None and meta[0].last is not None and not meta[0].suspect \
                    and (now - meta[1]).total_seconds() <= QUOTE_MAX_AGE_S:
                out[s] = meta[0].last
        rest = [s for s in symbols if s not in out]
        if rest and self._router is not None:
            try:
                for s, dc in (await self._router.get_day_changes(rest)).items():
                    out[s] = dc.last
            except Exception:
                log.info("personal alerts: day changes unavailable", exc_info=True)
        return out

    async def tick(self) -> int:
        """Returns how many alerts fired."""
        async with self._sm() as session:
            active = list(await session.scalars(
                select(PortalPriceAlert).where(PortalPriceAlert.status == ALERT_ACTIVE)
                .order_by(PortalPriceAlert.id).limit(MAX_ACTIVE)))
        if not active:
            return 0
        catalog = sorted({a.symbol for a in active if a.source != SOURCE_OVERVIEW})
        prices: dict[tuple[str, str], Decimal] = {}
        for s, p in (await self._catalog_prices(catalog)).items():
            prices[("catalog", s)] = p
        if self._overview is not None and any(a.source == SOURCE_OVERVIEW for a in active):
            try:
                for code, p in (await self._overview.prices()).items():
                    prices[(SOURCE_OVERVIEW, code)] = p
            except Exception:
                log.info("personal alerts: overview unavailable", exc_info=True)
        fired = 0
        for a in active:
            price = prices.get((a.source, a.symbol))
            if price is None or not hit(a.direction, a.threshold, price):
                continue
            async with self._sm() as session, session.begin():
                row = await session.get(PortalPriceAlert, a.id, with_for_update=True)
                if row is None or row.status != ALERT_ACTIVE:
                    continue  # deleted, edited or fired meanwhile
                if row.threshold != a.threshold or row.direction != a.direction:
                    continue  # edited meanwhile: checked again next round
                row.status = ALERT_TRIGGERED
                row.triggered_at = self._clock().astimezone(UTC)
                row.triggered_price = price
                await enqueue(session, source="system", category=CATEGORY, level="info",
                              text=text(row, price))
            fired += 1
        return fired
