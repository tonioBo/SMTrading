"""Protection alarm (worker job, every 5 minutes; the first run once the worker's startup
gate opens, at least 3 minutes after the worker start): every open position of a
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

A stop of a broker-linked portfolio not at the broker yet (`to_send`: e.g. a fractional
Alpaca exit renewed every session by BrokerSync, sent again at the open) protects while
the market is closed and for SEND_GRACE of an open session; still unsent after that it
no longer counts (`no_stop` / `short_stop`, the text says it waits to be sent).

A position changed in the last GRACE, or one of whose exit-side orders changed in the last
GRACE, is left to the next run (the agent may be placing or replacing its exits right
now); its open problems are kept as they are, not cleared. A stop being modified
(replace in flight) still protects; one being canceled does not. Brackets whose entry has
not filled are not positions.

Each new problem sends one Telegram warning (category `system`, never the activity feed
the agents read) and, when the Paperclip reader is configured and enabled, one Paperclip
issue per position assigned to the «Risk Manager» agent («PROTEGGI · …»). A portfolio no
agent can access (no `agent_portfolios` row: the owner's alone) gets the Telegram warning
only, never an issue: nothing of it reaches the agents. The open
problems live in `app_settings["protection_alarm"]`, so a restart sends nothing again; a
problem no longer found is cleared, and alerts again if it comes back. A failed issue is
tried again on the next runs (at most ISSUE_ATTEMPTS), its error kept in the state and
shown in the Paperclip status; after a timeout (the issue may exist anyway) the retry first
looks for an open issue with the same title and adopts it. The Paperclip configuration is
read only when an issue is pending. Every Paperclip request is made with no transaction
open."""

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

from smtrading.agents.access import portfolios_with_agents
from smtrading.common.config import get_settings
from smtrading.common.errors import SMTError
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
from smtrading.worker.jobstate import job_tick

log = logging.getLogger(__name__)

STATE_KEY = "protection_alarm"
INTERVAL_S = 300
GRACE = timedelta(minutes=5)
DAY_TIF_MARGIN = timedelta(minutes=30)
SEND_GRACE = timedelta(minutes=15)
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
    canceling: bool = False  # a cancel is in progress at the broker (a replace is not)
    parent_partial: bool = False  # held child of a partially filled bracket entry
    # waiting to be sent to the linked broker since (to_send, no broker order yet)
    unsent_since: datetime | None = None


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


def unsent_too_long(order: OrderView, market: str, now: datetime) -> bool:
    """A stop still waiting to be sent to the broker beyond SEND_GRACE of an open
    session (while the market is closed it waits for the open)."""
    if order.unsent_since is None:
        return False
    try:
        if not market_status(market, now).is_open:
            return False
    except SMTError:  # an unknown market: judged on the wait alone
        pass
    since = order.unsent_since
    if market in CALENDARS:
        try:
            session = session_on(market, local_date(market, now))
        except ValueError:
            session = None
        if session is not None:
            since = max(since, session[0])
    return now - since > SEND_GRACE


def evaluate(pos: PositionView, orders: list[OrderView], now: datetime,
             zone: ZoneInfo) -> list[Problem]:
    """The problems of one position, given its open orders (pure)."""
    exit_side = OrderSide.sell if pos.qty > 0 else OrderSide.buy
    exits = [o for o in orders if o.side == exit_side and o.type in STOP_TYPES]
    stops = [o for o in exits if o.status in FILLABLE_STATUSES and not o.canceling]
    unsent = [o for o in stops if unsent_too_long(o, pos.market, now)]
    stops = [o for o in stops if o not in unsent]
    held = [o for o in exits if o.status == OrderStatus.held and o.parent_partial]
    size = abs(pos.qty)
    problems: list[Problem] = []
    waiting = " (stop non ancora inviato al broker a seduta aperta)" if unsent else ""
    if not stops:
        text = "nessuno stop di uscita attivo" + waiting
        if held:
            text += " (stop in attesa: l'ingresso è eseguito solo in parte)"
        problems.append(Problem(NO_STOP, text))
        return problems
    covered = sum((o.remaining or Decimal(0)) for o in stops)
    if covered < size:
        problems.append(Problem(SHORT_STOP, f"lo stop copre {num(covered)} su {num(size)}"
                                            + waiting))
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

OWNER_ONLY_NOTE = " (nessun agente ha accesso a questo portafoglio: avviso solo a te)"


def telegram_text(pos: PositionView, problem: Problem, *, owner_only: bool = False) -> str:
    return (f"⚠️ Posizione senza protezione — {pos.portfolio_name} · {pos.symbol}: "
            f"{problem.text}" + (OWNER_ONLY_NOTE if owner_only else ""))


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
        ("Cosa fare: verifica la posizione e i suoi ordini (get_positions, list_orders). "
         "Correggi un'uscita esistente con modify_order o toglila con cancel_order; poi "
         "ripristina stop e target validi fino a revoca con place_oco_order di uscita "
         "(side opposto, tif gtc, qty non oltre quella della posizione rimasta scoperta: "
         "l'OCO non è reduce_only, una qty in eccesso aprirebbe una posizione opposta). "
         "Cancella o correggi prima le uscite che restano, per non coprire due volte la "
         "stessa qty."),
        *([("Ingresso bracket eseguito solo in parte: i suoi stop e target in attesa si "
            "attivano quando l'ingresso è eseguito per intero. Cancella la parte restante "
            "dell'ingresso (o l'intero bracket, poi proteggi la qty eseguita) oppure "
            "attendi l'esecuzione: non aggiungere un altro OCO che coprirebbe due volte "
            "la stessa qty.")] if any(o.parent_partial for o in f.orders) else []),
        "Rispondi qui con gli id degli ordini.",
    ]
    return "\n".join(lines)


# -- database -------------------------------------------------------------------------

def _view(o: Order, partial_parents: set[uuid.UUID]) -> OrderView:
    remaining = None if o.qty is None else o.qty - (o.filled_qty or Decimal(0))
    return OrderView(
        id=o.id, side=o.side, type=o.type, tif=o.tif, status=o.status, remaining=remaining,
        stop_price=o.stop_price, limit_price=o.limit_price, expires_at=o.expires_at,
        oco_group_id=o.oco_group_id, role=o.role, reduce_only=bool(o.reduce_only),
        canceling=o.broker_status == BrokerState.CANCEL_REQUESTED,
        parent_partial=o.parent_id in partial_parents,
        unsent_since=(o.updated_at if o.broker_link_id is not None
                      and o.broker_order_id is None
                      and o.broker_status == BrokerState.TO_SEND else None),
    )


def _recent(t: datetime | None, now: datetime) -> bool:
    return t is not None and now - GRACE < t <= now


async def find_problems(session: AsyncSession, now: datetime,
                        zone: ZoneInfo) -> tuple[list[Finding], set[str]]:
    """The positions with at least one problem, and the keys (`<portfolio_id>:<symbol>`)
    of the positions skipped because they, or one of their exit-side orders, changed
    within GRACE."""
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
    ]
    skipped = {f"{p.portfolio_id}:{p.symbol}" for p, *_ in rows
               if p.updated_at is not None and p.updated_at > now - GRACE}
    positions = [p for p in positions if f"{p.portfolio_id}:{p.symbol}" not in skipped]
    if not positions:
        return [], skipped
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
    touched: dict[tuple[uuid.UUID, str], set[OrderSide]] = defaultdict(set)
    for o in orders:
        by_pos[(o.portfolio_id, o.symbol)].append(_view(o, partial))
        if _recent(o.updated_at, now):
            touched[(o.portfolio_id, o.symbol)].add(o.side)
    out = []
    for pos in positions:
        exit_side = OrderSide.sell if pos.qty > 0 else OrderSide.buy
        if exit_side in touched.get((pos.portfolio_id, pos.symbol), set()):
            skipped.add(f"{pos.portfolio_id}:{pos.symbol}")
            continue
        mine = by_pos.get((pos.portfolio_id, pos.symbol), [])
        problems = evaluate(pos, mine, now, zone)
        if problems:
            out.append(Finding(pos, mine, problems))
    return out, skipped


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
        while True:  # the first check at once (after the worker's startup gate)
            try:
                await self.tick()
                await job_tick()  # «Salute del sistema»: last pass
            except Exception:
                log.warning("protection check failed; retrying in %ss", self._interval_s,
                            exc_info=True)
            await asyncio.sleep(self._interval_s)

    async def tick(self) -> None:
        now = self._clock()
        async with self._sm() as session:
            findings, skipped = await find_problems(session, now, self._zone)
            reachable = await portfolios_with_agents(
                session, {f.position.portfolio_id for f in findings})
        current = {Problem.key(f.position, p.kind): (f, p) for f in findings for p in f.problems}
        async with self._sm() as session, session.begin():
            state = await load_state(session, for_update=True)
            opened = state["open"]
            for key in [k for k in opened if k not in current and _pos_key(k) not in skipped]:
                del opened[key]  # resolved (or the position is closed): alerts again later
            for key, (f, p) in current.items():
                if key in opened:
                    continue
                await enqueue(session, source="system", category="system", level="warning",
                              text=telegram_text(
                                  f.position, p,
                                  owner_only=f.position.portfolio_id not in reachable))
                opened[key] = {"since": now.isoformat(), "issue": None, "attempts": 0}
                log.warning("position without protection: %s %s: %s",
                            f.position.portfolio_name, f.position.symbol, p.kind)
            state["last_run_at"] = now.isoformat()
            pending = self._pending(state, current, reachable)
            await save_state(session, state)
        if pending:
            await self._create_issues(pending, current)

    @staticmethod
    def _pending(state: dict, current: dict, reachable: set[uuid.UUID]) -> dict[str, list[str]]:
        """Position key -> problem keys still waiting for an issue; a position that
        already has an open issue lends it to its new problems (no second «PROTEGGI»).
        A portfolio no agent can access (not in `reachable`) never waits for one."""
        opened = state["open"]
        issued = {_pos_key(k): e["issue"] for k, e in opened.items() if e.get("issue")}
        out: dict[str, list[str]] = defaultdict(list)
        for key, entry in opened.items():
            if entry.get("issue") or key not in current:
                continue
            if current[key][0].position.portfolio_id not in reachable:
                continue  # the owner's alone: Telegram only
            if _pos_key(key) in issued:
                entry["issue"] = issued[_pos_key(key)]
                continue
            if int(entry.get("attempts") or 0) < ISSUE_ATTEMPTS:
                out[_pos_key(key)].append(key)
        return dict(out)

    async def _create_issues(self, pending: dict[str, list[str]], current: dict) -> None:
        async with self._sm() as session:
            cfg = await load_config(session)
            if not (cfg.enabled and cfg.complete and not cfg.unreadable):
                return
            opened = (await load_state(session))["open"]
        uncertain = {pk for pk, keys in pending.items()
                     if any((opened.get(k) or {}).get("uncertain") for k in keys)}
        results: dict[str, str | None] = {}
        timed_out: set[str] = set()
        error: str | None = None
        try:
            async with self._issues_factory(cfg) as client:
                assignee = find_agent_id(await client.agents(), RISK_MANAGER)
                if assignee is None:
                    raise PaperclipError(f"agente «{RISK_MANAGER}» non trovato")
                for pos_key, keys in pending.items():
                    finding = current[keys[0]][0]
                    title = issue_title(finding.position)
                    try:
                        if pos_key in uncertain and (
                                found := await client.open_issue_with_title(title)):
                            results[pos_key] = found
                            continue
                        results[pos_key] = await client.create_issue(
                            title=title,
                            description=issue_description(finding, self._zone),
                            assignee_agent_id=assignee)
                    except PaperclipError as e:
                        error = e.message
                        results[pos_key] = None
                        if e.message == "timeout":
                            timed_out.add(pos_key)
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
                        entry.pop("uncertain", None)
                    else:
                        entry["attempts"] = int(entry.get("attempts") or 0) + 1
                        if pos_key in timed_out:
                            entry["uncertain"] = True
            if error:
                state.update(last_error=error, last_error_at=now.isoformat())
                log.warning("protection issue not created in Paperclip: %s", error)
            elif results:
                state.update(last_error=None, last_error_at=None)
            await save_state(session, state)
