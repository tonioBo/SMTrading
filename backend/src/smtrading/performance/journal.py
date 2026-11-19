"""«Diario operazioni»: one row per trade and, for one trade, its whole story.

Trades are the ones of the «Strategie» page (`performance.strategies.fifo_trades`: FIFO
lots per portfolio+symbol over the fills of the non-archived portfolios, one opening
order = one trade, attributed to a strategy by its opening order with
`strategies.attribution`). The key of a trade is its opening order id; when the same
order opens a second trade (filled again after its first lots were closed) the key is
`<order id>.2`, `.3`… in time order.

Row fields (EUR at the current FX rate, like the «Strategie» page; None = unknown, «—»):
- `pnl_eur` = realised + unrealised − costs; `pnl_pct` = pnl_eur over the entry value
  (entry price × opened qty, in EUR); `holding_s` = from the first entry fill to the
  fill that closed it (now for an open trade);
- `outcome`: `open` while some qty is open, else `win` when pnl_eur > 0, else `loss`
  (the «Strategie» page's rule);
- `exit_reason`: the reason of the fill that closed it (the last exit of an open trade
  closed in part); `exit_reasons` every distinct reason of its exits, in order. From the
  exit fill's order: `stop` (role stop_loss or a stop/trailing order), `target` (role
  take_profit or a limit OCO leg), `liquidation` (margin), `plan_close` (SMTrading's close
  at the end of a «piano»: a `plan_closures` order or a system reduce-only order; an
  agents' close whose decision says «fine piano»), `admin` (portal / «Chiudi tutte»),
  `strategy_exit` (an agents' close, CHIUDI, whose proposal or approval of the same
  portfolio and symbol in the day before speaks of the strategy's exit rule),
  `agent_close` (any other agents' reduce-only close), `system` (another system order),
  `other` (an opposite order that is not a close).

Filters of the list: `period` (week | 1m | 3m | all, `agent_results.period_bounds`: a
trade is in the period when it was open at some time in it: opened before its end and
not closed before its start; open trades are always in), `portfolio_id`, `strategy`
(STR ref, `none` = «Senza strategia»), `symbol`, `outcome` (win | loss | open).
Newest opened first.

The detail adds the timeline: the decisions of the proposal (`details.ref` = the entry's
client_order_id without its `-rN` resend suffix: proposal with its details, i.e. the
signal and the indicator values, approval, veto), the other decisions naming the ref or
one of the trade's orders, the lessons (`kind=lesson`) naming them, the orders (entry,
bracket exits, OCO legs placed later, closes; cancel reasons from the `order.canceled`
activity), their changes and cancels through MCP or the portal (`mcp_audit_log`), the
fills (price, qty, fee, FX conversion and FX fee from the fill's FX journal) and the
activity feed events of those orders.
"""

import json
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any

from sqlalchemy import Text, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.activity.models import Activity, Decision, DecisionKind
from smtrading.agents.models import Agent, McpAuditLog
from smtrading.common.errors import SMTError
from smtrading.engine.models import LIQUIDATION_ROLE, Fill, Order, OrderType
from smtrading.engine.quotesource import QuoteSource
from smtrading.goals.models import PlanClosure
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Account, AccountKind, Journal, JournalLine, JournalType
from smtrading.performance.strategies import (
    _PREC,
    FillIn,
    Trade,
    _fills,
    _money,
    _Rates,
    _sum,
    fifo_trades,
)
from smtrading.portfolios.models import Portfolio
from smtrading.signals.service import strategy_states
from smtrading.strategies.attribution import order_attributor
from smtrading.valuation.service import value_portfolio

NO_STRATEGY = "none"
OUTCOMES = ("win", "loss", "open")
EXIT_LABELS = {
    "stop": "Stop loss",
    "target": "Target",
    "liquidation": "Liquidazione (margine)",
    "plan_close": "Chiusura di fine piano",
    "admin": "Chiusura degli amministratori",
    "strategy_exit": "Uscita della strategia",
    "agent_close": "Chiusura degli agenti (CHIUDI)",
    "system": "Ordine del sistema",
    "other": "Ordine opposto",
}
ROLE_LABELS = {"entry": "ingresso", "take_profit": "target", "stop_loss": "stop loss",
               LIQUIDATION_ROLE: "liquidazione"}
CANCEL_REASONS = {
    "oco_sibling_filled": "eseguita l'altra gamba OCO",
    "nothing_to_reduce": "nessuna posizione da ridurre",
    "plan_grace": "fine piano: nessuna aggiunta",
}
BY_LABELS = {"agent": "agenti", "admin": "amministratori", "system": "sistema"}
_RESEND = re.compile(r"-r\d+$")
_STRATEGY_EXIT = re.compile(
    r"regola d['’]uscita|uscita (della |di )?strategia|segnale d['’]uscita|exit[_ ]rule",
    re.IGNORECASE)
_PLAN_END = re.compile(r"fine piano", re.IGNORECASE)
_HINT_WINDOW = timedelta(days=1)
_PCT = Decimal("0.01")
_STOP_TYPES = (OrderType.stop, OrderType.stop_limit, OrderType.trailing_stop)
_AUDIT_METHODS = ("modify_order", "cancel_order")


def base_ref(client_order_id: str | None) -> str | None:
    """The proposal id of an entry's client_order_id (`PROP-…-r1` → `PROP-…`)."""
    if not client_order_id or not client_order_id.strip():
        return None
    return _RESEND.sub("", client_order_id.strip())


def parse_key(key: str) -> tuple[uuid.UUID, int] | None:
    head, _, n = key.partition(".")
    try:
        order_id = uuid.UUID(head)
        nth = int(n) if n else 1
    except ValueError:
        return None
    return (order_id, nth) if nth >= 1 else None


def _keys(trades: list[Trade]) -> dict[int, str]:
    """id(trade) → its key (trades are in time order per opening order)."""
    seen: dict[uuid.UUID, int] = defaultdict(int)
    out = {}
    for t in sorted(trades, key=lambda t: t.opened_at):
        seen[t.order_id] += 1
        n = seen[t.order_id]
        out[id(t)] = str(t.order_id) if n == 1 else f"{t.order_id}.{n}"
    return out


def _text_of(d: Decision) -> str:
    return " ".join(x for x in (d.summary, d.subject or "", d.reason or "",
                                json.dumps(d.details or {}, default=str)) if x)


# --- exit reasons ------------------------------------------------------------------------


@dataclass
class _Hints:
    plan_orders: set[uuid.UUID]
    decisions: list[Decision]  # proposals / approvals of the portfolios, by id

    def agent_close(self, order: Order) -> str:
        lo = order.created_at - _HINT_WINDOW
        for d in reversed(self.decisions):
            if d.portfolio_id not in (order.portfolio_id, None):
                continue
            if not (lo <= d.created_at <= order.created_at + timedelta(minutes=5)):
                continue
            text = _text_of(d)
            if order.symbol not in (d.symbols or []) and order.symbol not in text:
                continue
            if _PLAN_END.search(text):
                return "plan_close"
            if _STRATEGY_EXIT.search(text):
                return "strategy_exit"
        return "agent_close"


def exit_reason(order: Order, hints: _Hints) -> str:
    if order.role == LIQUIDATION_ROLE:
        return "liquidation"
    if order.role == "stop_loss" or order.type in _STOP_TYPES:
        return "stop"
    if order.role == "take_profit":
        return "target"
    if order.id in hints.plan_orders:
        return "plan_close"
    if order.source == "admin":
        return "admin"
    if order.source == "system":
        return "plan_close" if order.reduce_only and order.type == OrderType.market \
            else "system"
    if order.type == OrderType.limit and order.oco_group_id is not None:
        return "target"
    if order.reduce_only:
        return hints.agent_close(order)
    return "other"


async def _hints(session: AsyncSession, orders: list[Order]) -> _Hints:
    ids = [o.id for o in orders]
    plan = set(await session.scalars(
        select(PlanClosure.order_id).where(PlanClosure.order_id.in_(ids)))) if ids else set()
    closes = [o for o in orders if o.reduce_only and o.source not in ("admin", "system")
              and o.role is None and o.type == OrderType.market]
    decisions: list[Decision] = []
    if closes:
        pids = {o.portfolio_id for o in closes}
        lo = min(o.created_at for o in closes) - _HINT_WINDOW
        decisions = list(await session.scalars(
            select(Decision).where(
                Decision.kind.in_((DecisionKind.proposal, DecisionKind.approval)),
                or_(Decision.portfolio_id.in_(pids), Decision.portfolio_id.is_(None)),
                Decision.created_at >= lo,
            ).order_by(Decision.id)))
    return _Hints(plan, decisions)


# --- rows --------------------------------------------------------------------------------


@dataclass
class TradeRow:
    key: str
    portfolio_id: uuid.UUID
    portfolio_name: str
    symbol: str
    currency: str
    direction: str
    strategy_ref: str | None
    strategy_name: str | None
    proposal_ref: str | None
    order_id: uuid.UUID
    opened_at: datetime
    closed_at: datetime | None
    outcome: str  # win | loss | open
    qty: Decimal
    open_qty: Decimal
    entry_price: Decimal
    exit_price: Decimal | None
    mark: Decimal | None
    realized_pnl_eur: Decimal | None
    unrealized_pnl_eur: Decimal | None
    costs_eur: Decimal | None
    pnl_eur: Decimal | None
    pnl_pct: Decimal | None
    holding_s: int
    exit_reason: str | None
    exit_reason_label: str | None
    exit_reasons: list[str] = field(default_factory=list)


@dataclass
class _Built:
    rows: list[TradeRow]
    trades: dict[str, Trade]
    orders: dict[uuid.UUID, Order]
    portfolios: dict[uuid.UUID, Portfolio]


async def _build(session: AsyncSession, qs: QuoteSource, *, redis=None,
                 portfolio_id: uuid.UUID | None = None, symbol: str | None = None,
                 now: datetime | None = None) -> _Built:
    now = now or datetime.now(UTC)
    fills, portfolios, orders = await _fills(session, portfolio_id=portfolio_id,
                                             symbol=symbol)
    trades = fifo_trades(fills)
    if not trades:
        return _Built([], {}, orders, portfolios)
    attribute = await order_attributor(session, [orders[t.order_id] for t in trades])
    cache: dict[uuid.UUID, str | None] = {}
    for t in trades:
        if t.order_id not in cache:
            cache[t.order_id] = await attribute(orders[t.order_id])
        t.strategy = cache[t.order_id]
    exit_orders = {f.order_id: orders[f.order_id] for t in trades
                   for f, _, kind in t.legs if kind == "exit"}
    hints = await _hints(session, list(exit_orders.values()))
    reasons = {oid: exit_reason(o, hints) for oid, o in exit_orders.items()}
    marks: dict[uuid.UUID, dict[str, Decimal]] = {}
    for pid in sorted({t.portfolio_id for t in trades if t.open_qty}, key=str):
        try:
            v = await value_portfolio(session, qs, pid, redis=redis)
        except SMTError:
            continue
        marks[pid] = {p.symbol: p.mark for p in v.positions}
    states = await strategy_states(session)
    rates = _Rates(qs)
    keys = _keys(trades)
    rows, by_key = [], {}
    for t in trades:
        mark = marks.get(t.portfolio_id, {}).get(t.symbol) if t.open_qty else None
        with localcontext() as ctx:
            ctx.prec = _PREC
            if t.open_qty == 0:
                unreal = Decimal(0)
            elif mark is None:
                unreal = None
            else:
                unreal = await rates.eur(t.unrealized(mark), t.currency)
            real = await rates.eur(t.realized, t.currency)
            costs = _sum([await rates.eur(a, c) for c, a in sorted(t.costs.items())])
            pnl = None if None in (real, unreal, costs) else real + unreal - costs
            entry_eur = await rates.eur(t.entry_value, t.currency)
            pct = (pnl / entry_eur * 100).quantize(_PCT) \
                if pnl is not None and entry_eur else None
        exits = [reasons[f.order_id] for f, _, kind in t.legs if kind == "exit"]
        distinct = list(dict.fromkeys(exits))
        last = exits[-1] if exits else None
        outcome = "open" if t.open_qty else ("win" if pnl is not None and pnl > 0 else "loss")
        state = states.get(t.strategy) if t.strategy else None
        key = keys[id(t)]
        end = t.closed_at or now
        rows.append(TradeRow(
            key=key, portfolio_id=t.portfolio_id,
            portfolio_name=portfolios[t.portfolio_id].name, symbol=t.symbol,
            currency=t.currency, direction=t.direction, strategy_ref=t.strategy,
            strategy_name=state.name if state else None,
            proposal_ref=t.client_order_id, order_id=t.order_id, opened_at=t.opened_at,
            closed_at=t.closed_at, outcome=outcome, qty=t.qty, open_qty=t.open_qty,
            entry_price=t.entry_price, exit_price=t.exit_price, mark=mark,
            realized_pnl_eur=_money(real), unrealized_pnl_eur=_money(unreal),
            costs_eur=_money(costs), pnl_eur=_money(pnl), pnl_pct=pct,
            holding_s=max(0, round((end - t.opened_at).total_seconds())),
            exit_reason=last, exit_reason_label=EXIT_LABELS.get(last) if last else None,
            exit_reasons=distinct,
        ))
        by_key[key] = t
    rows.sort(key=lambda r: (r.opened_at, r.key), reverse=True)
    return _Built(rows, by_key, orders, portfolios)


async def trade_rows(session: AsyncSession, qs: QuoteSource, *, redis=None,
                     now: datetime | None = None) -> list[TradeRow]:
    """Every trade, newest opened first (unfiltered: the API filters and pages)."""
    return (await _build(session, qs, redis=redis, now=now)).rows


# --- detail ------------------------------------------------------------------------------


async def _agent_names(session: AsyncSession, ids: set) -> dict[str, str]:
    clean = set()
    for v in ids:
        try:
            clean.add(uuid.UUID(str(v)))
        except (ValueError, TypeError):
            continue
    if not clean:
        return {}
    return {str(a.id): a.name for a in await session.scalars(
        select(Agent).where(Agent.id.in_(clean)))}


def _related_orders(trade: Trade, all_trades: list[Trade], candidates: list[Order],
                    ref: str | None, now: datetime) -> list[Order]:
    """The trade's orders among the portfolio+symbol's orders: its entry and the orders
    of its fills, the orders sent for the same proposal (resends), the bracket children,
    the exit-side orders placed while it was open (OCO legs, stops, closes) unless they
    opened another trade, and the OCO siblings of those."""
    by_id = {o.id: o for o in candidates}
    entry = by_id.get(trade.order_id)
    own = {f.order_id for f, _, _ in trade.legs}
    others = {t.order_id for t in all_trades if t is not trade} - own
    start = entry.created_at if entry is not None else trade.opened_at
    end = trade.closed_at or now
    entry_side = entry.side if entry is not None else None
    chosen: set[uuid.UUID] = set(own)
    for o in candidates:
        if o.id in others:
            continue
        resend = bool(ref) and base_ref(o.client_order_id) == ref
        exit_side = (entry_side is not None and o.side != entry_side
                     and start <= o.created_at <= end)
        if resend or exit_side:
            chosen.add(o.id)
    changed = True
    while changed:
        changed = False
        groups = {by_id[i].oco_group_id for i in chosen if i in by_id and by_id[i].oco_group_id}
        for o in candidates:
            if o.id in chosen or o.id in others:
                continue
            if o.parent_id in chosen or (o.oco_group_id and o.oco_group_id in groups):
                chosen.add(o.id)
                changed = True
    return sorted((by_id[i] for i in chosen if i in by_id), key=lambda o: (o.created_at, str(o.id)))


def _decision_dto(d: Decision, names: dict[str, str]) -> dict[str, Any]:
    return {
        "id": d.id, "kind": d.kind, "created_at": d.created_at,
        "agent": names.get(str(d.agent_id)), "summary": d.summary, "subject": d.subject,
        "outcome": d.outcome, "reason": d.reason, "symbols": d.symbols or [],
        "details": d.details or {},
    }


def _order_label(o: Order, entry_id: uuid.UUID) -> str:
    if o.id == entry_id:
        return "ingresso"
    if o.role in ROLE_LABELS:
        return ROLE_LABELS[o.role]
    if o.oco_group_id is not None:
        return "OCO stop" if o.type in _STOP_TYPES else "OCO target"
    if o.reduce_only:
        return "chiusura"
    return "ordine"


async def _fx(session: AsyncSession, fill_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
    """fill id → {sold: {currency, amount}, bought: {…}, rate, fee: {ccy: amount}} of the
    fill's FX journal (its portfolio cash lines and the system_fees lines)."""
    out: dict[uuid.UUID, dict] = {}
    if not fill_ids:
        return out
    rows = (await session.execute(
        select(Journal.fill_id, Account.asset, Account.kind, JournalLine.quantity)
        .join(JournalLine, JournalLine.journal_id == Journal.id)
        .join(Account, Account.id == JournalLine.account_id)
        .where(Journal.type == JournalType.FX, Journal.fill_id.in_(fill_ids))
    )).all()
    for fill_id, asset, kind, qty in rows:
        fx = out.setdefault(fill_id, {"sold": None, "bought": None, "rate": None, "fee": {}})
        if kind == AccountKind.system_fees:
            fx["fee"][asset] = fx["fee"].get(asset, Decimal(0)) + qty
        elif kind == AccountKind.cash:
            side = "sold" if qty < 0 else "bought"
            prev = fx[side]
            amount = abs(qty) + (prev["amount"] if prev else Decimal(0))
            fx[side] = {"currency": asset, "amount": amount}
    for fx in out.values():
        if fx["sold"] and fx["bought"]:
            # the sold amount includes the FX fee (in the sold currency): the rate is the
            # conversion's, without it
            net = fx["sold"]["amount"] - fx["fee"].get(fx["sold"]["currency"], Decimal(0))
            if net > 0:
                with localcontext() as ctx:
                    ctx.prec = _PREC
                    fx["rate"] = (fx["bought"]["amount"] / net).quantize(Decimal("0.000001"))
    return out


def _fill_dto(f: FillIn, part: Decimal, kind: str, total_qty: dict, fee: dict,
              fx: dict | None, reason: str | None) -> dict[str, Any]:
    return {
        "fill_id": f.fill_id, "order_id": f.order_id, "kind": kind, "side": f.side,
        "ts": f.ts, "qty": part, "fill_qty": total_qty[f.fill_id], "price": f.price,
        "currency": f.currency, "fee_amount": fee.get("amount"),
        "fee_currency": fee.get("currency"), "fx": fx,
        "exit_reason": reason, "exit_reason_label": EXIT_LABELS.get(reason) if reason else None,
    }


def _fmt(v: Decimal | None) -> str:
    if v is None:
        return "—"
    return format(v.normalize(), "f") if v == v.to_integral() else format(v, "f").rstrip("0")


async def trade_detail(session: AsyncSession, qs: QuoteSource, key: str, *, redis=None,
                       now: datetime | None = None) -> dict[str, Any] | None:
    """The trade of `key` with its timeline, or None when there is none."""
    now = now or datetime.now(UTC)
    parsed = parse_key(key)
    if parsed is None:
        return None
    order = await session.get(Order, parsed[0])
    if order is None:
        return None
    built = await _build(session, qs, redis=redis, portfolio_id=order.portfolio_id,
                         symbol=order.symbol, now=now)
    canonical = str(parsed[0]) if parsed[1] == 1 else f"{parsed[0]}.{parsed[1]}"
    trade = built.trades.get(canonical)
    if trade is None:
        return None
    row = next(r for r in built.rows if r.key == canonical)
    ref = base_ref(trade.client_order_id)
    pid, symbol = trade.portfolio_id, trade.symbol

    candidates = list(await session.scalars(
        select(Order).where(Order.portfolio_id == pid, Order.symbol == symbol)
        .order_by(Order.created_at, Order.id)))
    related = _related_orders(trade, list(built.trades.values()), candidates, ref, now)
    ids = {o.id for o in related}
    sids = {str(i) for i in ids}

    # decisions: the proposal's (by ref) and those naming the ref or an order
    needles = sorted(sids | ({ref} if ref else set()))
    text = cast(Decision.details, Text)
    conds = [text.contains(n, autoescape=True) for n in needles]
    if ref:
        conds += [Decision.details["ref"].astext == ref,
                  Decision.summary.contains(ref, autoescape=True),
                  Decision.subject.contains(ref, autoescape=True)]
    decisions = list(await session.scalars(
        select(Decision).where(or_(*conds),
                               or_(Decision.portfolio_id == pid,
                                   Decision.portfolio_id.is_(None)))
        .order_by(Decision.id))) if conds else []
    names = await _agent_names(session, {d.agent_id for d in decisions}
                               | {o.agent_id for o in related if o.agent_id})
    lessons = [d for d in decisions if d.kind == DecisionKind.lesson]
    others = [d for d in decisions if d.kind != DecisionKind.lesson]
    proposal = next((d for d in others if d.kind == DecisionKind.proposal
                     and (d.details or {}).get("ref") == ref), None) if ref else None

    # activity of those orders (cancel reasons, rejections, exits canceled…)
    start = min([o.created_at for o in related] + [trade.opened_at])
    end = (trade.closed_at or now) + timedelta(days=1)
    activity = []
    cancel: dict[str, dict] = {}
    for a in await session.scalars(
            select(Activity).where(Activity.portfolio_id == pid, Activity.ts >= start,
                                   Activity.ts <= end).order_by(Activity.id)):
        data = a.data if isinstance(a.data, dict) else {}
        named = {str(data.get("order_id"))} | {str(x) for x in data.get("order_ids") or []}
        if not (named & sids) and not (a.type.startswith("plan.")
                                       and data.get("symbol") == symbol):
            continue
        activity.append({"id": a.id, "ts": a.ts, "type": a.type, "level": a.level,
                         "summary": a.summary, "data": data})
        if a.type == "order.canceled" and str(data.get("order_id")) in sids:
            cancel[str(data["order_id"])] = data

    # changes and cancels by MCP tool or from the portal
    audits = list(await session.scalars(
        select(McpAuditLog).where(
            McpAuditLog.params["order_id"].astext.in_(sids),
            or_(McpAuditLog.method.in_(_AUDIT_METHODS), McpAuditLog.method.like("admin:%")),
        ).order_by(McpAuditLog.id))) if sids else []
    names |= await _agent_names(session, {a.agent_id for a in audits if a.agent_id})

    # fills
    fills_qty: dict = {}
    fees: dict = {}
    fill_ids = [f.fill_id for f, _, _ in trade.legs if f.fill_id]
    for f in await session.scalars(select(Fill).where(Fill.id.in_(fill_ids))):
        fills_qty[f.id] = f.qty
        fees[f.id] = {"amount": f.fee_amount, "currency": f.fee_currency}
    fx = await _fx(session, fill_ids)
    exit_orders = [built.orders[f.order_id] for f, _, k in trade.legs if k == "exit"]
    hints = await _hints(session, exit_orders)
    fills = [
        _fill_dto(f, part, kind, fills_qty, fees.get(f.fill_id, {}), fx.get(f.fill_id),
                  exit_reason(built.orders[f.order_id], hints) if kind == "exit" else None)
        for f, part, kind in trade.legs
    ]

    entry_id = trade.order_id
    order_dtos = []
    for o in related:
        c = cancel.get(str(o.id))
        reason = None
        if o.status == "canceled":
            why = (c or {}).get("reason") or o.reject_message
            by = (c or {}).get("by")
            reason = ", ".join(x for x in (
                CANCEL_REASONS.get(why, why) if why else None,
                f"da {BY_LABELS.get(by, by)}" if by else None) if x) or None
        order_dtos.append({
            "id": o.id, "label": _order_label(o, entry_id), "role": o.role,
            "client_order_id": o.client_order_id, "side": o.side, "type": o.type,
            "tif": o.tif, "qty": o.qty, "filled_qty": o.filled_qty,
            "limit_price": o.limit_price, "stop_price": o.stop_price,
            "avg_fill_price": o.avg_fill_price, "status": o.status, "source": o.source,
            "agent": names.get(o.agent_id or ""), "reduce_only": o.reduce_only,
            "parent_id": o.parent_id, "oco_group_id": o.oco_group_id,
            "created_at": o.created_at, "updated_at": o.updated_at,
            "reject_code": o.reject_code, "reject_message": o.reject_message,
            "cancel_reason": reason,
        })
    audit_dtos = [{
        "id": a.id, "ts": a.created_at, "method": a.method, "outcome": a.outcome,
        "agent": names.get(str(a.agent_id)) if a.agent_id else None,
        "order_id": a.params.get("order_id"),
        "changes": {k: v for k, v in (a.params or {}).items()
                    if k != "order_id" and v not in (None, "")},
    } for a in audits]

    timeline = _timeline(others, lessons, order_dtos, fills, audit_dtos,
                         activity, names)
    instrument = await session.get(Instrument, symbol)
    return {
        "trade": row,
        "instrument_name": instrument.name if instrument else None,
        "proposal": _decision_dto(proposal, names) if proposal else None,
        "decisions": [_decision_dto(d, names) for d in others],
        "lessons": [_decision_dto(d, names) for d in lessons],
        "orders": order_dtos,
        "fills": fills,
        "changes": audit_dtos,
        "activity": activity,
        "timeline": timeline,
    }


_KIND_TITLES = {"proposal": "Proposta", "approval": "Approvazione del Risk Manager",
                "veto": "Veto del Risk Manager", "strategy": "Decisione sulla strategia",
                "allocation": "Allocazione", "risk_rule": "Regola di rischio",
                "other": "Decisione", "team_rule": "Regola del team"}
_SIDE_IT = {"buy": "acquisto", "sell": "vendita"}
_TYPE_IT = {"market": "a mercato", "limit": "limite", "stop": "stop", "stop_limit": "stop limite",
            "trailing_stop": "stop mobile"}
_STATUS_IT = {"new": "nuovo", "held": "in attesa dell'ingresso", "queued": "in coda",
              "pending": "attivo", "triggered": "scattato", "partially_filled": "eseguito in parte",
              "filled": "eseguito", "canceled": "annullato", "rejected": "rifiutato",
              "expired": "scaduto"}


def _timeline(decisions, lessons, orders, fills, audits, activity, names
              ) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for d in decisions:
        title = _KIND_TITLES.get(d.kind, "Decisione")
        who = names.get(str(d.agent_id))
        items.append({"ts": d.created_at, "type": f"decision.{d.kind}",
                      "title": f"{title}{f' · {who}' if who else ''}", "text": d.summary,
                      "decision_id": d.id, "order_id": None})
    for d in lessons:
        items.append({"ts": d.created_at, "type": "lesson", "title": "Lezione",
                      "text": d.summary, "decision_id": d.id, "order_id": None})
    for o in orders:
        px = o["limit_price"] if o["limit_price"] is not None else o["stop_price"]
        price = f" a {_fmt(px)}" if px is not None else ""
        items.append({
            "ts": o["created_at"], "type": "order.placed",
            "title": f"Ordine {o['label']}",
            "text": f"{_SIDE_IT.get(str(o['side']), o['side'])} {_fmt(o['qty'])} "
                    f"{_TYPE_IT.get(str(o['type']), o['type'])}{price} · stato "
                    f"{_STATUS_IT.get(str(o['status']), str(o['status']))}",
            "decision_id": None, "order_id": o["id"]})
        if str(o["status"]) in ("canceled", "rejected", "expired") and o["updated_at"]:
            why = o["cancel_reason"] or o["reject_message"]
            items.append({
                "ts": o["updated_at"], "type": f"order.{o['status']}",
                "title": f"Ordine {o['label']} {_STATUS_IT.get(str(o['status']))}",
                "text": why or "", "decision_id": None, "order_id": o["id"]})
    for f in fills:
        what = "Ingresso eseguito" if f["kind"] == "entry" else (
            f"Uscita eseguita · {f['exit_reason_label']}" if f["exit_reason_label"]
            else "Uscita eseguita")
        fee = f" · commissione {_fmt(f['fee_amount'])} {f['fee_currency']}" \
            if f["fee_amount"] else ""
        items.append({"ts": f["ts"], "type": f"fill.{f['kind']}", "title": what,
                      "text": f"{_SIDE_IT.get(str(f['side']), f['side'])} {_fmt(f['qty'])} a "
                              f"{_fmt(f['price'])} "
                              f"{f['currency']}{fee}",
                      "decision_id": None, "order_id": f["order_id"]})
    for a in audits:
        verb = "Annullamento richiesto" if a["method"].endswith("cancel_order") \
            or "cancel" in a["method"] else "Modifica"
        changes = ", ".join(f"{k} {v}" for k, v in a["changes"].items())
        items.append({"ts": a["ts"], "type": "order.change",
                      "title": f"{verb}{f' · {a["agent"]}' if a['agent'] else ''}",
                      "text": f"{changes}{'' if a['outcome'] == 'ok' else f' · esito {a["outcome"]}'}",
                      "decision_id": None, "order_id": a["order_id"]})
    for a in activity:
        if a["type"] in ("order.filled", "order.partially_filled", "order.canceled",
                         "order.rejected", "order.expired", "order.accepted"):
            continue  # already in the orders and fills
        items.append({"ts": a["ts"], "type": f"activity.{a['type']}", "title": "Evento",
                      "text": a["summary"], "decision_id": None,
                      "order_id": a["data"].get("order_id")})
    items.sort(key=lambda i: i["ts"])
    return items
