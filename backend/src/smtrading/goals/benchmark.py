"""The benchmarks of the `beat_index` goals (SPY, QQQ, VWCE.DE), in EUR.

The worker (`BenchmarkSampler`, run by the goals alerts job every 5 minutes) writes the
`benchmark_snapshots`: the price of each benchmark of an active beat_index goal (the
quote's last, else its mid; one quote request per benchmark, so one failing symbol does
not stop the others) with the FX to EUR of that moment, both through the market data
router; a row only when the price in EUR changed or the latest row is an hour old.
When a period has no row before its start (a goal just created, or the first run), the
daily closes of the period (and of the days before it) are backfilled from the router's
daily bars, each at 22:30 local of its trading day, with the ECB rate of that day
(`get_fx_rate_on`; a day whose rate cannot be read is left out). The backfill is marked
done only once every bar has its row; a failed or partial one is tried again with a
backoff (5 minutes, doubling, at most an hour), as long as the process runs.

Reads (`benchmark_change`, `benchmark_series`) use the snapshots only, nothing is fetched
on read: the change of a period is from the latest row before its first local midnight
(the last close before the period; else the first row inside it, only when it is within a
day of the start, otherwise no base: «non iniziato») to the latest row up to now (or up
to the period's end). A latest row more than one business day older than that moment is
stale: no change (logged once per row)."""

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
BASE_WITHIN = timedelta(days=1)  # an in-period base must be this close to the start
RETRY_FIRST, RETRY_MAX = timedelta(minutes=5), timedelta(hours=1)
BACKFILL_BEFORE = timedelta(days=10)
CLOSE_AT = time(22, 30)  # a backfilled daily close is stamped at this local time
_Q8 = Decimal("0.00000001")


def local_midnight(day, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=zone).astimezone(UTC)


def change_pct(base: Decimal, current: Decimal) -> Decimal | None:
    if base <= 0:
        return None
    return (current / base - 1) * 100


_stale_logged: set[tuple[str, datetime]] = set()


def business_days_back(moment: datetime, zone: ZoneInfo) -> datetime:
    """`moment` one business day (Monday–Friday, local) earlier."""
    local = moment.astimezone(zone) - timedelta(days=1)
    while local.weekday() >= 5:
        local -= timedelta(days=1)
    return local.astimezone(UTC)


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
               BenchmarkSnapshot.ts < end_ts,
               BenchmarkSnapshot.ts <= start_ts + BASE_WITHIN)
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
    current = (await session.execute(
        select(BenchmarkSnapshot.ts, BenchmarkSnapshot.price_eur)
        .where(BenchmarkSnapshot.symbol == symbol, BenchmarkSnapshot.ts <= now,
               BenchmarkSnapshot.ts < end_ts)
        .order_by(BenchmarkSnapshot.ts.desc()).limit(1))).first()
    if current is None:
        return None
    if current[0] < business_days_back(min(now, end_ts), zone):
        if (symbol, current[0]) not in _stale_logged:
            _stale_logged.add((symbol, current[0]))
            log.warning("benchmark %s: latest row %s is stale, no change", symbol,
                        current[0].isoformat())
        return None
    return change_pct(base, current[1])


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
    (get_quotes, get_bars, get_fx_rate, get_fx_rate_on). Every router call is made with
    no transaction open; a failing symbol is logged and skipped."""

    def __init__(self, sessionmaker: async_sessionmaker, router, zone: ZoneInfo) -> None:
        self._sm = sessionmaker
        self._router = router
        self._zone = zone
        self._backfilled: set[tuple[str, object]] = set()  # done (per process)
        self._retry: dict[tuple[str, object], tuple[int, datetime]] = {}  # failures, next

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

    async def _backfill(self, symbol: str, ccy: str, start_ts: datetime,
                        now: datetime) -> bool:
        """Writes the daily closes; True once every bar has its row (False: a day whose
        FX could not be read is left out, to be tried again)."""
        bars = await self._router.get_bars(symbol, Timeframe.d1, start_ts - BACKFILL_BEFORE,
                                           now)
        rows, rates, complete = {}, {}, True
        for bar in bars:
            day = (bar.ts + timedelta(hours=12)).date()  # 23:00 UTC of the day before too
            if day not in rates:
                try:
                    rates[day] = (Decimal(1) if ccy == EUR
                                  else await self._router.get_fx_rate_on(ccy, EUR, day))
                except Exception:
                    log.warning("benchmark %s: no %s FX of %s, day left out", symbol, ccy,
                                day, exc_info=True)
                    rates[day] = None
            fx = rates[day]
            if fx is None:
                complete = False
                continue
            ts = min(datetime.combine(day, CLOSE_AT, tzinfo=self._zone).astimezone(UTC), now)
            rows[ts] = {"symbol": symbol, "ts": ts, "price": bar.close, "currency": ccy,
                        "fx_eur": fx, "price_eur": _eur(bar.close * fx)}
        await self._write(list(rows.values()))
        return complete

    async def _maybe_backfill(self, symbol: str, ccy: str, start, now: datetime) -> None:
        key = (symbol, start)
        start_ts = local_midnight(start, self._zone)
        if start_ts > now or key in self._backfilled:
            return
        failures, next_at = self._retry.get(key, (0, now))
        if now < next_at:
            return
        if key not in self._retry and await self._has_base(symbol, start_ts):
            self._backfilled.add(key)
            return
        try:
            done = await self._backfill(symbol, ccy, start_ts, now)
        except Exception:
            log.warning("benchmark %s: backfill failed", symbol, exc_info=True)
            done = False
        if done:
            self._backfilled.add(key)
            self._retry.pop(key, None)
        else:
            self._retry[key] = (failures + 1, now + min(RETRY_FIRST * 2 ** failures,
                                                         RETRY_MAX))

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
        for symbol, starts in sorted(needs.items()):
            ccy = currencies.get(symbol)
            if ccy is None:
                log.warning("benchmark %s: instrument not found", symbol)
                continue
            for start in sorted(starts):
                await self._maybe_backfill(symbol, ccy, start, now)
            try:
                quote = (await self._router.get_quotes([symbol])).get(symbol)
                if _price(quote) is None:
                    continue
                fx = Decimal(1) if ccy == EUR else await self._router.get_fx_rate(ccy, EUR)
                await self._sample(symbol, ccy, fx, quote, now)
            except Exception:
                log.warning("benchmark %s not sampled", symbol, exc_info=True)
