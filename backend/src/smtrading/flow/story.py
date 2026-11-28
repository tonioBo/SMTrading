"""«Fiume dei flussi» v2: the story of one order, and of one MCP call without an order.

Order story (portfolios of the river only: `neurograph_portfolio_ids`, as `flow.events`):

    decision   the agent's decision linked by `orders.decision_id` (the order's, else its
               parent's): summary, reason, confidence, expected; orders before the link
               (28/11) have none: «decisione non collegata»
    mcp        the agent's MCP call that created it: an order tool of the same agent, same
               symbol when the call names one, running when the order was created
               (start <= created_at <= end); tool, outcome, reason code, time, symbol only
    sent       broker_sent_at and the link's broker
    rejected   updated_at of a rejected order, its code and message
    children   the stop / target legs (parent_id, OCO siblings)
    fills      price filled against the price expected: the order's limit (or stop) price,
               else the quote of the fill's side (ask to buy, bid to sell, else last)
    telegram   the messages of the order's activity rows: category and time, never the text
    outcome    status, filled quantity, average price

`steps` is the same journey as one time-ordered list for the side panel. The MCP story is
the call alone: tool, agent, outcome, reason code (the outcome when not ok), symbol, and
the order it created (the same match reversed: an order of the same agent and symbol
created while the call ran), if any."""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import Interval, case, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.activity.models import Activity, Decision
from smtrading.agents.models import Agent, McpAuditLog
from smtrading.brokers.models import BrokerLink
from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus
from smtrading.flow.events import TELEGRAM_CATEGORY_IT, _cap, mcp_path
from smtrading.flow.map import agent_key, broker_key, broker_label
from smtrading.portfolios.models import neurograph_portfolio_ids
from smtrading.telegram.models import TelegramOutbox

NO_DECISION = "decisione non collegata"
NO_MCP_AGENT = "chiamata MCP non trovata"
NO_MCP_OTHER = "ordine non nato da una chiamata MCP di un agente"
REFUSED_NOTE = "chiamata rifiutata: nessun ordine creato"
MESSAGE_MAX = 300
TELEGRAM_AFTER = timedelta(days=1)  # an order's messages: until a day after its last change
CALL_SLACK = timedelta(seconds=1)  # clock skew between the call's row and the order's

# the MCP tools that create orders
ORDER_TOOLS = ("place_order", "place_bracket_order", "place_oco_order", "close_position",
               "close_all_positions", "modify_order")

STATUS_IT = {
    "new": "inviato", "held": "in attesa del padre", "queued": "in coda (mercato chiuso)",
    "pending": "in attesa del prezzo", "triggered": "scattato",
    "partially_filled": "eseguito in parte", "filled": "eseguito", "canceled": "annullato",
    "rejected": "rifiutato", "expired": "scaduto",
}
ROLE_IT = {"entry": "entrata", "take_profit": "obiettivo", "stop_loss": "stop"}
DIGEST_IT = "nel riepilogo"
SIDE_IT = {"buy": "acquisto", "sell": "vendita"}

_MS = literal_column("interval '1 millisecond'", type_=Interval)


def _step(at: datetime | None, step: str, label: str, node: str) -> dict | None:
    return {"at": at, "step": step, "label": label, "node": node} if at else None


def _expected(o: Order, f: Fill) -> tuple[Decimal | None, str | None]:
    if o.limit_price is not None:
        return o.limit_price, "limite"
    if o.stop_price is not None:
        return o.stop_price, "stop"
    quote = f.quote_ask if f.side == OrderSide.buy else f.quote_bid
    quote = quote if quote is not None else f.quote_last
    return (quote, "quotazione") if quote is not None else (None, None)


def _slippage_pct(price: Decimal, expected: Decimal | None) -> Decimal | None:
    if not expected:
        return None
    return ((price - expected) * 100 / expected).quantize(Decimal("0.01"))


async def _agent(session: AsyncSession, agent_id: str | None) -> dict | None:
    if not agent_id:
        return None
    try:
        aid = uuid.UUID(str(agent_id))
    except ValueError:
        return None
    name = await session.scalar(select(Agent.name).where(Agent.id == aid))
    return {"key": agent_key(aid), "name": name} if name is not None else None


async def _decision(session: AsyncSession, o: Order) -> dict | None:
    decision_id = o.decision_id
    if decision_id is None and o.parent_id is not None:
        decision_id = await session.scalar(select(Order.decision_id)
                                           .where(Order.id == o.parent_id))
    if decision_id is None:
        return None
    d = await session.get(Decision, decision_id)
    if d is None:
        return None
    return {"id": d.id, "kind": d.kind, "summary": d.summary, "reason": d.reason,
            "confidence": d.confidence, "expected": d.expected, "at": d.created_at}


def _symbol_col():
    symbols = McpAuditLog.params["symbols"]
    return func.coalesce(
        McpAuditLog.params["symbol"].astext,
        case((func.jsonb_typeof(symbols) == "array", symbols[0].astext),
             (func.jsonb_typeof(symbols) == "string", symbols.astext)))


async def _mcp_call(session: AsyncSession, o: Order) -> dict | None:
    """The agent's MCP call that created `o` (see the module docstring), or None."""
    try:
        aid = uuid.UUID(str(o.agent_id))
    except (TypeError, ValueError):
        return None
    ts = McpAuditLog.created_at - McpAuditLog.latency_ms * _MS
    symbol = _symbol_col()
    row = (await session.execute(
        select(McpAuditLog.id, McpAuditLog.method, McpAuditLog.outcome,
               McpAuditLog.latency_ms, ts.label("ts"), symbol.label("symbol"))
        .where(McpAuditLog.agent_id == aid, McpAuditLog.method.in_(ORDER_TOOLS),
               ts <= o.created_at + CALL_SLACK,
               McpAuditLog.created_at >= o.created_at - CALL_SLACK,
               McpAuditLog.created_at <= o.created_at + timedelta(minutes=5),
               or_(symbol.is_(None), symbol == o.symbol))
        .order_by(McpAuditLog.created_at, McpAuditLog.id).limit(1))).first()
    if row is None:
        return None
    return {"audit_id": row.id, "tool": row.method,
            "outcome": "ok" if row.outcome == "ok" else "error",
            "reason_code": None if row.outcome == "ok" else row.outcome,
            "latency_ms": row.latency_ms, "symbol": _cap(row.symbol), "at": row.ts}


async def _children(session: AsyncSession, o: Order) -> list[dict]:
    cond = Order.parent_id == o.id
    if o.oco_group_id is not None:
        cond = or_(cond, Order.oco_group_id == o.oco_group_id)
    rows = (await session.scalars(select(Order).where(cond, Order.id != o.id)
                                  .order_by(Order.created_at, Order.id))).all()
    return [{"id": c.id, "role": c.role, "role_it": ROLE_IT.get(c.role or "", c.role),
             "type": c.type.value, "side": c.side.value, "status": c.status.value,
             "status_it": STATUS_IT.get(c.status.value, c.status.value),
             "stop_price": c.stop_price, "limit_price": c.limit_price, "at": c.created_at}
            for c in rows]


async def _fills(session: AsyncSession, o: Order) -> list[dict]:
    out = []
    for f in await session.scalars(select(Fill).where(Fill.order_id == o.id)
                                   .order_by(Fill.created_at, Fill.id)):
        expected, source = _expected(o, f)
        out.append({"id": f.id, "at": f.created_at, "qty": f.qty, "price": f.price,
                    "expected_price": expected, "expected_from": source,
                    "slippage_pct": _slippage_pct(f.price, expected), "source": f.source})
    return out


async def _telegram(session: AsyncSession, o: Order) -> list[dict]:
    rows = (await session.execute(
        select(Activity.ts, Activity.telegram, TelegramOutbox.category,
               TelegramOutbox.status, TelegramOutbox.sent_at, TelegramOutbox.created_at)
        .outerjoin(TelegramOutbox, TelegramOutbox.id == Activity.outbox_id)
        .where(Activity.portfolio_id == o.portfolio_id,
               Activity.ts >= o.created_at - timedelta(minutes=1),
               Activity.ts <= max(o.created_at, o.updated_at) + TELEGRAM_AFTER,
               Activity.data["order_id"].astext == str(o.id),
               or_(Activity.outbox_id.is_not(None), Activity.telegram == "digest"))
        .order_by(Activity.ts, Activity.id))).all()
    out = []
    for r in rows:
        if r.category is None:  # grouped in a summary
            out.append({"category": "digest", "category_it": DIGEST_IT,
                        "status": r.telegram, "at": r.ts})
        else:
            out.append({"category": r.category,
                        "category_it": TELEGRAM_CATEGORY_IT.get(r.category, r.category),
                        "status": r.status, "at": r.sent_at or r.created_at})
    return out


async def order_story(session: AsyncSession, order_id: uuid.UUID) -> dict | None:
    o = await session.scalar(select(Order).where(
        Order.id == order_id, Order.portfolio_id.in_(neurograph_portfolio_ids())))
    if o is None:
        return None
    kind = (await session.scalar(select(BrokerLink.kind)
                                 .where(BrokerLink.id == o.broker_link_id))
            if o.broker_link_id else None)
    broker = ({"kind": kind, "label": broker_label(kind), "node": broker_key(kind)}
              if kind else None)
    agent = await _agent(session, o.agent_id)
    decision = await _decision(session, o)
    mcp = await _mcp_call(session, o) if o.source == "agent" else None
    sent = ({"at": o.broker_sent_at, "broker": broker["label"] if broker else None}
            if o.broker_sent_at else None)
    rejected = ({"at": o.updated_at, "code": o.reject_code,
                 "message": (o.reject_message or "")[:MESSAGE_MAX] or None}
                if o.status == OrderStatus.rejected else None)
    children = await _children(session, o)
    fills = await _fills(session, o)
    telegram = await _telegram(session, o)
    status = o.status.value

    steps = [
        _step(decision and decision["at"], "decision",
              f"decisione: {decision['summary']}" if decision else "", agent["key"]
              if agent else "mcp"),
        _step(mcp and mcp["at"], "mcp", f"chiamata {mcp['tool']}" if mcp else "", "mcp"),
        _step(o.created_at, "created", f"{SIDE_IT[o.side.value]} {o.symbol}: controlli e "
                                       "prenotazione", "eng"),
        *(_step(c["at"], "child", f"{c['role_it'] or 'ordine collegato'} "
                                  f"{c['status_it']}", "eng") for c in children),
        _step(sent and sent["at"], "sent", f"inviato a {broker['label']}" if broker else "",
              "sync"),
        *(_step(f["at"], "fill", f"eseguito {f['qty']} a {f['price']}",
                broker["node"] if broker and f["source"] == "broker" else "sim")
          for f in fills),
        *(_step(t["at"], "telegram", f"Telegram: {t['category_it']}", "tg") for t in telegram),
        _step(rejected and rejected["at"], "rejected",
              f"rifiutato: {rejected['code'] or ''}".rstrip(": ") if rejected else "", "eng"),
    ]
    steps = sorted((s for s in steps if s), key=lambda s: s["at"])
    return {
        "order": {"id": o.id, "symbol": o.symbol, "side": o.side.value, "type": o.type.value,
                  "qty": o.qty, "notional": o.notional, "limit_price": o.limit_price,
                  "stop_price": o.stop_price, "status": status,
                  "status_it": STATUS_IT.get(status, status), "role": o.role,
                  "source": o.source, "parent_id": o.parent_id, "created_at": o.created_at,
                  "broker": broker},
        "agent": agent,
        "decision": decision, "decision_note": None if decision else NO_DECISION,
        "mcp": mcp,
        "mcp_note": None if mcp else (NO_MCP_AGENT if o.source == "agent" else NO_MCP_OTHER),
        "sent": sent, "rejected": rejected, "children": children, "fills": fills,
        "telegram": telegram,
        "outcome": {"status": status, "status_it": STATUS_IT.get(status, status),
                    "filled_qty": o.filled_qty, "avg_fill_price": o.avg_fill_price,
                    "at": o.updated_at},
        "steps": steps,
    }


async def mcp_story(session: AsyncSession, audit_id: int) -> dict | None:
    """One agent's MCP call (never an `admin:*` row): tool, outcome, reason code, symbol."""
    ts = McpAuditLog.created_at - McpAuditLog.latency_ms * _MS
    row = (await session.execute(
        select(McpAuditLog.id, McpAuditLog.agent_id, McpAuditLog.method,
               McpAuditLog.outcome, McpAuditLog.latency_ms, ts.label("ts"),
               _symbol_col().label("symbol"), Agent.name)
        .join(Agent, Agent.id == McpAuditLog.agent_id)
        .where(McpAuditLog.id == audit_id, ~McpAuditLog.method.like("admin:%")))).first()
    if row is None:
        return None
    ok = row.outcome == "ok"
    key = agent_key(row.agent_id)
    order_id = None
    if ok and row.method in ORDER_TOOLS:
        conds = [Order.agent_id == str(row.agent_id), Order.source == "agent",
                 Order.created_at >= row.ts - CALL_SLACK,
                 Order.created_at <= row.ts + timedelta(milliseconds=row.latency_ms or 0)
                 + CALL_SLACK,
                 Order.portfolio_id.in_(neurograph_portfolio_ids())]
        if row.symbol:
            conds.append(Order.symbol == row.symbol)
        order_id = await session.scalar(  # the entry before its legs
            select(Order.id).where(*conds)
            .order_by(Order.parent_id.is_not(None), Order.created_at, Order.id).limit(1))
    path, _ = mcp_path(key, row.method)
    return {"audit_id": row.id, "at": row.ts, "tool": row.method,
            "agent": {"key": key, "name": row.name},
            "outcome": "ok" if ok else "error", "reason_code": None if ok else row.outcome,
            "latency_ms": row.latency_ms, "symbol": _cap(row.symbol),
            "path": path,
            "note": None if ok else (REFUSED_NOTE if row.method in ORDER_TOOLS
                                     else "chiamata non riuscita"),
            "order_id": str(order_id) if order_id else None}
