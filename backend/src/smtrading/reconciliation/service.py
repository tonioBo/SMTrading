"""Reconciliation with the broker (plan 21): for every portfolio linked to a broker (paper,
demo or real money), SMTrading's positions, cash and open orders against the broker
account's, read with the adapter's read calls only.

`check_link` (one link, every non-archived portfolio linked to it):

1. a short session reads our side: the summed positions and cash of the linked
   portfolios, the orders routed to the link (open, or with a broker id);
2. with no transaction open, the broker calls: `prepare`, `get_account`,
   `positions_after_account`, `get_open_orders`. The adapter's client is wrapped in
   ReadOnlyHttp: anything but a GET/HEAD (and the eToro eligibility POST, a read) is
   refused before it leaves, so a check can never place or cancel an order. A real-money
   link goes through its live gate as well (GET always passes);
3. FX rates and the prices of the open buys (no transaction open);
4. one transaction writes a ReconciliationCheck per portfolio (compare.py), under a
   per-link advisory lock.

A broker failure (network, auth, rate limit, read-only refusal) writes an `unverifiable`
row with the error and no items: no alert, and the next check carries the mismatch
streaks over it (as if it had not happened).

Persistence, the 2-check rule: a mismatch item carries `streak` (consecutive verifiable
checks with the same key in mismatch) and `since`. At `streak` ≥ CONFIRM_CHECKS it is
confirmed: one `broker.reconciliation_mismatch` activity on the portfolio (warning, not
sent to Telegram by the planner; visible to the agents of that portfolio, no simulation
words, no broker or link name) and one Telegram message to the owner (category system),
once per portfolio, item key and local day (Europe/Rome; the dedup reads the activity
rows, so a restart sends nothing again). Nothing is ever corrected: the Risk Manager
decides (agent docs).
"""

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.activity.mapping import ActivityDraft
from smtrading.activity.models import Activity, ActivityLevel
from smtrading.activity.service import record_activity
from smtrading.brokers.base import AccountSnapshot, BrokerOpenOrder
from smtrading.brokers.live import LIVE_KINDS
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.service import open_adapter
from smtrading.common.config import get_settings
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.logsetup import redact_log_text
from smtrading.common.money import decimal_to_str
from smtrading.engine.models import OPEN_STATUSES, Order, OrderSide, OrderType
from smtrading.engine.quotesource import QuoteSource
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Account, AccountBalance, AccountKind
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.reconciliation.compare import (
    MISMATCH,
    UNVERIFIABLE,
    CashView,
    OurOrder,
    compare_cash,
    compare_orders,
    compare_positions,
    overall,
)
from smtrading.reconciliation.models import ReconciliationCheck
from smtrading.telegram.outbox import enqueue

log = logging.getLogger(__name__)

CONFIRM_CHECKS = 2
ALERT_TYPE = "broker.reconciliation_mismatch"
READ_ONLY_MESSAGE = "riconciliazione: solo letture verso il broker"
MAX_ERROR = 500


class ReadOnlyHttp:
    """The adapter's client during a reconciliation: only reads reach the broker."""

    def __init__(self, inner) -> None:
        self._inner = inner

    async def request(self, method: str, url, **kwargs):
        m = method.upper()
        path = str(url).split("?", 1)[0]
        read_post = m == "POST" and path.endswith("/eligibility") and not (
            kwargs.get("extensions") or {}).get("smt_order_id")
        if m not in ("GET", "HEAD") and not read_post:
            raise RuntimeError(READ_ONLY_MESSAGE)
        return await self._inner.request(method, url, **kwargs)


@dataclass
class _Ours:
    portfolios: list[tuple[UUID, str]]
    positions: dict[str, Decimal]
    cash: dict[str, Decimal]
    orders: list[OurOrder]
    currencies: dict[str, str]  # symbol -> instrument currency


def _our_order(o: Order) -> OurOrder:
    filled = o.filled_qty or Decimal(0)
    qty = None if o.qty is None else max(o.qty - filled, Decimal(0))
    # plan 29: a synthetic order is at the broker only once triggered, as a market order
    # (resting, it is kept here by design: never read, it has no broker_link_id)
    kind = OrderType.market if o.synthetic else o.type
    return OurOrder(
        id=o.id, portfolio_id=o.portfolio_id, symbol=o.symbol, side=str(o.side),
        type=str(kind), open=o.status in OPEN_STATUSES, qty=qty,
        limit_price=o.limit_price if kind in (OrderType.limit, OrderType.stop_limit)
        else None,
        stop_price=o.stop_price if kind in (OrderType.stop, OrderType.stop_limit) else None,
        broker_order_id=o.broker_order_id, broker_status=o.broker_status,
        sent=o.broker_order_id is not None or o.broker_sent_at is not None,
        updated_at=o.updated_at)


async def _read_ours(session: AsyncSession, link_id: UUID) -> _Ours:
    portfolios = [(pid, name) for pid, name in (await session.execute(
        select(Portfolio.id, Portfolio.name)
        .where(Portfolio.broker_link_id == link_id,
               Portfolio.status != PortfolioStatus.archived)
        .order_by(Portfolio.id))).all()]
    pids = [p for p, _ in portfolios]
    ours = _Ours(portfolios, {}, {}, [], {})
    if not pids:
        return ours
    rows = await session.execute(
        select(Account.kind, Account.asset, AccountBalance.balance)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .where(Account.portfolio_id.in_(pids),
               Account.kind.in_((AccountKind.position, AccountKind.cash))))
    for kind, asset, balance in rows:
        target = ours.positions if kind == AccountKind.position else ours.cash
        target[asset] = target.get(asset, Decimal(0)) + balance
    ours.positions = {s: q for s, q in ours.positions.items() if q != 0}
    orders = await session.scalars(
        select(Order).where(Order.broker_link_id == link_id,
                            Order.status.in_(OPEN_STATUSES)))
    ours.orders = [_our_order(o) for o in orders]
    symbols = set(ours.positions) | {o.symbol for o in ours.orders}
    if symbols:
        ours.currencies = dict((await session.execute(
            select(Instrument.symbol, Instrument.currency)
            .where(Instrument.symbol.in_(sorted(symbols))))).all())
    return ours


async def _closed_by_broker_id(session: AsyncSession, link_id: UUID,
                               broker: list[BrokerOpenOrder], known: set[str]
                               ) -> list[OurOrder]:
    """Our orders (any status) behind the broker's open orders not already read."""
    ids = sorted({b.broker_order_id for b in broker} - known)
    if not ids:
        return []
    return [_our_order(o) for o in await session.scalars(
        select(Order).where(Order.broker_link_id == link_id,
                            Order.broker_order_id.in_(ids)))]


async def _cash_view(qs: QuoteSource | None, ours: _Ours, snap: AccountSnapshot) -> CashView:
    """Our cash and the open buys' cost in the account currency (no transaction open)."""
    ccy = snap.currency
    view = CashView(currency=ccy, broker=snap.cash_available, ours=None)
    buys = [o for o in ours.orders if o.open and o.sent and o.side == OrderSide.buy.value]
    needed = {c for c, v in ours.cash.items() if c != ccy and v != 0} | {
        ours.currencies.get(o.symbol, ccy) for o in buys} - {ccy}
    rates = {ccy: Decimal(1)}
    try:
        for c in sorted(needed):
            if qs is None:
                return view
            rates[c] = await qs.get_fx_rate(c, ccy)
    except Exception as e:  # noqa: BLE001  (no rate: the cash is not verifiable)
        log.info("reconciliation: fx rate to %s unavailable: %s", ccy, e)
        return view
    with localcontext() as ctx:
        ctx.prec = 60
        view.ours = sum((v * rates[c] for c, v in ours.cash.items() if c in rates),
                        Decimal(0))
        view.foreign = sum((abs(v) * rates[c] for c, v in ours.cash.items()
                            if c != ccy and c in rates), Decimal(0))
    cost: Decimal | None = Decimal(0)
    for o in buys:
        price = o.limit_price or o.stop_price
        if o.qty is None:
            cost = None
            break
        if price is None and qs is not None:
            try:
                quote = (await qs.get_quotes([o.symbol])).get(o.symbol)
            except Exception:  # noqa: BLE001  (no price: the cash check is a warning)
                quote = None
            price = None if quote is None else (quote.ask or quote.last)
        if price is None:
            cost = None
            break
        cost += o.qty * price * rates[ours.currencies.get(o.symbol, ccy)]
    view.open_buys = cost
    return view


def _num(value: str | None) -> str:
    return "—" if value is None else decimal_to_str(Decimal(value)).replace(".", ",")


def _mismatch_text(item: dict) -> str:
    return (f"{item['label']}: SMTrading {_num(item['ours'])}, broker {_num(item['broker'])}"
            f" ({item['note']})")


async def _previous(session: AsyncSession, pid: UUID) -> ReconciliationCheck | None:
    return await session.scalar(
        select(ReconciliationCheck)
        .where(ReconciliationCheck.portfolio_id == pid,
               ReconciliationCheck.status != UNVERIFIABLE)
        .order_by(ReconciliationCheck.ts.desc(), ReconciliationCheck.id.desc()).limit(1))


def _day(now: datetime) -> str:
    """The local day (SMTrading's time zone) of an alert, kept in its data."""
    return now.astimezone(ZoneInfo(get_settings().tz)).date().isoformat()


async def _alerted_today(session: AsyncSession, pid: UUID, key: str, now: datetime) -> bool:
    return await session.scalar(select(Activity.id).where(
        Activity.type == ALERT_TYPE, Activity.portfolio_id == pid,
        Activity.data["key"].astext == key,
        Activity.data["day"].astext == _day(now)).limit(1)) is not None


def alert_draft(item: dict, check_id: int, now: datetime) -> ActivityDraft:
    """The agents' activity of a confirmed mismatch (no simulation words, no broker or
    link name: only what SMTrading and the broker say)."""
    return ActivityDraft(
        ALERT_TYPE, ActivityLevel.warning, False,
        f"Riconciliazione con il broker: {_mismatch_text(item)}, da {item['streak']} "
        "controlli di fila. Nessuna correzione automatica: decide il Risk Manager.",
        {"key": item["key"], "kind": item["kind"], "symbol": item.get("symbol"),
         "ours": item["ours"], "broker": item["broker"], "diff": item["diff"],
         "note": item["note"], "streak": item["streak"], "since": item["since"],
         "check_id": check_id, "day": _day(now)},
    )


def _for_portfolio(items: list[dict], pid: UUID) -> list[dict]:
    key = str(pid)
    return [dict(i) for i in items if i.get("portfolio_id", key) == key]


async def _write(session: AsyncSession, link: BrokerLink, ours: _Ours, items: list[dict] | None,
                 meta: dict, error: str | None, trigger: str, now: datetime
                 ) -> list[ReconciliationCheck]:
    rows: list[ReconciliationCheck] = []
    names = ", ".join(name for _, name in ours.portfolios)
    telegrammed: set[str] = set()
    for pid, _ in ours.portfolios:
        if items is None:
            row = ReconciliationCheck(portfolio_id=pid, broker_link_id=link.id,
                                      status=UNVERIFIABLE, trigger=trigger, items=[],
                                      meta=meta, error=error, ts=now)
            session.add(row)
            rows.append(row)
            continue
        mine = _for_portfolio(items, pid)
        previous = await _previous(session, pid)
        before = {i["key"]: i for i in (previous.items if previous is not None else [])
                  if i.get("status") == MISMATCH}
        for item in mine:
            if item["status"] != MISMATCH:
                continue
            last = before.get(item["key"])
            item["streak"] = int(last.get("streak") or 1) + 1 if last else 1
            item["since"] = (last.get("since") if last else None) or now.isoformat()
        confirmed = []
        for item in mine:
            if (item["status"] == MISMATCH and item["streak"] >= CONFIRM_CHECKS
                    and not await _alerted_today(session, pid, item["key"], now)):
                item["alerted"] = True
                confirmed.append(item)
        row = ReconciliationCheck(portfolio_id=pid, broker_link_id=link.id,
                                  status=overall(mine), trigger=trigger, items=mine,
                                  meta=meta, ts=now)
        session.add(row)
        await session.flush()
        rows.append(row)
        for item in confirmed:
            record_activity(session, alert_draft(item, row.id, now), portfolio_id=pid,
                            agent_id=None)
            if item["key"] not in telegrammed:
                telegrammed.add(item["key"])
                await enqueue(session, source="system", category="system", level="warning",
                              text=f"⚠️ Riconciliazione broker — {names}: "
                                   f"{_mismatch_text(item)}. Da {item['streak']} controlli "
                                   "di fila; nessuna correzione automatica.")
            log.warning("reconciliation mismatch confirmed: portfolio %s %s", pid,
                        item["key"])
    await session.flush()
    return rows


async def check_link(
    sessionmaker: async_sessionmaker[AsyncSession], link_id: UUID, qs: QuoteSource | None, *,
    http=None, live_http_factory: Callable | None = None, trigger: str = "schedule",
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[ReconciliationCheck]:
    """Check one link (module docstring); one committed row per non-archived linked
    portfolio ([] without any). A real-money link's client comes from
    `live_http_factory` (the live gate) when given, else open_adapter builds one."""
    async with contextlib.AsyncExitStack() as stack:
        async with sessionmaker() as session:
            link = await session.get(BrokerLink, link_id)
            if link is None:
                raise SMTError(ErrorCode.NOT_FOUND, "collegamento broker non trovato",
                               {"broker_link_id": str(link_id)})
            ours = await _read_ours(session, link_id)
            if not ours.portfolios:
                return []
            instruments = list(await session.scalars(select(Instrument).where(
                Instrument.symbol.in_(sorted(set(ours.positions)
                                             | {o.symbol for o in ours.orders})))))
            client = http
            if link.kind in LIVE_KINDS and live_http_factory is not None:
                client = await stack.enter_async_context(live_http_factory(link_id))
            meta = {"link_name": link.name, "link_portfolios": len(ours.portfolios)}
            error: str | None = None
            try:
                adapter = await stack.enter_async_context(
                    open_adapter(session, link_id, client, sessionmaker=sessionmaker))
            except Exception as e:  # noqa: BLE001  (an unreadable config: unverifiable)
                adapter, error = None, redact_log_text(str(e))[:MAX_ERROR] or type(e).__name__
        snap = positions = broker_orders = None
        dust: dict[str, Decimal] = {}
        if adapter is not None:
            inner = adapter.http
            adapter.http = ReadOnlyHttp(inner)
            try:
                await adapter.prepare(instruments)
                snap = await adapter.get_account()
                positions = await adapter.positions_after_account()
                broker_orders = await adapter.get_open_orders()
                symbols = set(ours.positions) | set(positions)
                await adapter.load_fee_dust(symbols)
                dust = {sym: adapter.fee_dust(sym, max(
                    abs(ours.positions.get(sym, Decimal(0))),
                    abs(positions.get(sym, Decimal(0))))) for sym in symbols}
            except Exception as e:  # noqa: BLE001  (any broker failure: unverifiable)
                error = redact_log_text(str(e))[:MAX_ERROR] or type(e).__name__
                log.warning("reconciliation of broker link %s not verifiable: %s",
                            link_id, error)
            finally:
                adapter.http = inner
    items: list[dict] | None = None
    if error is None:
        cash = await _cash_view(qs, ours, snap)
        meta.update(currency=snap.currency, orders_checked=broker_orders is not None)
    now = clock()
    async with sessionmaker() as session, session.begin():
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                              {"k": f"reconciliation:{link_id}"})
        if error is None:
            items = compare_positions(ours.positions, positions, ours.orders, dust)
            items.append(compare_cash(cash))
            if broker_orders is not None:
                known = {o.broker_order_id for o in ours.orders if o.broker_order_id}
                closed = await _closed_by_broker_id(session, link_id, broker_orders, known)
                items += compare_orders(ours.orders + closed, broker_orders, now)
        link = await session.get(BrokerLink, link_id)
        rows = await _write(session, link, ours, items, meta, error, trigger, now)
    return rows


async def check_all(sessionmaker: async_sessionmaker[AsyncSession], qs: QuoteSource | None, *,
                    http=None, live_http_factory: Callable | None = None,
                    trigger: str = "schedule",
                    clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> int:
    """Check every enabled link with a non-archived linked portfolio; the number of rows
    in mismatch. A link that fails unexpectedly is logged and the others go on."""
    async with sessionmaker() as session:
        link_ids = list(await session.scalars(
            select(BrokerLink.id).where(BrokerLink.enabled.is_(True)).where(
                select(Portfolio.id).where(Portfolio.broker_link_id == BrokerLink.id,
                                           Portfolio.status != PortfolioStatus.archived)
                .exists()).order_by(BrokerLink.name)))
    mismatched = 0
    for link_id in link_ids:
        try:
            rows = await check_link(sessionmaker, link_id, qs, http=http,
                                    live_http_factory=live_http_factory, trigger=trigger,
                                    clock=clock)
        except Exception:
            log.exception("reconciliation of broker link %s failed", link_id)
            continue
        mismatched += sum(1 for r in rows if r.status == MISMATCH)
    return mismatched
