"""The evening summary of mode B (Telegram, `source="system"`, never the activity feed):
per portfolio with rules, once a day (`job_runs`), at the later of the NYSE close + 10
minutes and 22:30 in the portfolio's time zone on a session day, at 22:30 on the others.

The day is a UTC day, the one of the blocked-entry counter (`guards.blocked_key`, the key
`count_blocked` writes): the trades triggered in it and the entries the guards blocked in
it. The counters are read from Redis before any query (no transaction open across it)."""
import logging
import statistics
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.autorules.guards import (
    BLOCKED_KEY,
    BROKER_DOWN,
    BUSY,
    CLOSED,
    NO_STREAM,
    SPREAD,
    STALE,
)
from smtrading.autorules.tables import BRAKED, AutoRule, AutoTrade
from smtrading.autorules.tables import CLOSED as TRADE_CLOSED
from smtrading.common.config import get_settings
from smtrading.instruments.market_hours import session_on
from smtrading.intraday.day_trades import DAY_TRADE_MARKET
from smtrading.jobs.runs import job_done, run_job_once
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.telegram.outbox import enqueue

log = logging.getLogger(__name__)

SUMMARY_JOB = "autorules_summary"
SUMMARY_CHECK_S = 60
AFTER_CLOSE = timedelta(minutes=10)
EVENING = time(22, 30)
IEX_NOTE = "Prezzi gratuiti: volume e scambi solo IEX per le azioni."


def _it(d: Decimal | int, places: int = 0) -> str:
    """Italian digits: 1234.5 → «1.234,50» with places=2."""
    text = f"{Decimal(d):,.{places}f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def _median(values: list[int]) -> str:
    return _it(round(statistics.median(values))) if values else "n/d"


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=UTC)
    return start, start + timedelta(days=1)


async def _blocked(redis, day: date) -> dict[uuid.UUID, dict[str, int]]:
    """The day's counters of every rule: rule id -> reason -> n."""
    out: dict[uuid.UUID, dict[str, int]] = {}
    pattern = BLOCKED_KEY.format(rule_id="*", day=f"{day:%Y%m%d}")
    async for key in redis.scan_iter(match=pattern):
        name = key.decode() if isinstance(key, bytes) else key
        try:
            rule_id = uuid.UUID(name.split(":")[2])
        except (IndexError, ValueError):
            continue
        counts = await redis.hgetall(key)
        out[rule_id] = {(k.decode() if isinstance(k, bytes) else k): int(v)
                        for k, v in counts.items()}
    return out


async def evening_summary_text(session: AsyncSession, redis, portfolio: Portfolio,
                               day: date) -> str | None:
    """«Riepilogo regole automatiche · …» of the UTC day `day`; None without trades and
    without blocked entries."""
    blocked_by_rule = await _blocked(redis, day)  # before any query
    start, end = _day_bounds(day)
    rule_ids = set(await session.scalars(
        select(AutoRule.id).where(AutoRule.portfolio_id == portfolio.id)))
    blocked: dict[str, int] = {}
    for rule_id, counts in blocked_by_rule.items():
        if rule_id in rule_ids:
            for reason, n in counts.items():
                blocked[reason] = blocked.get(reason, 0) + n
    trades = list(await session.scalars(
        select(AutoTrade).where(AutoTrade.portfolio_id == portfolio.id,
                                AutoTrade.trigger_at >= start, AutoTrade.trigger_at < end)))
    closed = [t for t in trades if t.status == TRADE_CLOSED]
    k = sum(blocked.values())
    if not closed and not k:
        return None
    won = sum(1 for t in closed if t.net_result is not None and t.net_result > 0)
    lost = sum(1 for t in closed if t.net_result is not None and t.net_result < 0)
    net = sum((t.net_result for t in closed if t.net_result is not None), Decimal(0))
    fees: dict[str, Decimal] = {}
    for t in closed:
        if t.fees:
            ccy = t.fees_currency or portfolio.base_currency
            fees[ccy] = fees.get(ccy, Decimal(0)) + t.fees
    fees_text = " + ".join(f"{_it(a, 2)} {c}" for c, a in sorted(fees.items())) or (
        f"0,00 {portfolio.base_currency}")
    slips = [t.entry_slippage_bps for t in trades if t.entry_slippage_bps is not None]
    slip = _it(sum(slips, Decimal(0)) / len(slips), 1) if slips else "n/d"
    brakes = await session.scalar(
        select(func.count()).select_from(AutoRule).where(
            AutoRule.portfolio_id == portfolio.id, AutoRule.status == BRAKED,
            AutoRule.status_changed_at >= start, AutoRule.status_changed_at < end))
    return (
        f"Riepilogo regole automatiche · {portfolio.name} · {day:%d/%m}: "
        f"{len(closed)} operazioni ({won} in guadagno, {lost} in perdita), "
        f"risultato netto {_it(net, 2)} {portfolio.base_currency} dopo {fees_text} di "
        f"commissioni; tempi mediani scatto→invio "
        f"{_median([t.ms_trigger_to_send for t in trades if t.ms_trigger_to_send is not None])}"
        f" ms, invio→broker "
        f"{_median([t.ms_send_to_broker for t in trades if t.ms_send_to_broker is not None])}"
        f" ms, broker→esecuzione "
        f"{_median([t.ms_broker_to_fill for t in trades if t.ms_broker_to_fill is not None])}"
        f" ms; scarto medio d'entrata {slip} punti base; scatti bloccati {k} "
        f"({blocked.get(STALE, 0)} prezzo vecchio, {blocked.get(SPREAD, 0)} forbice larga, "
        f"{blocked.get(NO_STREAM, 0)} senza flusso, {blocked.get(BROKER_DOWN, 0)} broker "
        f"fermo, {blocked.get(CLOSED, 0)} mercato chiuso, {blocked.get(BUSY, 0)} già in "
        f"posizione); freni scattati {brakes or 0}. {IEX_NOTE}")


def summary_due_at(day: date, zone: ZoneInfo) -> datetime:
    """When the summary of `day` (a date in `zone`) is due."""
    evening = datetime.combine(day, EVENING, tzinfo=zone)
    try:
        session = session_on(DAY_TRADE_MARKET, day)
    except ValueError:  # outside the calendar's window
        session = None
    if session is None:
        return evening
    return max(session[1] + AFTER_CLOSE, evening)


def _zone(portfolio: Portfolio) -> ZoneInfo:
    try:
        return ZoneInfo(portfolio.timezone or get_settings().tz)
    except (KeyError, ValueError):
        return ZoneInfo(get_settings().tz)


async def send_due_summaries(sessionmaker, redis, now: datetime, *,
                             checked: set[str]) -> int:
    """The summaries due at `now` not sent yet, each once (`job_runs`); `checked` keeps
    the keys already looked at by this process (no query for them again). Returns how
    many were queued."""
    async with sessionmaker() as s:
        portfolios = list(await s.scalars(
            select(Portfolio).where(
                Portfolio.status != PortfolioStatus.archived,
                Portfolio.id.in_(select(AutoRule.portfolio_id).distinct()))
            .order_by(Portfolio.id)))
    sent = 0
    for p in portfolios:
        day = now.astimezone(_zone(p)).date()
        key = f"{p.id}:{day.isoformat()}"
        if key in checked or now < summary_due_at(day, _zone(p)):
            continue
        try:
            async with sessionmaker() as s:
                if await job_done(s, SUMMARY_JOB, key):
                    checked.add(key)
                    continue
            async with sessionmaker() as s:  # the counters are read before its queries
                text = await evening_summary_text(s, redis, p, day)
            if text is not None:
                async with sessionmaker() as s, s.begin():
                    if await run_job_once(s, SUMMARY_JOB, key):
                        await enqueue(s, source="system", category="system", level="info",
                                      text=text)
                        sent += 1
            checked.add(key)
        except Exception:
            log.exception("automatic rules summary of %s failed", key)
    return sent
