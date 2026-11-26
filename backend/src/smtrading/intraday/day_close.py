"""The end-of-session safety close (worker job): `day_close_minutes` before the session end
of the symbol's market (half days included), the day quantity (`day_quantities`) of every
portfolio with mode A or mode B on is sold by the system with a reduce-only order
(`engine.close_position`, which also cancels the symbol's stop and target): at market on a
tight quote, with a DAY limit at the last trade minus the profile's margin on a quote wider
than MAX_SPREAD (it expires at the session end, never rests overnight), nothing without a
last trade. Once per (portfolio, symbol, session) (`job_runs`); never with the market
closed. A Telegram notice for every close, and one more when a minute before the end the
day quantity is still open (never a second order). Telegram only (`source="system"`), never
the activity feed the agents read.

Market data is read with no transaction open; every close runs in its own transaction."""
import logging
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.common.errors import SMTError
from smtrading.engine.quotesource import QuoteSource
from smtrading.engine.service import close_position
from smtrading.events import discard_post_commit, dispatch_post_commit
from smtrading.instruments.market_hours import local_date, market_status
from smtrading.instruments.models import Instrument
from smtrading.intraday.calendar import day_close_at, session_window
from smtrading.intraday.day_positions import day_quantities
from smtrading.intraday.models import DAY_SCOPE
from smtrading.jobs.runs import job_done, run_job_once
from smtrading.marketdata.quality import is_wide
from smtrading.marketdata.types import Quote
from smtrading.portfolios.locks import lock_live_portfolio
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.positions.models import Position
from smtrading.risk.profiles import effective_risk_profile
from smtrading.signals.service import _it
from smtrading.telegram.outbox import enqueue

log = logging.getLogger(__name__)

DAY_CLOSE_JOB = "intraday_day_close"
DAY_CLOSE_CHECK_JOB = "intraday_day_close_check"
CHECK_BEFORE_END = timedelta(minutes=1)
DEFAULT_MARGIN_PCT = Decimal("0.5")
_CENT = Decimal("0.01")


def close_limit_price(
    quote: Quote, margin_pct: Decimal, tick_size: Decimal | None
) -> tuple[bool, Decimal | None]:
    """(wide spread, limit price): tight → (False, None), at market; wide with a last trade
    → (True, last × (1 − margin/100) floored to the tick, 2 decimals without a tick); wide
    without a last trade → (True, None)."""
    if not is_wide(quote):
        return False, None
    if quote.last is None or quote.last <= 0:
        return True, None
    tick = tick_size.normalize() if tick_size else _CENT  # 0.0100 from the database: 0.01
    raw = quote.last * (1 - margin_pct / 100)
    return True, (raw / tick).to_integral_value(ROUND_FLOOR) * tick


def _price(d: Decimal) -> str:
    return format(d, "f").replace(".", ",")


@dataclass
class _Due:
    portfolio_id: uuid.UUID
    name: str
    symbol: str
    day: date
    close: datetime
    tick_size: Decimal | None
    margin_pct: Decimal
    closing: bool  # in the safety-close window, not done yet
    checking: bool  # in the last minute, not checked yet

    @property
    def key(self) -> str:
        return f"{self.portfolio_id}:{self.symbol}:{self.day.isoformat()}"


class DayCloser:
    def __init__(self, sessionmaker: async_sessionmaker, qs: QuoteSource, redis: Any, *,
                 clock: Callable[[], datetime] | None = None, tz: str | None = None) -> None:
        self._sm = sessionmaker
        self._qs = qs
        self._redis = redis
        # the caller passes `now` to tick(); kept for the worker job's interface
        self._clock = clock or (lambda: datetime.now(UTC))
        self._tz = tz

    async def tick(self, now: datetime) -> None:
        for due in await self._due(now):
            if due.closing:
                try:
                    await self._close(due, now)
                except Exception:
                    log.exception("safety close of %s failed", due.key)
        for due in await self._due(now):
            if due.checking:
                try:
                    await self._check(due, now)
                except Exception:
                    log.exception("safety close check of %s failed", due.key)

    async def _due(self, now: datetime) -> list[_Due]:
        """The (portfolio, symbol) pairs with a day quantity and something to do now."""
        out: list[_Due] = []
        async with self._sm() as s:
            portfolios = list(await s.scalars(
                select(Portfolio).where(DAY_SCOPE, Portfolio.status != PortfolioStatus.archived)
                .order_by(Portfolio.id)))
            for p in portfolios:
                qtys = await day_quantities(s, p.id, now)
                if qtys:
                    out += await self._due_of(s, p, list(qtys), now)
        return out

    async def _due_of(self, s: AsyncSession, p: Portfolio, symbols: list[str],
                      now: datetime) -> list[_Due]:
        instruments = {i.symbol: i for i in await s.scalars(
            select(Instrument).where(Instrument.symbol.in_(symbols)))}
        margin: Decimal | None = None
        out: list[_Due] = []
        for symbol in sorted(symbols):
            inst = instruments[symbol]
            window = session_window(inst.market, now)
            cutoff = day_close_at(inst.market, now, p.day_close_minutes)
            if window is None or cutoff is None or not (cutoff <= now < window[1]):
                continue
            if not market_status(inst.market, now).is_open:
                continue
            if margin is None:
                profile = await effective_risk_profile(s, p)
                margin = (profile.day_close_limit_margin_pct
                          if profile is not None and profile.day_close_limit_margin_pct
                          else DEFAULT_MARGIN_PCT)
            due = _Due(p.id, p.name, symbol, local_date(inst.market, now), window[1],
                       inst.tick_size, margin, False, False)
            due.closing = not await job_done(s, DAY_CLOSE_JOB, due.key)
            due.checking = (window[1] - CHECK_BEFORE_END <= now
                            and not await job_done(s, DAY_CLOSE_CHECK_JOB, due.key))
            if due.closing or due.checking:
                out.append(due)
        return out

    async def _quote(self, symbol: str) -> Quote | None:
        """A usable quote, read with no transaction open; None (try again at the next tick)
        when there is none or the quality guard marked it suspect."""
        try:
            quote = (await self._qs.get_quotes([symbol])).get(symbol)
        except SMTError as e:
            log.warning("safety close: no quote for %s: %s", symbol, e.code)
            return None
        return None if quote is None or quote.suspect is not None else quote

    async def _close(self, due: _Due, now: datetime) -> None:
        quote = await self._quote(due.symbol)
        if quote is None:
            return
        wide, limit = close_limit_price(quote, due.margin_pct, due.tick_size)
        async with self._sm() as s:
            try:
                if not await run_job_once(s, DAY_CLOSE_JOB, due.key):
                    return  # another worker did it
                await lock_live_portfolio(s, due.portfolio_id)
                qty = (await day_quantities(s, due.portfolio_id, now)).get(due.symbol)
                if qty:
                    await self._send(s, due, qty, wide, limit, now)
                await s.commit()
            except BaseException:
                discard_post_commit(s)
                raise
            await dispatch_post_commit(s, self._redis)

    async def _send(self, s: AsyncSession, due: _Due, qty: Decimal, wide: bool,
                    limit: Decimal | None, now: datetime) -> None:
        head = f"Chiusura di sicurezza · {due.name} · {due.symbol}"
        if wide and limit is None:
            await self._notify(s, (
                f"Chiusura di sicurezza impossibile · {due.name} · {due.symbol}: "
                f"{_it(qty)} di giornata ancora aperti, nessun prezzo affidabile "
                "(forbice larga senza ultimo scambio). Nessun ordine inviato."))
            return
        position = await s.get(Position, (due.portfolio_id, due.symbol),
                               populate_existing=True)
        earlier = (position.qty if position is not None else qty) - qty
        try:
            await close_position(
                s, self._qs, due.portfolio_id, due.symbol, qty, source="system",
                agent_id=None, now=now, redis=self._redis, limit_price=limit,
                client_order_id=f"smt-dayclose-{due.day:%Y%m%d}-{due.symbol}")
        except SMTError as e:  # the rejected order is kept (committed by the caller)
            log.warning("safety close of %s refused: %s", due.key, e.code)
            await self._notify(s, (
                f"Chiusura di sicurezza rifiutata · {due.name} · {due.symbol}: "
                f"{_it(qty)} di giornata ancora aperti. Errore {e.code}: {e.message}"))
            return
        how = "a mercato" if limit is None else f"con limite {_price(limit)} (forbice larga)"
        minutes = math.ceil((due.close - now).total_seconds() / 60)
        text = (f"{head}: {_it(qty)} in vendita {how}, {minutes} minuti prima della fine "
                "seduta. Motivo: posizione di giornata ancora aperta.")
        if earlier > 0:
            text += (f"\nRestano {_it(earlier)} comprati prima di oggi: riproteggili "
                     "(stop e obiettivo del simbolo annullati).")
        await self._notify(s, text)

    async def _check(self, due: _Due, now: datetime) -> None:
        async with self._sm() as s:
            if not await run_job_once(s, DAY_CLOSE_CHECK_JOB, due.key):
                return
            qty = (await day_quantities(s, due.portfolio_id, now)).get(due.symbol)
            if qty:
                await self._notify(s, (
                    f"Chiusura di sicurezza non eseguita · {due.name} · {due.symbol}: "
                    f"{_it(qty)} ancora aperti a 1 minuto dalla fine"))
            await s.commit()

    @staticmethod
    async def _notify(s: AsyncSession, text: str) -> None:
        await enqueue(s, source="system", category="system", level="warning", text=text)
