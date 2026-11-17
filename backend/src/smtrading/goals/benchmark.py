"""The benchmarks of the `beat_index` goals (SPY, QQQ, VWCE.DE), in EUR.

The worker (`BenchmarkSampler`, run by the goals alerts job every 5 minutes) writes the
`benchmark_snapshots`: the price of each benchmark of an active beat_index goal (the
quote's last, else its mid) with the FX to EUR of that moment, both through the market
data router; a row only when the price in EUR changed or the latest row is an hour old.
When a period has no row before its start (a goal just created, or the first run; tried
once per process and period), the daily closes of the period (and of the days before it) are backfilled from the router's
daily bars, each at 22:30 local of its trading day, with the current FX (there is no FX
history: from then on each start carries the FX of its moment).

Reads (`benchmark_change`, `benchmark_series`) use the snapshots only, nothing is fetched
on read: the change of a period is from the latest row before its first local midnight
(the last close before the period; else the first row inside it) to the latest row up to
now (or up to the period's end)."""

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.goals.models import BenchmarkSnapshot
from smtrading.goals.periods import Period
from smtrading.instruments.models import Instrument
from smtrading.marketdata.types import Timeframe

log = logging.getLogger(__name__)

EUR = "EUR"
RESAMPLE_AFTER = timedelta(hours=1)
BACKFILL_BEFORE = timedelta(days=10)
CLOSE_AT = time(22, 30)  # a backfilled daily close is stamped at this local time
_Q8 = Decimal("0.00000001")


def local_midnight(day, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=zone).astimezone(UTC)


def change_pct(base: Decimal, current: Decimal) -> Decimal | None:
    if base <= 0:
        return None
    return (current / base - 1) * 100


async def _base(session: AsyncSession, symbol: str, start_ts: datetime,
                end_ts: datetime) -> Decimal | None:
    before = await session.scalar(
        select(BenchmarkSnapshot.price_eur)
        .where(BenchmarkSnapshot.symbol == symbol, BenchmarkSnapshot.ts < start_ts)
        .order_by(BenchmarkSnapshot.ts.desc()).limit(1))
    if before is not None:
        return before
    return await session.scalar(
        select(BenchmarkSnapshot.price_eur)
        .where(BenchmarkSnapshot.symbol == symbol, BenchmarkSnapshot.ts >= start_ts,
               BenchmarkSnapshot.ts < end_ts)
        .order_by(BenchmarkSnapshot.ts).limit(1))


async def benchmark_change(session: AsyncSession, symbol: str, period: Period,
                           now: datetime, zone: ZoneInfo) -> Decimal | None:
    """The change in percent (EUR) of `symbol` over `period` up to `now`, or None
    without data (before the period too)."""
    start_ts = local_midnight(period.start, zone)
    end_ts = local_midnight(period.end + timedelta(days=1), zone)
    if now < start_ts:
        return None
    base = await _base(session, symbol, start_ts, end_ts)
    if base is None:
        return None
    current = await session.scalar(
        select(BenchmarkSnapshot.price_eur)
        .where(BenchmarkSnapshot.symbol == symbol, BenchmarkSnapshot.ts <= now,
               BenchmarkSnapshot.ts < end_ts)
        .order_by(BenchmarkSnapshot.ts.desc()).limit(1))
    return None if current is None else change_pct(base, current)


async def benchmark_series(session: AsyncSession, symbol: str, period: Period,
                           since: datetime | None, now: datetime,
                           zone: ZoneInfo) -> list[tuple[datetime, Decimal]]:
    """(ts, change in percent from the period's base) of the rows of `period` from
    `since` up to `now`; the series starts at the period's first midnight with 0."""
    start_ts = local_midnight(period.start, zone)
    end_ts = local_midnight(period.end + timedelta(days=1), zone)
    base = await _base(session, symbol, start_ts, end_ts)
    if base is None or base <= 0 or now < start_ts:
        return []
    lower = max(start_ts, since) if since is not None else start_ts
    rows = (await session.execute(
        select(BenchmarkSnapshot.ts, BenchmarkSnapshot.price_eur)
        .where(BenchmarkSnapshot.symbol == symbol, BenchmarkSnapshot.ts >= lower,
               BenchmarkSnapshot.ts <= now, BenchmarkSnapshot.ts < end_ts)
        .order_by(BenchmarkSnapshot.ts))).all()
    out = [(start_ts, Decimal(0))] if lower == start_ts else []
    out += [(ts, change_pct(base, eur)) for ts, eur in rows]
    return out


# -- the sampler (worker) --------------------------------------------------------------


def _price(quote) -> Decimal | None:
    if quote is None:
        return None
    if quote.last is not None and quote.last > 0:
        return quote.last
    if quote.bid and quote.ask and quote.bid > 0 and quote.ask > 0:
        return (quote.bid + quote.ask) / 2
    return None


def _eur(value: Decimal) -> Decimal:
    return value.quantize(_Q8, rounding=ROUND_HALF_EVEN)


class BenchmarkSampler:
    """Writes the benchmark snapshots (see the module docstring) through `router`
    (get_quotes, get_bars, get_fx_rate). Every router call is made with no transaction
    open; a failing symbol is logged and skipped."""

    def __init__(self, sessionmaker: async_sessionmaker, router, zone: ZoneInfo) -> None:
        self._sm = sessionmaker
        self._router = router
        self._zone = zone
        self._backfilled: set[tuple[str, object]] = set()  # tried once per process

    async def _currencies(self, symbols: Iterable[str]) -> dict[str, str]:
        async with self._sm() as session:
            rows = (await session.execute(select(Instrument.symbol, Instrument.currency)
                                          .where(Instrument.symbol.in_(list(symbols))))).all()
        return {sym: ccy.upper() for sym, ccy in rows}

    async def _has_base(self, symbol: str, start_ts: datetime) -> bool:
        async with self._sm() as session:
            return await session.scalar(
                select(BenchmarkSnapshot.ts)
                .where(BenchmarkSnapshot.symbol == symbol, BenchmarkSnapshot.ts < start_ts,
                       BenchmarkSnapshot.ts >= start_ts - BACKFILL_BEFORE).limit(1)) is not None

    async def _write(self, rows: list[dict]) -> None:
        if not rows:
            return
        async with self._sm() as session, session.begin():
            await session.execute(insert(BenchmarkSnapshot).values(rows)
                                  .on_conflict_do_nothing(index_elements=["symbol", "ts"]))

    async def _backfill(self, symbol: str, ccy: str, fx: Decimal, start_ts: datetime,
                        now: datetime) -> None:
        bars = await self._router.get_bars(symbol, Timeframe.d1, start_ts - BACKFILL_BEFORE,
                                           now)
        rows = {}
        for bar in bars:
            day = (bar.ts + timedelta(hours=12)).date()  # 23:00 UTC of the day before too
            ts = min(datetime.combine(day, CLOSE_AT, tzinfo=self._zone).astimezone(UTC), now)
            rows[ts] = {"symbol": symbol, "ts": ts, "price": bar.close, "currency": ccy,
                        "fx_eur": fx, "price_eur": _eur(bar.close * fx)}
        await self._write(list(rows.values()))

    async def _sample(self, symbol: str, ccy: str, fx: Decimal, quote, now: datetime) -> None:
        price = _price(quote)
        if price is None:
            return
        eur = _eur(price * fx)
        async with self._sm() as session:
            last = (await session.execute(
                select(BenchmarkSnapshot.ts, BenchmarkSnapshot.price_eur)
                .where(BenchmarkSnapshot.symbol == symbol, BenchmarkSnapshot.ts <= now)
                .order_by(BenchmarkSnapshot.ts.desc()).limit(1))).first()
        if last is not None and last[1] == eur and now - last[0] < RESAMPLE_AFTER:
            return
        await self._write([{"symbol": symbol, "ts": now, "price": price, "currency": ccy,
                            "fx_eur": fx, "price_eur": eur}])

    async def run(self, needs: dict[str, set], now: datetime) -> None:
        """`needs`: benchmark -> the starts (local dates) of the periods to cover."""
        if not needs:
            return
        currencies = await self._currencies(needs)
        try:
            quotes = await self._router.get_quotes(sorted(needs))
        except Exception:
            log.warning("benchmark quotes failed", exc_info=True)
            quotes = {}
        for symbol, starts in sorted(needs.items()):
            ccy = currencies.get(symbol)
            if ccy is None:
                log.warning("benchmark %s: instrument not found", symbol)
                continue
            try:
                fx = Decimal(1) if ccy == EUR else await self._router.get_fx_rate(ccy, EUR)
                for start in sorted(starts):
                    start_ts = local_midnight(start, self._zone)
                    if (start_ts <= now and (symbol, start) not in self._backfilled
                            and not await self._has_base(symbol, start_ts)):
                        self._backfilled.add((symbol, start))
                        await self._backfill(symbol, ccy, fx, start_ts, now)
                await self._sample(symbol, ccy, fx, quotes.get(symbol), now)
            except Exception:
                log.warning("benchmark %s not sampled", symbol, exc_info=True)
