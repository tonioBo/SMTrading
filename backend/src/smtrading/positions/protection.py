"""Protection alarm (worker job, at start and every 5 minutes): every open position of a
non-archived portfolio must have active exit stops covering its quantity, valid beyond
the current session.

Protective orders: open (fillable) stop, stop_limit or trailing_stop orders on the exit
side (sell for a long, buy for a short), not being canceled. Problems, one key each per
position (`<portfolio_id>:<symbol>:<kind>`):

- `no_stop`: no protective order at all (a stop still held by a partially filled bracket
  entry is not active yet, and is named in the text);
- `short_stop`: the protective orders cover less than the position's quantity;
- `day_tif`: a protective order, or the other leg of its OCO group (the target), has tif
  `day` and the position was opened before that day's session, or the session ends in
  less than DAY_TIF_MARGIN. The end is the order's `expires_at` (set by the engine: the
  session close, manual closures included; midnight for CRYPTO and FX), else the market's
  next close, else (CRYPTO, FX) 00:00 UTC.

A position changed in the last GRACE is left to the next run (the agent may be placing
its exits right now). Brackets whose entry has not filled are not positions.

Each new problem sends one Telegram warning (category `system`, never the activity feed
the agents read) and, when the Paperclip reader is configured and enabled, one Paperclip
issue per position assigned to the «Risk Manager» agent («PROTEGGI · …»). The open
problems live in `app_settings["protection_alarm"]`, so a restart sends nothing again; a
problem no longer found is cleared, and alerts again if it comes back. A failed issue is
tried again on the next runs (at most ISSUE_ATTEMPTS), its error kept in the state and
shown in the Paperclip status. Every Paperclip request is made with no transaction open."""

import asyncio
import copy
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.common.config import get_settings
from smtrading.common.money import decimal_to_str
from smtrading.engine.models import (
    FILLABLE_STATUSES,
    OPEN_STATUSES,
    BrokerState,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from smtrading.instruments.market_hours import CALENDARS, local_date, market_status, session_on
from smtrading.instruments.models import Instrument
from smtrading.paperclip.client import PaperclipError
from smtrading.paperclip.config import PaperclipConfig, load_config
from smtrading.paperclip.issues import RISK_MANAGER, PaperclipIssues, find_agent_id
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.positions.models import Position
from smtrading.risk.models import AppSetting
from smtrading.telegram.outbox import enqueue

log = logging.getLogger(__name__)

STATE_KEY = "protection_alarm"
INTERVAL_S = 300
GRACE = timedelta(minutes=5)
DAY_TIF_MARGIN = timedelta(minutes=30)
ISSUE_ATTEMPTS = 3
STOP_TYPES = frozenset({OrderType.stop, OrderType.stop_limit, OrderType.trailing_stop})
NO_STOP, SHORT_STOP, DAY_TIF = "no_stop", "short_stop", "day_tif"

DEFAULT_STATE: dict[str, Any] = {"open": {}, "last_error": None, "last_error_at": None,
                                 "last_run_at": None}


# -- evaluation (pure) ------------------------------------------------------------------

@dataclass(frozen=True)
class PositionView:
    portfolio_id: uuid.UUID
    portfolio_name: str
    symbol: str
    market: str
    currency: str
    qty: Decimal
    avg_price: Decimal
    opened_at: datetime | None


@dataclass(frozen=True)
class OrderView:
    id: uuid.UUID
    side: OrderSide
    type: OrderType
    tif: TimeInForce
    status: OrderStatus
    remaining: Decimal | None  # None: a notional order (no quantity known)
    stop_price: Decimal | None
    limit_price: Decimal | None
    expires_at: datetime | None
    oco_group_id: uuid.UUID | None
    role: str | None
    reduce_only: bool
    canceling: bool = False  # a cancel or a replace is in progress at the broker
    parent_partial: bool = False  # held child of a partially filled bracket entry


@dataclass(frozen=True)
class Problem:
    kind: str
    text: str

    @staticmethod
    def key(pos: PositionView, kind: str) -> str:
        return f"{pos.portfolio_id}:{pos.symbol}:{kind}"


@dataclass
class Finding:
    position: PositionView
    orders: list[OrderView]
    problems: list[Problem] = field(default_factory=list)


def num(d: Decimal | None) -> str:
    """Italian decimal comma, no trailing zeros."""
    return "—" if d is None else decimal_to_str(d).replace(".", ",")


def _midnight_utc(now: datetime) -> datetime:
    return datetime.combine(now.astimezone(UTC).date() + timedelta(days=1), time(0), tzinfo=UTC)


def day_window(order: OrderView, market: str, now: datetime) -> tuple[datetime, datetime]:
    """(session start, end) of a tif `day` order: when it expires and when that session
    began (24 h before the end for markets without a calendar)."""
    end = order.expires_at
    if end is None:
        end = market_status(market, now).next_close if market in CALENDARS else None
        end = end or _midnight_utc(now)
    start = end - timedelta(hours=24)
    if market in CALENDARS:
        try:
            session = session_on(market, local_date(market, end - timedelta(minutes=1)))
        except ValueError:
            session = None
        if session is not None:
            start = session[0]
    return start, end


def evaluate(pos: PositionView, orders: list[OrderView], now: datetime,
             zone: ZoneInfo) -> list[Problem]:
    """The problems of one position, given its open orders (pure)."""
    exit_side = OrderSide.sell if pos.qty > 0 else OrderSide.buy
    exits = [o for o in orders if o.side == exit_side and o.type in STOP_TYPES]
    stops = [o for o in exits if o.status in FILLABLE_STATUSES and not o.canceling]
    held = [o for o in exits if o.status == OrderStatus.held and o.parent_partial]
    size = abs(pos.qty)
    problems: list[Problem] = []
    if not stops:
        text = "nessuno stop di uscita attivo"
        if held:
            text += " (stop in attesa: l'ingresso è eseguito solo in parte)"
        problems.append(Problem(NO_STOP, text))
        return problems
    covered = sum((o.remaining or Decimal(0)) for o in stops)
    if covered < size:
        problems.append(Problem(SHORT_STOP, f"lo stop copre {num(covered)} su {num(size)}"))
    # day stops matter only when the gtc ones alone do not cover the position
    lasting = [o for o in stops if o.tif != TimeInForce.day]
    if sum((o.remaining or Decimal(0)) for o in lasting) >= size:
        stops = lasting
    groups = {o.oco_group_id for o in stops if o.oco_group_id is not None}
    legs = stops + [o for o in orders if o.oco_group_id in groups and o not in stops
                    and o.status in FILLABLE_STATUSES and not o.canceling]
    for o in legs:
        if o.tif != TimeInForce.day:
            continue
        start, end = day_window(o, pos.market, now)
        old = pos.opened_at is None or pos.opened_at < start
        if old or end - now < DAY_TIF_MARGIN:
            what = "lo stop" if o in stops else "il target"
            when = end.astimezone(zone).strftime("%d/%m %H:%M")
            problems.append(Problem(DAY_TIF, f"{what} vale solo per la giornata (tif day, "
                                             f"scade il {when})"))
            break
    return problems


# -- texts ------------------------------------------------------------------------------

def telegram_text(pos: PositionView, problem: Problem) -> str:
    return (f"⚠️ Posizione senza protezione — {pos.portfolio_name} · {pos.symbol}: "
            f"{problem.text}")


def issue_title(pos: PositionView) -> str:
    return f"PROTEGGI · {pos.portfolio_name} · {pos.symbol}"


def _order_line(o: OrderView, zone: ZoneInfo) -> str:
    price = o.stop_price if o.type in STOP_TYPES else o.limit_price
    parts = [f"{o.type.value} {o.side.value}", f"qty {num(o.remaining)}",
             f"prezzo {num(price)}", f"tif {o.tif.value}", f"stato {o.status.value}"]
    if o.role:
        parts.append(f"ruolo {o.role}")
    if o.reduce_only:
        parts.append("reduce_only")
    if o.expires_at is not None and o.tif == TimeInForce.day:
        parts.append(f"scade {o.expires_at.astimezone(zone).strftime('%d/%m %H:%M')}")
    return f"- {o.id}: " + ", ".join(parts)


def issue_description(f: Finding, zone: ZoneInfo) -> str:
    pos = f.position
    side = "long" if pos.qty > 0 else "short"
    opened = pos.opened_at.astimezone(zone).strftime("%d/%m/%Y %H:%M") if pos.opened_at else "—"
    lines = [
        "Allarme operativo: posizione aperta senza protezione valida.",
        "",
        f"- portfolio_id: {pos.portfolio_id}",
        f"- simbolo: {pos.symbol} ({side})",
        f"- qty: {num(abs(pos.qty))} · prezzo medio: {num(pos.avg_price)} {pos.currency}",
        f"- aperta il: {opened}",
        "",
        "Problemi:",
        *[f"- {p.text}" for p in f.problems],
        "",
        "Ordini aperti trovati sul simbolo:",
        *([_order_line(o, zone) for o in f.orders] or ["- nessuno"]),
        "",
        ("Cosa fare: verifica la posizione (get_positions, get_orders) e ripristina stop e "
        "target validi fino a revoca: place_oco_order di uscita (side opposto, qty della "
        "posizione, tif gtc, reduce_only). Prima cancella o sostituisci gli ordini di uscita "
        "che restano, per non coprire due volte la stessa qty. Rispondi qui con gli id "
        "degli ordini."),
    ]
    return "\n".join(lines)


# -- database -------------------------------------------------------------------------

def _view(o: Order, partial_parents: set[uuid.UUID]) -> OrderView:
    remaining = None if o.qty is None else o.qty - (o.filled_qty or Decimal(0))
    return OrderView(
        id=o.id, side=o.side, type=o.type, tif=o.tif, status=o.status, remaining=remaining,
        stop_price=o.stop_price, limit_price=o.limit_price, expires_at=o.expires_at,
        oco_group_id=o.oco_group_id, role=o.role, reduce_only=bool(o.reduce_only),
        canceling=o.broker_status in (BrokerState.CANCEL_REQUESTED,
                                      BrokerState.REPLACE_REQUESTED),
        parent_partial=o.parent_id in partial_parents,
    )


async def find_problems(session: AsyncSession, now: datetime, zone: ZoneInfo) -> list[Finding]:
    """The positions with at least one problem (positions changed within GRACE skipped)."""
    rows = (await session.execute(
        select(Position, Portfolio.name, Instrument.market, Instrument.currency)
        .join(Portfolio, Portfolio.id == Position.portfolio_id)
        .join(Instrument, Instrument.symbol == Position.symbol)
        .where(Portfolio.status != PortfolioStatus.archived, Position.qty != 0)
        .order_by(Portfolio.name, Position.symbol))).all()
    positions = [
        PositionView(p.portfolio_id, name, p.symbol, market, ccy, p.qty, p.avg_price,
                     p.opened_at)
        for p, name, market, ccy in rows
        if p.updated_at is None or p.updated_at <= now - GRACE
    ]
    if not positions:
        return []
    orders = list(await session.scalars(
        select(Order)
        .where(Order.portfolio_id.in_({p.portfolio_id for p in positions}),
               Order.symbol.in_({p.symbol for p in positions}),
               Order.status.in_(OPEN_STATUSES))
        .order_by(Order.created_at, Order.id)))
    parent_ids = {o.parent_id for o in orders
                  if o.status == OrderStatus.held and o.parent_id is not None}
    partial = set(await session.scalars(
        select(Order.id).where(Order.id.in_(parent_ids),
                               Order.status == OrderStatus.partially_filled))) \
        if parent_ids else set()
    by_pos: dict[tuple[uuid.UUID, str], list[OrderView]] = defaultdict(list)
    for o in orders:
        by_pos[(o.portfolio_id, o.symbol)].append(_view(o, partial))
    out = []
    for pos in positions:
        mine = by_pos.get((pos.portfolio_id, pos.symbol), [])
        problems = evaluate(pos, mine, now, zone)
        if problems:
            out.append(Finding(pos, mine, problems))
    return out


async def load_state(session: AsyncSession, *, for_update: bool = False) -> dict[str, Any]:
    if for_update:
        await session.execute(insert(AppSetting).values(key=STATE_KEY, value=DEFAULT_STATE)
                              .on_conflict_do_nothing(index_elements=["key"]))
        q = select(AppSetting.value).where(AppSetting.key == STATE_KEY).with_for_update()
    else:
        q = select(AppSetting.value).where(AppSetting.key == STATE_KEY)
    raw = await session.scalar(q)
    state = copy.deepcopy(DEFAULT_STATE)
    if isinstance(raw, dict):
        state.update({k: v for k, v in raw.items() if k in DEFAULT_STATE})
    if not isinstance(state["open"], dict):
        state["open"] = {}
    return state


async def save_state(session: AsyncSession, state: dict[str, Any]) -> None:
    value = {k: state.get(k, v) for k, v in DEFAULT_STATE.items()}
    stmt = insert(AppSetting).values(key=STATE_KEY, value=value)
    await session.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value}))
    await session.flush()


# -- the job ----------------------------------------------------------------------------

IssuesFactory = Callable[[PaperclipConfig], PaperclipIssues]


def _pos_key(key: str) -> str:
    return key.rsplit(":", 1)[0]


class ProtectionAlarm:
    def __init__(self, sessionmaker: async_sessionmaker, *,
                 issues_factory: IssuesFactory | None = None,
                 clock: Callable[[], datetime] | None = None, tz: str | None = None,
                 interval_s: float = INTERVAL_S) -> None:
        self._sm = sessionmaker
        self._issues_factory = issues_factory or (lambda cfg: PaperclipIssues(cfg))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._zone = ZoneInfo(tz or get_settings().tz)
        self._interval_s = interval_s

    async def run(self) -> None:
        while True:  # the first check at once: a restart near the close is covered
            try:
                await self.tick()
            except Exception:
                log.warning("protection check failed; retrying in %ss", self._interval_s,
                            exc_info=True)
            await asyncio.sleep(self._interval_s)

    async def tick(self) -> None:
        now = self._clock()
        async with self._sm() as session:
            findings = await find_problems(session, now, self._zone)
            cfg = await load_config(session)
        current = {Problem.key(f.position, p.kind): (f, p) for f in findings for p in f.problems}
        async with self._sm() as session, session.begin():
            state = await load_state(session, for_update=True)
            opened = state["open"]
            for key in [k for k in opened if k not in current]:
                del opened[key]  # resolved (or the position is closed): alerts again later
            for key, (f, p) in current.items():
                if key in opened:
                    continue
                await enqueue(session, source="system", category="system", level="warning",
                              text=telegram_text(f.position, p))
                opened[key] = {"since": now.isoformat(), "issue": None, "attempts": 0}
                log.warning("position without protection: %s %s: %s",
                            f.position.portfolio_name, f.position.symbol, p.kind)
            state["last_run_at"] = now.isoformat()
            pending = self._pending(state, current)
            await save_state(session, state)
        if pending and cfg.enabled and cfg.complete and not cfg.unreadable:
            await self._create_issues(pending, current)

    @staticmethod
    def _pending(state: dict, current: dict) -> dict[str, list[str]]:
        """Position key -> problem keys still waiting for an issue; a position that
        already has an open issue lends it to its new problems (no second «PROTEGGI»)."""
        opened = state["open"]
        issued = {_pos_key(k): e["issue"] for k, e in opened.items() if e.get("issue")}
        out: dict[str, list[str]] = defaultdict(list)
        for key, entry in opened.items():
            if entry.get("issue") or key not in current:
                continue
            if _pos_key(key) in issued:
                entry["issue"] = issued[_pos_key(key)]
                continue
            if int(entry.get("attempts") or 0) < ISSUE_ATTEMPTS:
                out[_pos_key(key)].append(key)
        return dict(out)

    async def _create_issues(self, pending: dict[str, list[str]], current: dict) -> None:
        async with self._sm() as session:
            cfg = await load_config(session)
        results: dict[str, str | None] = {}
        error: str | None = None
        try:
            async with self._issues_factory(cfg) as client:
                assignee = find_agent_id(await client.agents(), RISK_MANAGER)
                if assignee is None:
                    raise PaperclipError(f"agente «{RISK_MANAGER}» non trovato")
                for pos_key, keys in pending.items():
                    finding = current[keys[0]][0]
                    try:
                        results[pos_key] = await client.create_issue(
                            title=issue_title(finding.position),
                            description=issue_description(finding, self._zone),
                            assignee_agent_id=assignee)
                    except PaperclipError as e:
                        error = e.message
                        results[pos_key] = None
        except PaperclipError as e:
            error = e.message
        except Exception:
            log.warning("protection issue: unexpected error", exc_info=True)
            error = "errore inatteso"
        now = self._clock()
        async with self._sm() as session, session.begin():
            state = await load_state(session, for_update=True)
            for pos_key, keys in pending.items():
                ident = results.get(pos_key)
                for key in keys:
                    entry = state["open"].get(key)
                    if entry is None or entry.get("issue"):
                        continue
                    if ident:
                        entry["issue"] = ident
                    else:
                        entry["attempts"] = int(entry.get("attempts") or 0) + 1
            if error:
                state.update(last_error=error, last_error_at=now.isoformat())
                log.warning("protection issue not created in Paperclip: %s", error)
            elif results:
                state.update(last_error=None, last_error_at=None)
            await save_state(session, state)
