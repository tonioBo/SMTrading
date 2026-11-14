"""Live reconciliation: the broker's positions and cash against a real-money portfolio.

Run by the LiveMonitor every LIVE_RECONCILE_INTERVAL_S and after each fill; it never
corrects anything. Reads happen in a short session, the broker calls (GET only) with no
transaction open (R9), the outcome is written in a last session (lock order R8.9: link
FOR NO KEY UPDATE, then the portfolio through `trip_reduce_only`):

- bands, not skips (R8.8): our quantity of a symbol is consistent when it lies within
  [broker − open buys, broker + open sells], the open quantities of the orders claimed or
  sent to the broker (read before and after the broker calls: the larger counts), with a
  QTY_EPSILON; a difference inside the band is `explained` (a fill not yet synced), one
  outside is a position diff. A symbol with an open notional order (no quantity) cannot
  be bounded: `skipped`;
- cash: broker cash below ours by more than the tolerance is a shortfall; the tolerance is
  `cash_tolerance` + 1 % of the cash held in other currencies than the account's
  (converted, R8.5) + what the open buys at the broker may still cost (a buy filled there
  and not yet synced). Open sells never skip it (a surplus is fine: the account is
  dedicated). A missing FX rate or price: `skipped`;
- debounce (R8.7): a diff is recorded at once (`Reconciliation(ok=False)`), the portfolio
  goes reduce-only (`reconcile_diff` / `cash_shortfall`) only when the same diff is found
  again on the next pass;
- an `explained` or `skipped` symbol (or cash) that lasts more than UNEXPLAINED_ALERT_AFTER
  sends one critical `live.reconcile_unexplained` per episode (its `since` is carried in
  the stored rows);
- rows are written only when the outcome changes (cash amounts aside), plus one heartbeat
  row per HEARTBEAT; the returned row is the current outcome either way;
- another account behind the same keys (fingerprint) -> the activation service's
  `account_changed` (disarmed, reduce-only, one critical `live.account_changed`); the
  stored fingerprint changes only with a new test;
- an Alpaca account that is no longer a cash account (multiplier > 1, R8.10) -> reduce-only
  (`margin_account`, critical alert);
- broker failures raise LiveBrokerUnreachable (`auth` for HTTP 401/403), a broker rate
  limit LiveBrokerRateLimited (not a health failure).

Local corporate actions skip live-linked portfolios (corporate.actions, R8.6): a split
at the broker shows here as a position diff, for the admin to resolve.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.activity.models import Activity
from smtrading.brokers.base import AccountSnapshot, RateLimited
from smtrading.brokers.live.account import (
    account_fingerprint,
    decrypt_live_config,
    snapshot_record,
)
from smtrading.brokers.live.protect import live_alert, trip_reduce_only
from smtrading.brokers.live.service import account_changed
from smtrading.brokers.models import BrokerLink, Reconciliation
from smtrading.brokers.registry import adapter_class
from smtrading.engine.models import OPEN_STATUSES, Order, OrderSide, OrderType
from smtrading.engine.quotesource import QuoteSource
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Account, AccountBalance, AccountKind
from smtrading.portfolios.locks import lock_broker_link
from smtrading.portfolios.models import Portfolio
from smtrading.risk.live_checks import LIVE_CASH_MARGIN_PCT, STALE_KEY

log = logging.getLogger(__name__)

LIVE_RECONCILE_INTERVAL_S = 60
CASH_TOLERANCE_EUR = Decimal("0.50")
CASH_TOLERANCE_PCT = Decimal("0.001")
FX_BAND_PCT = Decimal("0.01")
QTY_EPSILON = Decimal("1e-8")
UNEXPLAINED_ALERT_AFTER = timedelta(minutes=30)
HEARTBEAT = timedelta(hours=1)
INFLIGHT = "ordine in corso"
NO_FX = "cambio non disponibile"
NO_PRICE = "prezzo non disponibile"
UNEXPLAINED_KIND = "reconcile_unexplained"
_AUTH = re.compile(r"HTTP (401|403)\b|credenziali non valide")  # Alpaca, Trading 212
_RATE = re.compile(r"HTTP 429\b")  # Alpaca (Trading 212 raises RateLimited)
_PREC = 60


class LiveBrokerUnreachable(Exception):
    """The broker calls of a reconciliation failed (network, 5xx...). `auth`: the broker
    refused the credentials (HTTP 401/403)."""

    def __init__(self, message: str, *, auth: bool = False) -> None:
        super().__init__(message)
        self.auth = auth


class LiveBrokerRateLimited(Exception):
    """The broker refused a call for its rate limit (HTTP 429): retried next pass."""


def cash_tolerance(ours: Decimal, eur_in_ccy: Decimal) -> Decimal:
    """max(0,50 € in the account currency, 0,1% of our cash)."""
    return max(CASH_TOLERANCE_EUR * eur_in_ccy, ours * CASH_TOLERANCE_PCT)


def _num(v: Decimal) -> str:
    return format(v.normalize(), "f") if v != 0 else "0"


@dataclass
class _Buy:
    symbol: str
    currency: str
    qty: Decimal | None  # open quantity (None: a notional order)
    price: Decimal | None  # limit/stop price (None: a market order, priced by quote)
    notional: Decimal | None  # open amount of a notional order


@dataclass
class _State:
    portfolio_ids: list[UUID]
    positions: dict[str, Decimal] = field(default_factory=dict)
    cash: dict[str, Decimal] = field(default_factory=dict)
    open_buy: dict[str, Decimal] = field(default_factory=dict)
    open_sell: dict[str, Decimal] = field(default_factory=dict)
    unbounded: set[str] = field(default_factory=set)
    buys: dict[UUID, _Buy] = field(default_factory=dict)


async def _read_state(session: AsyncSession, link_id: UUID) -> _State:
    portfolio_ids = list(await session.scalars(
        select(Portfolio.id).where(Portfolio.broker_link_id == link_id).order_by(Portfolio.id)
    ))
    state = _State(portfolio_ids)
    if not portfolio_ids:
        return state
    rows = await session.execute(
        select(Account.kind, Account.asset, AccountBalance.balance)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .where(Account.portfolio_id.in_(portfolio_ids),
               Account.kind.in_((AccountKind.position, AccountKind.cash)))
    )
    for kind, asset, balance in rows:
        target = state.positions if kind == AccountKind.position else state.cash
        target[asset] = target.get(asset, Decimal(0)) + balance
    state.positions = {s: q for s, q in state.positions.items() if q != 0}
    orders = (await session.execute(
        select(Order, Instrument.currency)
        .join(Instrument, Instrument.symbol == Order.symbol)
        .where(
            or_(Order.portfolio_id.in_(portfolio_ids), Order.broker_link_id == link_id),
            Order.status.in_(OPEN_STATUSES),
            or_(Order.broker_order_id.is_not(None), Order.broker_sent_at.is_not(None)),
        )
    )).all()
    for order, currency in orders:
        filled = order.filled_qty or Decimal(0)
        qty = None if order.qty is None else max(order.qty - filled, Decimal(0))
        if qty is None:
            state.unbounded.add(order.symbol)
        else:
            side = state.open_buy if order.side == OrderSide.buy else state.open_sell
            side[order.symbol] = side.get(order.symbol, Decimal(0)) + qty
        if order.side == OrderSide.buy:
            price = (order.limit_price if order.type in (OrderType.limit, OrderType.stop_limit)
                     else order.stop_price if order.type == OrderType.stop else None)
            notional = None
            if order.notional is not None:
                done = filled * (order.avg_fill_price or Decimal(0))
                notional = max(order.notional - done, Decimal(0))
            state.buys[order.id] = _Buy(order.symbol, currency, qty, price, notional)
    return state


async def _rates(qs: QuoteSource | None, ccys: set[str], ccy: str) -> dict[str, Decimal] | None:
    """Units of `ccy` per unit of each currency; None if a rate is missing."""
    rates: dict[str, Decimal] = {ccy: Decimal(1)}
    try:
        for c in ccys - {ccy}:
            if qs is None:
                return None
            rates[c] = await qs.get_fx_rate(c, ccy)
    except Exception as e:  # noqa: BLE001  (no rate: the cash is not compared)
        log.warning("live reconciliation: fx rate to %s unavailable: %s", ccy, e)
        return None
    return rates


@dataclass
class _Money:
    ours: Decimal  # our cash, in the account currency
    eur: Decimal  # 1 EUR in the account currency
    foreign: Decimal  # our cash held in other currencies, converted
    buys: Decimal | None  # what the open buys may still cost (None: a price is missing)


async def _money(qs: QuoteSource | None, cash: dict[str, Decimal], buys: list[_Buy],
                 ccy: str) -> _Money | None:
    """Our cash and the open buys' cost in the account currency `ccy` (no transaction
    open); None if an FX rate is missing."""
    rates = await _rates(qs, {*cash, "EUR", *(b.currency for b in buys)}, ccy)
    if rates is None:
        return None
    with localcontext() as ctx:
        ctx.prec = _PREC
        ours = sum((amount * rates[c] for c, amount in cash.items()), Decimal(0))
        foreign = sum((abs(amount) * rates[c] for c, amount in cash.items() if c != ccy),
                      Decimal(0))
    cost: Decimal | None = Decimal(0)
    margin = 1 + LIVE_CASH_MARGIN_PCT / 100
    for b in buys:
        value = b.notional
        if value is None:
            price = b.price
            if price is None and qs is not None:
                try:
                    quote = (await qs.get_quotes([b.symbol])).get(b.symbol)
                except Exception:  # noqa: BLE001  (no price: the cash is not compared)
                    quote = None
                price = None if quote is None else (quote.ask or quote.last)
            if price is None or b.qty is None:
                cost = None
                break
            value = b.qty * price
        with localcontext() as ctx:
            ctx.prec = _PREC
            cost += value * margin * rates[b.currency]
    return _Money(ours, rates["EUR"], foreign, cost)


async def reconcile_live(
    sessionmaker: async_sessionmaker[AsyncSession], link_id: UUID, http_factory, *,
    qs: QuoteSource | None = None,
) -> Reconciliation | None:
    """Reconcile a live link (live_state not null) with its linked portfolio; None (no
    broker call) for any other link or without a linked portfolio. Broker failures raise
    LiveBrokerUnreachable (LiveBrokerRateLimited for HTTP 429). The returned row (the
    first linked portfolio's) is the current outcome; it is stored only on a change or
    as the hourly heartbeat."""
    async with sessionmaker() as session:
        link = await session.get(BrokerLink, link_id)
        if link is None or link.live_state is None:
            return None
        before = await _read_state(session, link_id)
        if not before.portfolio_ids:
            return None
        kind, fingerprint = link.kind, link.account_fingerprint
        config = decrypt_live_config(link)  # keys registered for log masking
        instruments = list(await session.scalars(
            select(Instrument).where(Instrument.symbol.in_(sorted(before.positions)))
        )) if before.positions else []
        read_at = await session.scalar(select(func.now()))

    http = http_factory(link_id)
    try:
        adapter = adapter_class(kind)(config, http)
        await adapter.prepare(instruments)
        snap = await adapter.get_account()
        broker = await adapter.positions_after_account()  # T212: the positions just read
    except Exception as e:
        if isinstance(e, RateLimited) or _RATE.search(str(e)):
            raise LiveBrokerRateLimited(str(e)[:500]) from e
        raise LiveBrokerUnreachable(str(e)[:500], auth=bool(_AUTH.search(str(e)))) from e
    finally:
        await http.aclose()

    async with sessionmaker() as session:
        after = await _read_state(session, link_id)
    buys = list({**before.buys, **after.buys}.values())
    money = await _money(qs, after.cash, buys, snap.currency)  # no transaction open

    async with sessionmaker() as session:
        link = await lock_broker_link(session, link_id)  # R8.9: the link first
        after = await _read_state(session, link_id)
        pids = after.portfolio_ids or before.portfolio_ids
        if fingerprint is not None and account_fingerprint(kind, snap.account_id) != fingerprint:
            row = await _account_changed(session, link, pids)
            await session.commit()
            return row
        new = snapshot_record(snap)
        old = link.last_account_snapshot if isinstance(link.last_account_snapshot, dict) else {}
        if _stale_since(old, read_at):  # a buy was sent after this read: still stale
            new[STALE_KEY] = old[STALE_KEY]
        link.last_account_snapshot = new
        link.last_snapshot_at = await session.scalar(select(func.now()))
        now = link.last_snapshot_at
        diffs, position_diff, cash_short = _compare(before, after, broker, snap, money)
        rows = []
        for pid in pids:
            rows.append(await _record(session, link, pid, diffs, now,
                                      position_diff=position_diff, cash_short=cash_short))
        if kind == "alpaca_live" and snap.account_type == "margin":  # R8.10
            for pid in pids:
                await trip_reduce_only(session, pid, cause="margin_account",
                                       details={"link_id": link_id,
                                                "account_type": snap.account_type})
        await session.commit()
    return rows[0] if rows else None


def _stale_since(snapshot: dict, read_at: datetime) -> bool:
    at = snapshot.get(STALE_KEY)
    if not at:
        return False
    try:
        return datetime.fromisoformat(str(at)) >= read_at
    except ValueError:
        return True


def _compare(before: _State, after: _State, broker: dict[str, Decimal],
             snap: AccountSnapshot, money: _Money | None) -> tuple[list[dict], bool, bool]:
    diffs: list[dict] = []
    position_diff = False
    unbounded = before.unbounded | after.unbounded
    for symbol in sorted(set(after.positions) | set(broker) | unbounded):
        ours, theirs = after.positions.get(symbol, Decimal(0)), broker.get(symbol, Decimal(0))
        if abs(ours - theirs) <= QTY_EPSILON:
            continue
        if symbol in unbounded:
            diffs.append({"symbol": symbol, "skipped": INFLIGHT})
            continue
        buy = max(before.open_buy.get(symbol, Decimal(0)), after.open_buy.get(symbol, Decimal(0)))
        sell = max(before.open_sell.get(symbol, Decimal(0)),
                   after.open_sell.get(symbol, Decimal(0)))
        if theirs - buy - QTY_EPSILON <= ours <= theirs + sell + QTY_EPSILON:
            diffs.append({"symbol": symbol, "ours": _num(ours), "broker": _num(theirs),
                          "explained": INFLIGHT})
            continue
        diffs.append({"symbol": symbol, "ours": _num(ours), "broker": _num(theirs)})
        position_diff = True
    cash_short = False
    if money is None:
        diffs.append({"cash": {"skipped": NO_FX}})
    elif money.buys is None:
        diffs.append({"cash": {"skipped": NO_PRICE}})
    else:
        with localcontext() as ctx:
            ctx.prec = _PREC
            tolerance = (cash_tolerance(money.ours, money.eur) + money.foreign * FX_BAND_PCT
                         + money.buys)
        if snap.cash_available < money.ours - tolerance:
            cash_short = True
            diffs.append({"cash": {"ours": _num(money.ours), "broker": _num(snap.cash_available),
                                   "currency": snap.currency, "tolerance": _num(tolerance)}})
    return diffs, position_diff, cash_short


# -- recording: debounce, changes only, unexplained for too long ---------------------------

def _key(entry: dict) -> str:
    return f"symbol:{entry['symbol']}" if "symbol" in entry else "cash"


def _pending(entry: dict) -> str | None:
    """'skipped' / 'explained' for an entry that is not (yet) a diff, else None."""
    if "symbol" in entry:
        return "skipped" if "skipped" in entry else "explained" if "explained" in entry else None
    return "skipped" if "skipped" in entry["cash"] else None


def _actionable(diffs: list[dict]) -> list:
    """What the debounce compares: the position diffs and whether cash is short."""
    out: list = []
    for d in diffs:
        if _pending(d) is not None:
            continue
        out.append(["cash"] if "cash" in d else [d["symbol"], d["ours"], d["broker"]])
    return out


def _signature(diffs: list[dict]) -> list:
    """The outcome without the cash amounts (which move with every FX tick)."""
    out: list = []
    for d in diffs:
        if "cash" in d:
            out.append(["cash", d["cash"].get("skipped", "short"), d.get("since")])
        else:
            out.append([d["symbol"], d.get("ours"), d.get("broker"), _pending(d), d.get("since")])
    return out


async def _previous(session: AsyncSession, link_id: UUID, pid: UUID) -> Reconciliation | None:
    return await session.scalar(
        select(Reconciliation)
        .where(Reconciliation.broker_link_id == link_id, Reconciliation.portfolio_id == pid)
        .order_by(Reconciliation.ts.desc(), Reconciliation.id.desc()).limit(1))


async def _record(session: AsyncSession, link: BrokerLink, pid: UUID, diffs: list[dict],
                  now: datetime, *, position_diff: bool, cash_short: bool) -> Reconciliation:
    previous = await _previous(session, link.id, pid)
    prev_diffs = previous.diffs if previous is not None and isinstance(previous.diffs, list) \
        else []
    since = {(_key(d), _pending(d)): d.get("since") for d in prev_diffs if "account" not in d
             and _pending(d) is not None}
    entries = []
    for d in diffs:
        state = _pending(d)
        if state is not None:
            d = {**d, "since": since.get((_key(d), state)) or now.isoformat()}
        entries.append(d)
    actionable = _actionable(entries)
    confirmed = bool(actionable) and previous is not None and not previous.ok \
        and _actionable(prev_diffs) == actionable
    row = Reconciliation(broker_link_id=link.id, portfolio_id=pid, diffs=entries,
                         ok=not actionable)
    changed = previous is None or _signature(prev_diffs) != _signature(entries) \
        or previous.ok != row.ok or now - previous.ts >= HEARTBEAT
    if changed:
        session.add(row)
        await session.flush()
    if confirmed:  # the same diff on two passes in a row (R8.7)
        if position_diff:
            await trip_reduce_only(session, pid, cause="reconcile_diff",
                                   details={"link_id": link.id, "diffs": entries})
        if cash_short:
            await trip_reduce_only(session, pid, cause="cash_shortfall",
                                   details={"link_id": link.id, "diffs": entries})
    if actionable:
        log.warning("live link %s: reconciliation differs (%s): %s", link.id,
                    "confirmed" if confirmed else "first pass", entries)
    for d in entries:
        await _unexplained_alert(session, link, pid, d, now)
    return row


async def _unexplained_alert(session: AsyncSession, link: BrokerLink, pid: UUID, entry: dict,
                             now: datetime) -> None:
    """One critical alert per episode of a symbol (or the cash) skipped or explained by
    open orders for more than UNEXPLAINED_ALERT_AFTER."""
    state = _pending(entry)
    if state is None:
        return
    since = datetime.fromisoformat(entry["since"])
    if now - since <= UNEXPLAINED_ALERT_AFTER:
        return
    key = _key(entry)
    sent = await session.scalar(select(Activity.id).where(
        Activity.type == f"live.{UNEXPLAINED_KIND}", Activity.portfolio_id == pid,
        Activity.data["key"].astext == key, Activity.data["since"].astext == entry["since"],
    ).limit(1))
    if sent is not None:
        return
    what = "la liquidità" if key == "cash" else entry["symbol"]
    await live_alert(
        session, kind=UNEXPLAINED_KIND,
        summary=f"Riconciliazione: {what} non verificabile da oltre 30 minuti",
        portfolio_id=pid, link_id=link.id,
        data={"key": key, "since": entry["since"], "state": state, "entry": entry},
    )


async def _account_changed(session: AsyncSession, link: BrokerLink,
                           pids: list[UUID]) -> Reconciliation | None:
    """Another account behind the keys: the activation service's `account_changed`
    (disarmed when it was not `configured`, portfolio in reduce-only, one critical
    `live.account_changed`) the first time; afterwards only the row is recorded."""
    rows = [Reconciliation(broker_link_id=link.id, portfolio_id=pid,
                           diffs=[{"account": "cambiato"}], ok=False) for pid in pids]
    already = (await _previous(session, link.id, pids[0])) if pids else None
    if already is None or already.diffs != [{"account": "cambiato"}]:
        await account_changed(session, link, "system")
        session.add_all(rows)
        await session.flush()
        log.warning("live link %s: account changed, back to configured", link.id)
    return rows[0] if rows else None
