"""«Screener tecnico» and the multi-period sector map (plan 30): technical figures per
catalog instrument from its daily bars, precomputed by the worker so the page is instant.

`metrics(bars)` (pure): last close, RSI 14, SMA 200, the 52-week high and low (the bars
of the last 365 days), the last session's high, low and volume, the average volume of
the 20 sessions before it, and the change over 1 session, 1 week, 1 month (30 days) and
since the start of the year (against the last close of the previous year).

`ScreenerJob` (worker, behind the startup gate): after each close it refreshes a group's
rows — `eu` (EU equities) from EU_AT, `us` (US equities) from US_AT, on weekdays, and
`crypto` from CRYPTO_AT every day — CHUNK symbols per tick (daily bars only, through
the market data router: its cache, rate limits and quotas), pausing while the gate says
the providers are busy (`quota_gate`: Yahoo's hourly use under QUOTA_SHARE of its
budget). A row computed after the group's start time today is skipped, so a restart
resumes where it stopped; when no row is left the group is marked done for the day in
`job_runs` (idempotent). A symbol whose bars fail is retried on later ticks at most
MAX_ATTEMPTS times. A group never completed is computed at once (first deploy).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.common.errors import SMTError
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.jobs.runs import job_done, run_job_once
from smtrading.marketdata import indicators, quota
from smtrading.marketdata.indicators_service import fetch_bars
from smtrading.marketdata.types import Bar, Timeframe
from smtrading.marketsportal.models import MarketScreenRow
from smtrading.risk.models import JobRun
from smtrading.worker.jobstate import job_tick

log = logging.getLogger(__name__)

JOB = "market screener"
BARS = 300  # SMA 200, 52 weeks and the start of the year
TICK_S = 60
CHUNK = 20  # symbols per tick: at most CHUNK daily-bar requests a minute
MAX_ATTEMPTS = 3
QUOTA_SHARE = Decimal("0.6")
EU_AT, US_AT, CRYPTO_AT = time(17, 0), time(22, 0), time(0, 30)  # UTC
GROUPS: dict[str, tuple[tuple[AssetClass, ...], time, bool]] = {
    # group -> (asset classes, start time UTC, weekdays only)
    "eu": ((AssetClass.eu_equity,), EU_AT, True),
    "us": ((AssetClass.us_equity,), US_AT, True),
    "crypto": ((AssetClass.crypto,), CRYPTO_AT, False),
}
_Q = Decimal("0.01")


def _q(d: Decimal | None) -> Decimal | None:
    return None if d is None else d.quantize(_Q, rounding=ROUND_HALF_EVEN)


def _pct(now: Decimal, ref: Decimal | None) -> Decimal | None:
    if ref is None or ref <= 0:
        return None
    return _q((now / ref - 1) * 100)


def _last(values: list) -> Any:
    return next((v for v in reversed(values) if v is not None), None)


def metrics(bars: list[Bar]) -> dict | None:
    """The row's figures from daily bars (oldest first); None under 2 bars."""
    if len(bars) < 2:
        return None
    closes = [b.close for b in bars]
    last = bars[-1]
    as_of = last.ts.date()
    year = [b for b in bars if b.ts.date() > as_of - timedelta(days=365)]

    def close_on_or_before(day: date) -> Decimal | None:
        return next((b.close for b in reversed(bars) if b.ts.date() <= day), None)

    prior = [b.volume for b in bars[-21:-1]]
    avg20 = sum(prior, Decimal(0)) / 20 if len(prior) == 20 else None
    ytd_ref = next((b.close for b in reversed(bars) if b.ts.year < as_of.year), None)
    return {
        "as_of": as_of, "bars": len(bars), "close": last.close,
        "rsi14": _q(_last(indicators.rsi(closes, 14))),
        "sma200": _last(indicators.sma(closes, 200)) if len(closes) >= 200 else None,
        "high_52w": max(b.high for b in year), "low_52w": min(b.low for b in year),
        "day_high": last.high, "day_low": last.low, "volume": last.volume,
        "avg_volume20": avg20,
        "chg_1d": _pct(last.close, closes[-2]),
        "chg_1w": _pct(last.close, close_on_or_before(as_of - timedelta(days=7))),
        "chg_1m": _pct(last.close, close_on_or_before(as_of - timedelta(days=30))),
        "chg_ytd": _pct(last.close, ytd_ref),
    }


# ------------------------------------------------------------------ presets

def _gt(a, b) -> bool:
    return a is not None and b is not None and a > b


def _lt(a, b) -> bool:
    return a is not None and b is not None and a < b


PRESETS: dict[str, tuple[str, Callable[[MarketScreenRow], bool]]] = {
    "rsi_oversold": ("RSI(14) sotto 30", lambda r: _lt(r.rsi14, Decimal(30))),
    "rsi_overbought": ("RSI(14) sopra 70", lambda r: _gt(r.rsi14, Decimal(70))),
    "above_sma200": ("Prezzo sopra la SMA 200", lambda r: _gt(r.close, r.sma200)),
    "below_sma200": ("Prezzo sotto la SMA 200", lambda r: _lt(r.close, r.sma200)),
    "new_high": ("Nuovo massimo a 52 settimane",
                 lambda r: r.day_high is not None and r.high_52w is not None
                 and r.day_high >= r.high_52w),
    "new_low": ("Nuovo minimo a 52 settimane",
                lambda r: r.day_low is not None and r.low_52w is not None
                and r.day_low <= r.low_52w),
    "volume_spike": ("Volume oltre 2× la media a 20 giorni",
                     lambda r: r.avg_volume20 is not None and r.avg_volume20 > 0
                     and r.volume is not None and r.volume > 2 * r.avg_volume20),
    "near_low": ("Entro il 5% dal minimo a 52 settimane",
                 lambda r: r.low_52w is not None and r.low_52w > 0
                 and r.close <= r.low_52w * Decimal("1.05")),
}


def row_dto(r: MarketScreenRow, i: Instrument) -> dict:
    def ratio(a, b, minus_one: bool = True) -> Decimal | None:
        if a is None or b is None or b <= 0:
            return None
        return _q((a / b - (1 if minus_one else 0)) * (100 if minus_one else 1))

    return {
        "symbol": i.symbol, "name": i.name, "sector": i.sector, "market": i.market,
        "currency": i.currency, "asset_class": str(i.asset_class), "as_of": r.as_of,
        "close": r.close, "rsi14": r.rsi14, "sma200": r.sma200,
        "vs_sma200_pct": ratio(r.close, r.sma200),
        "high_52w": r.high_52w, "low_52w": r.low_52w,
        "from_low_pct": ratio(r.close, r.low_52w),
        "from_high_pct": ratio(r.close, r.high_52w),
        "volume_ratio": ratio(r.volume, r.avg_volume20, minus_one=False),
        "chg_1d": r.chg_1d, "chg_1w": r.chg_1w, "chg_1m": r.chg_1m, "chg_ytd": r.chg_ytd,
    }


async def upsert(session: AsyncSession, symbol: str, m: dict, now: datetime) -> None:
    values = {"symbol": symbol, "computed_at": now, **m}
    stmt = insert(MarketScreenRow).values(**values)
    await session.execute(stmt.on_conflict_do_update(
        index_elements=[MarketScreenRow.symbol],
        set_={k: stmt.excluded[k] for k in values if k != "symbol"}))


# ------------------------------------------------------------------ the job


async def quota_gate(redis) -> bool:
    """True while Yahoo's hourly use is under QUOTA_SHARE of its budget (the screener
    never takes the allowance the trading side needs). Redis down: True."""
    if redis is None:
        return True
    from smtrading.marketdata.providers.yahoo import YahooProvider

    try:
        used = (await quota.usage(redis, ["yahoo"])).get("yahoo")
    except Exception:  # noqa: BLE001
        return True
    if used is None:
        return True
    if used.blocked_until is not None:
        return False
    budget = YahooProvider.default_hourly_budget
    return not budget or used.hour < QUOTA_SHARE * budget


class ScreenerJob:
    def __init__(self, sessionmaker: async_sessionmaker, router, redis=None, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 chunk: int = CHUNK, tick_s: float = TICK_S,
                 gate: Callable[[], Awaitable[bool]] | None = None) -> None:
        self._sm = sessionmaker
        self._router = router
        self._clock = clock
        self._chunk = chunk
        self._tick_s = tick_s
        self._gate = gate or (lambda: quota_gate(redis))
        self._attempts: dict[tuple[str, str], int] = {}

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
                await job_tick()
            except Exception:
                log.warning("market screener failed; retrying in %ss", self._tick_s,
                            exc_info=True)
            await asyncio.sleep(self._tick_s)

    async def _due(self, now: datetime) -> list[tuple[str, str, datetime]]:
        """(group, day key, start instant) of the groups to work on now."""
        out = []
        today = now.date()
        async with self._sm() as session:
            for group, (classes, at, weekdays) in GROUPS.items():
                key = f"{today.isoformat()}:{group}"
                if await job_done(session, JOB, key):
                    continue
                start = datetime.combine(today, at, UTC)
                ever = await session.scalar(
                    select(JobRun.key).where(JobRun.job == JOB,
                                             JobRun.key.like(f"%:{group}")).limit(1))
                if ever is None:  # never completed (first deploy): from today's midnight
                    out.append((group, key, datetime.combine(today, time(0), UTC)))
                elif now >= start and not (weekdays and today.weekday() >= 5):
                    out.append((group, key, start))
        return out

    async def _pending(self, group: str, key: str, since: datetime) -> list[str]:
        classes = GROUPS[group][0]
        async with self._sm() as session:
            rows = (await session.execute(
                select(Instrument.symbol, MarketScreenRow.computed_at)
                .outerjoin(MarketScreenRow, MarketScreenRow.symbol == Instrument.symbol)
                .where(Instrument.active.is_(True), Instrument.asset_class.in_(classes))
                .order_by(Instrument.symbol))).all()
        return [s for s, at in rows
                if (at is None or at < since)
                and self._attempts.get((key, s), 0) < MAX_ATTEMPTS]

    async def tick(self) -> int:
        """One pass: at most `chunk` symbols. Returns how many rows were written."""
        now = self._clock().astimezone(UTC)
        written = 0
        budget = self._chunk
        for group, key, since in await self._due(now):
            pending = await self._pending(group, key, since)
            if not pending:
                async with self._sm() as session, session.begin():
                    await run_job_once(session, JOB, key)
                log.info("market screener: %s done", key)
                continue
            if budget <= 0:
                break
            if not await self._gate():
                log.info("market screener: providers busy, waiting")
                break
            for sym in pending[:budget]:
                budget -= 1
                try:
                    bars = await fetch_bars(self._router, sym, Timeframe.d1, BARS, now=now)
                except SMTError as e:
                    self._attempts[(key, sym)] = self._attempts.get((key, sym), 0) + 1
                    log.info("market screener: %s bars failed: %s", sym, e.code)
                    if str(e.code) == "RATE_LIMITED" or "rate_limited" in str(e.details):
                        budget = 0
                        break
                    continue
                m = metrics(list(bars))
                if m is None:
                    self._attempts[(key, sym)] = MAX_ATTEMPTS  # no history: give up today
                    continue
                async with self._sm() as session, session.begin():
                    await upsert(session, sym, m, self._clock().astimezone(UTC))
                written += 1
            if budget <= 0:
                break
        return written
