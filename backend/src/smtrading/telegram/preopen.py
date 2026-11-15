"""The pre-open check: Monday to Friday at 08:45 local time (`get_settings().tz`; admin
setting `app_settings["preopen_check"]` = {"on", "at"}, default on at 08:45) one Telegram
message, category `system`, straight into the outbox (never the activity feed, which the
agents read):

- «✅ Pronti per l'apertura — <date>» with a few short lines, or
- «⚠️ Controllo pre-apertura: N problemi — <date>» with the list, or
- «📅 Mercati chiusi oggi — <date>» when NYSE, NASDAQ, MTA and XETRA are all closed
  (no other check runs). Saturdays and Sundays nothing is sent.

What is checked:
1. quotes of the relevant symbols through the market-data router (the symbols of the open
   positions, then the `allowed_symbols` of the portfolios' risk profiles; at most 20):
   available, and not older than QUOTE_MAX_AGE (before the open the last quote is the
   previous close);
2. the markets of today: open, closed, early closes (manual ones marked);
3. worker heartbeat, Redis and database reachable, BrokerSync and LiveMonitor holding
   their advisory locks (their loops are running);
4. kill switch off, no active trading halt, no portfolio locked or reduce-only (at most
   LIST_MAX halts and names, then «… e altri K»);
5. broker links: every enabled link in `status` error (its short last error), and the
   real-money links armed or in dry run whose account data (`last_snapshot_at`, kept by
   the LiveMonitor) is missing or older than LIVE_SNAPSHOT_MAX_AGE.
6. the Paperclip reader, only when it is enabled: unreachable (at least
   ERRORS_BEFORE_ALERT failed reads in a row, with its short last error) or its isolation
   policies not `ok` (changed, not verified, not active), with a short reason; a disabled
   reader adds nothing.
The text is always split with `render.split_message`, every part queued in the same
transaction, so it never exceeds Telegram's limit.

Archived portfolios are left out. Idempotent per local date (`job_runs`, job
`telegram.preopen`, claimed in the same transaction as the outbox row). While Telegram is
not ready nothing is claimed; the message still goes out once it is, within SEND_WINDOW
after the set time; later the day is claimed and skipped (WARNING). The checks read the
database in short sessions and do their network I/O with no transaction open (R9)."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.activity.models import ActivityLevel
from smtrading.brokers.live.monitor import ADVISORY_LOCK_KEY as MONITOR_LOCK_KEY
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.sync import ADVISORY_LOCK_KEY as SYNC_LOCK_KEY
from smtrading.common.config import get_settings
from smtrading.common.heartbeat import heartbeat_age_s
from smtrading.engine.quotesource import QuoteSource
from smtrading.instruments.market_hours import manual_closure, session_on
from smtrading.jobs.runs import job_done, run_job_once
from smtrading.paperclip import config as paperclip_config
from smtrading.paperclip.reader import ERRORS_BEFORE_ALERT as PAPERCLIP_ERRORS
from smtrading.portfolios.models import Portfolio, PortfolioStatus, live_portfolio_ids
from smtrading.positions.models import Position
from smtrading.risk import halts
from smtrading.risk.models import RiskProfile
from smtrading.risk.settings import is_kill_switch_on
from smtrading.telegram.config import is_ready, load_config
from smtrading.telegram.outbox import enqueue
from smtrading.telegram.preopen_settings import load_preopen_settings
from smtrading.telegram.render import split_message

log = logging.getLogger(__name__)

JOB = "telegram.preopen"
SEND_WINDOW = timedelta(hours=2)  # after the set time a missed check is not sent any more
MAIN_MARKETS = ("NYSE", "NASDAQ", "MTA", "XETRA")
MAX_SYMBOLS = 20
QUOTE_MAX_AGE = timedelta(days=4)  # a weekend plus a holiday since the last close
QUOTE_MAX_AGE_S = 60  # asked of the router: a fresh read, not an old cache entry
HEARTBEAT_MAX_AGE_S = 60
REASON_MAX = 80
LIST_MAX = 5  # halts and portfolio names listed, then «… e altri K»
LIVE_SNAPSHOT_MAX_AGE = timedelta(minutes=30)  # account data of an armed/dry-run link
MONITORED_LIVE_STATES = ("armed", "dry_run")
TICK_S = 30


# -- report ----------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketDay:
    market: str
    open: bool
    early_close: str | None  # local time (HH:MM) of an early close
    manual: bool  # the admin's manual closure


@dataclass
class PreopenReport:
    day: date
    markets: list[MarketDay]
    quotes_checked: int
    problems: list[str] = field(default_factory=list)


def market_days(day: date, zone: ZoneInfo) -> list[MarketDay]:
    """The main markets on `day` (the same calendar date on every one of them before the
    European open)."""
    out = []
    for market in MAIN_MARKETS:
        manual = manual_closure(market, day) is not None
        session = session_on(market, day)
        if session is None:
            out.append(MarketDay(market, False, None, manual))
            continue
        _, close, early = session
        out.append(MarketDay(market, True,
                             close.astimezone(zone).strftime("%H:%M") if early else None,
                             manual))
    return out


def capped(items: list[str], limit: int = LIST_MAX) -> str:
    """`a, b, c` with at most `limit` items, then «… e altri K» (pure)."""
    shown = ", ".join(items[:limit])
    return shown if len(items) <= limit else f"{shown} … e altri {len(items) - limit}"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def render_report(report: PreopenReport) -> str:
    """The message (pure)."""
    when = report.day.strftime("%d/%m/%Y")
    n = len(report.problems)
    if n:
        lines = [f"⚠️ Controllo pre-apertura: {_plural(n, 'problema', 'problemi')} — {when}"]
        lines += [f"• {p}" for p in report.problems]
    else:
        lines = [f"✅ Pronti per l'apertura — {when}"]
    open_ = [m.market for m in report.markets if m.open]
    closed = [m.market for m in report.markets if not m.open]
    lines.append(f"Mercati aperti oggi: {', '.join(open_)}")
    if closed:
        lines.append(f"Chiusi oggi: {', '.join(closed)}")
    early = [f"{m.market} alle {m.early_close}{' (chiusura manuale)' if m.manual else ''}"
             for m in report.markets if m.early_close]
    if early:
        lines.append(f"Chiusura anticipata: {', '.join(early)}")
    if not n:
        lines.append("Quotazioni: " + (
            f"{_plural(report.quotes_checked, 'simbolo aggiornato', 'simboli aggiornati')}"
            if report.quotes_checked else "nessun simbolo da controllare"))
        lines.append("Sistema: servizi attivi; database e Redis ok")
        lines.append("Rischio: kill switch spento, nessun blocco aperture, "
                     "nessun portafoglio bloccato")
    return "\n".join(lines)


def render_closed(day: date) -> str:
    return f"📅 Mercati chiusi oggi — {day:%d/%m/%Y}"


# -- checks ----------------------------------------------------------------------------


async def _symbols(session: AsyncSession) -> list[str]:
    held = list(await session.scalars(
        select(Position.symbol).distinct()
        .where(Position.portfolio_id.in_(live_portfolio_ids()), Position.qty != 0)
        .order_by(Position.symbol)))
    allowed = await session.scalars(
        select(RiskProfile.allowed_symbols)
        .join(Portfolio, Portfolio.risk_profile_id == RiskProfile.id)
        .where(Portfolio.status != PortfolioStatus.archived)
        .order_by(RiskProfile.name))
    extra = sorted({s for symbols in allowed for s in (symbols or []) if isinstance(s, str)})
    out = list(dict.fromkeys(held + extra))
    return out[:MAX_SYMBOLS]


async def _holds_lock(session: AsyncSession, key: int) -> bool:
    return bool(await session.scalar(text(
        "select exists (select 1 from pg_locks where locktype = 'advisory' and granted "
        "and objsubid = 1 and ((classid::bigint << 32) | objid::bigint) = :k)"), {"k": key}))


def _short(reason: str | None) -> str:
    reason = " ".join((reason or "").split())
    return reason if len(reason) <= REASON_MAX else reason[:REASON_MAX - 1].rstrip() + "…"


async def _database_problems(session: AsyncSession,
                             now: datetime) -> tuple[list[str], list[str]]:
    """(problems, symbols to check) from the database."""
    problems = []
    if not await _holds_lock(session, SYNC_LOCK_KEY):
        problems.append("Sincronizzazione broker non attiva")
    if not await _holds_lock(session, MONITOR_LOCK_KEY):
        problems.append("Monitor del denaro reale non attivo")
    if await is_kill_switch_on(session):
        problems.append("Kill switch attivo")
    active = await halts.active_halts(session)
    for h in await halts.halt_dtos(session, active[:LIST_MAX]):
        where = "globale" if h["scope"] == "global" else f"su {h['portfolio'] or 'portafoglio'}"
        reason = _short(h["reason"])
        problems.append(f"Blocco aperture {where} attivo{': ' + reason if reason else ''}")
    if len(active) > LIST_MAX:
        problems.append(f"… e altri {len(active) - LIST_MAX} blocchi aperture")
    rows = (await session.execute(
        select(Portfolio.name, Portfolio.status)
        .where(Portfolio.status.in_((PortfolioStatus.locked, PortfolioStatus.reduce_only)))
        .order_by(Portfolio.name))).all()
    for status, label in ((PortfolioStatus.locked, "Portafogli bloccati"),
                          (PortfolioStatus.reduce_only, "Portafogli in sola riduzione")):
        names = [name for name, st in rows if st == status]
        if names:
            problems.append(f"{label}: {capped(names)}")
    problems += await _link_problems(session, now)
    problems += await _paperclip_problems(session)
    return problems, await _symbols(session)


async def _paperclip_problems(session: AsyncSession) -> list[str]:
    """The Paperclip reader line(s): nothing while the reader is disabled."""
    if not (await paperclip_config.load_config(session)).enabled:
        return []
    state = await paperclip_config.load_state(session)
    errors = int(state.get("consecutive_errors") or 0)
    isolation = state.get("isolation")
    changes = state.get("changes") or {}
    changed = (f"Isolamento Paperclip cambiato: {len(changes.get('removed') or [])} regole "
               f"rimosse, {len(changes.get('changed') or [])} modificate")
    if errors >= PAPERCLIP_ERRORS:
        reason = _short(state.get("last_error"))
        out = [f"Paperclip non raggiungibile da {errors} letture"
               + (f": {reason}" if reason else "")]
        return out + [changed] if isolation == "changed" else out
    if isolation == "ok":
        return []
    if isolation == "changed":
        return [changed]
    if isolation == "off":
        return ["Isolamento Paperclip non attivo"]
    reason = _short(state.get("last_error")) or "nessuna lettura riuscita"
    return [f"Isolamento Paperclip non verificato: {reason}"]


async def _link_problems(session: AsyncSession, now: datetime) -> list[str]:
    links = list(await session.scalars(
        select(BrokerLink).where(BrokerLink.enabled.is_(True)).order_by(BrokerLink.name)))
    problems = []
    for link in links:
        name = " ".join(link.name.split())
        if link.status == "error":
            reason = _short(link.last_error)
            problems.append(f"Collegamento broker {name} in errore"
                            f"{': ' + reason if reason else ''}")
        elif link.live_state in MONITORED_LIVE_STATES:
            if link.last_snapshot_at is None:
                problems.append(f"Collegamento broker {name}: nessun dato del conto")
            elif now - link.last_snapshot_at > LIVE_SNAPSHOT_MAX_AGE:
                minutes = int((now - link.last_snapshot_at).total_seconds() // 60)
                problems.append(f"Collegamento broker {name}: dati del conto fermi da "
                                f"{minutes} min")
    return problems


async def _quote_problems(qs: QuoteSource, symbols: list[str], now: datetime) -> list[str]:
    if not symbols:
        return []
    try:
        quotes = await qs.get_quotes(symbols, max_age_s=QUOTE_MAX_AGE_S)
    except Exception:
        log.info("pre-open check: quotes of %s symbols failed together", len(symbols),
                 exc_info=True)
        quotes = {}
        for sym in symbols:  # one at a time, to name the missing ones
            try:
                quotes.update(await qs.get_quotes([sym], max_age_s=QUOTE_MAX_AGE_S))
            except Exception:
                log.info("pre-open check: no quote for %s", sym, exc_info=True)
    missing = [s for s in symbols if s not in quotes]
    stale = [s for s in symbols if s in quotes and quotes[s].ts < now - QUOTE_MAX_AGE]
    problems = []
    if missing:
        problems.append(f"Quotazioni non disponibili: {', '.join(missing)}")
    if stale:
        problems.append(f"Quotazioni vecchie: {', '.join(stale)}")
    return problems


async def _redis_problems(redis) -> list[str]:
    if redis is None:
        return ["Redis non raggiungibile"]
    try:
        await redis.ping()
        age = await heartbeat_age_s(redis)
    except Exception:
        log.warning("pre-open check: redis unreachable", exc_info=True)
        return ["Redis non raggiungibile"]
    if age is None:
        return ["Battito del worker assente"]
    if age > HEARTBEAT_MAX_AGE_S:
        return [f"Worker senza battito da {int(age)} s"]
    return []


async def build_report(sm: async_sessionmaker, qs: QuoteSource, redis, now: datetime,
                       zone: ZoneInfo) -> PreopenReport:
    """Runs the checks: database reads first (one short session, closed before any network
    I/O), then Redis and the quotes."""
    day = now.astimezone(zone).date()
    markets = market_days(day, zone)
    problems: list[str] = []
    symbols: list[str] = []
    try:
        async with sm() as session:
            db_problems, symbols = await _database_problems(session, now)
        problems += db_problems
    except Exception:
        log.exception("pre-open check: database checks failed")
        problems.append("Database non raggiungibile")
    problems += await _redis_problems(redis)
    problems += await _quote_problems(qs, symbols, now)
    return PreopenReport(day, markets, len(symbols), problems)


# -- scheduler -------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PreopenCheck:
    def __init__(self, sessionmaker: async_sessionmaker, qs: QuoteSource, redis, *,
                 clock: Callable[[], datetime] = _utcnow, tz: str | None = None,
                 tick_s: float = TICK_S):
        self._sm = sessionmaker
        self._qs = qs
        self._redis = redis
        self._clock = clock
        self._zone = ZoneInfo(tz or get_settings().tz)
        self._tick_s = tick_s
        self._done: str | None = None  # the local date known done (or given up)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("pre-open check failed")
            await asyncio.sleep(self._tick_s)

    async def tick(self) -> None:
        now = self._clock()
        local = now.astimezone(self._zone)
        key = local.date().isoformat()
        if local.weekday() >= 5 or key == self._done:
            return
        async with self._sm() as session:
            settings = await load_preopen_settings(session)
            if not settings.on:
                return  # not marked: turned on later the same morning it still runs
            start = datetime.combine(local.date(), settings.at, tzinfo=self._zone)
            if local < start:
                return
            if await job_done(session, JOB, key):
                self._done = key
                return
            ready = is_ready(await load_config(session))
        late = local >= start + SEND_WINDOW
        if not ready and not late:
            return  # not claimed: it goes out if Telegram is ready within the window
        if late:
            async with self._sm() as session, session.begin():
                if await run_job_once(session, JOB, key):
                    log.warning("%s of %s skipped: not sent within %s of %s", JOB, key,
                                SEND_WINDOW, settings.at.strftime("%H:%M"))
            self._done = key
            return
        markets = market_days(local.date(), self._zone)
        if not any(m.open for m in markets):
            message, level = render_closed(local.date()), ActivityLevel.info
        else:
            report = await build_report(self._sm, self._qs, self._redis, now, self._zone)
            message = render_report(report)
            level = ActivityLevel.warning if report.problems else ActivityLevel.info
        async with self._sm() as session, session.begin():
            cfg = await load_config(session)
            if not is_ready(cfg):
                return  # turned off meanwhile: tried again next tick
            if await run_job_once(session, JOB, key):  # else another worker sent it
                for part, entities in split_message(message):
                    await enqueue(session, source="system", category="system", level=level,
                                  text=part, entities=entities, cfg=cfg)
        self._done = key
