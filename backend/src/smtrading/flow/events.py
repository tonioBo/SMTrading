"""Real flow events of the «Fiume dei flussi», translated from records that already exist.

Read only: one bounded query per source, nothing written, nothing published.

    mcp_audit_log    an agent's call      agent → Paperclip → MCP → <dest> (→ MCP → Paperclip)
    orders           sent to a broker     Motore → BrokerSync → <broker>      (broker_sent_at)
    orders           rejected             Motore → BrokerSync → <broker>, or MCP → Motore
    fills            executed             <broker> → BrokerSync → Motore, or Motore → Simulato
    auto_trades      an automatic entry   Prezzi → Regole automatiche → Motore (trigger_at)
    telegram_outbox  a system message     Motore → Telegram (sent; agents' messages are
                                          already their `send_telegram` call)

Orders, fills and automatic trades only of the portfolios counted in the totals
(`total_portfolio_ids`: hidden test and archived portfolios left out); the agents' MCP
calls always (they are real traffic, also on a test portfolio). Of a call's parameters
only the symbol (`symbol`, or the first of `symbols`) leaves the database.

Event ids are stable (`mcp:<id>`, `order:<id>:sent`, `order:<id>:rejected`, `fill:<id>`,
`tg:<id>`, `auto:<id>`): the page polls with a time cursor and drops the ids it has seen.
"""

from datetime import datetime, timedelta

from sqlalchemy import Interval, case, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.agents.models import McpAuditLog
from smtrading.autorules.tables import REJECTED as AUTO_REJECTED
from smtrading.autorules.tables import AutoTrade
from smtrading.brokers.models import BrokerLink
from smtrading.engine.models import Fill, Order, OrderStatus
from smtrading.flow.map import ONE_WAY_DESTS, agent_key, broker_key, destination
from smtrading.portfolios.models import total_portfolio_ids
from smtrading.telegram.models import OutboxStatus, TelegramOutbox

OVERLAP = timedelta(seconds=10)  # a poll re-reads this much before its cursor
FIRST_WINDOW = timedelta(minutes=15)  # the first poll (no cursor)
LIMIT_MIN, LIMIT_MAX, LIMIT_DEFAULT = 1, 2000, 500
RETURN_HOP = "risposta"

# the category of a system message, in plain Italian (unknown: the category itself)
TELEGRAM_CATEGORY_IT = {
    "event": "avviso", "digest": "riepilogo", "daily": "riepilogo del giorno",
    "weekly": "riepilogo settimanale", "system": "messaggio di sistema", "test": "prova",
    "live": "conto reale", "agent": "messaggio",
}

_MS = literal_column("interval '1 millisecond'", type_=Interval)


def _event(id_: str, ts: datetime, kind: str, who: str, path: list[str], hops: list[str],
           outcome: str = "ok", latency_ms: int | None = None,
           symbol: str | None = None) -> dict:
    return {"id": id_, "ts": ts, "kind": kind, "who": who, "path": path, "hops": hops,
            "outcome": outcome, "latency_ms": latency_ms, "symbol": symbol}


def mcp_path(agent: str, method: str) -> tuple[list[str], list[str]]:
    """The path and hop labels of one MCP call: forward hops carry the tool name, the
    return hops (calls that answer with data) «risposta»."""
    dest = destination(method)
    if dest == "mcp":
        return [agent, "pc", "mcp", "pc"], [method, method, RETURN_HOP]
    path = [agent, "pc", "mcp", dest]
    hops = [method, method, method]
    if dest not in ONE_WAY_DESTS:
        path += ["mcp", "pc"]
        hops += [RETURN_HOP, RETURN_HOP]
    return path, hops


async def _mcp(session: AsyncSession, since: datetime, until: datetime, limit: int) -> list[dict]:
    ts = (McpAuditLog.created_at - McpAuditLog.latency_ms * _MS).label("ts")
    symbols = McpAuditLog.params["symbols"]
    symbol = func.coalesce(
        McpAuditLog.params["symbol"].astext,
        case((func.jsonb_typeof(symbols) == "array", symbols[0].astext),
             (func.jsonb_typeof(symbols) == "string", symbols.astext)),
    ).label("symbol")
    # filtered on the row's write time: a long call (its row is written at the end) is
    # never lost behind the cursor; the page drops the ids it has already seen
    q = (select(McpAuditLog.id, McpAuditLog.agent_id, McpAuditLog.method,
                McpAuditLog.outcome, McpAuditLog.latency_ms, ts, symbol)
         .where(McpAuditLog.agent_id.is_not(None), ~McpAuditLog.method.like("admin:%"),
                McpAuditLog.created_at > since, ts <= until)
         .order_by(ts, McpAuditLog.id).limit(limit))
    out = []
    for r in (await session.execute(q)).all():
        path, hops = mcp_path(agent_key(r.agent_id), r.method)
        out.append(_event(f"mcp:{r.id}", r.ts, "mcp", path[0], path, hops,
                          "ok" if r.outcome == "ok" else "error", r.latency_ms, r.symbol))
    return out


async def _orders_sent(session, since, until, limit) -> list[dict]:
    q = (select(Order.id, Order.symbol, Order.broker_sent_at, BrokerLink.kind)
         .join(BrokerLink, BrokerLink.id == Order.broker_link_id)
         .where(Order.broker_sent_at > since, Order.broker_sent_at <= until,
                Order.portfolio_id.in_(total_portfolio_ids()))
         .order_by(Order.broker_sent_at, Order.id).limit(limit))
    return [_event(f"order:{r.id}:sent", r.broker_sent_at, "order_sent", "eng",
                   ["eng", "sync", broker_key(r.kind)], ["invio", f"ordine {r.symbol}"],
                   symbol=r.symbol)
            for r in (await session.execute(q)).all()]


async def _orders_rejected(session, since, until, limit) -> list[dict]:
    q = (select(Order.id, Order.symbol, Order.updated_at, Order.broker_sent_at,
                BrokerLink.kind)
         .outerjoin(BrokerLink, BrokerLink.id == Order.broker_link_id)
         .where(Order.status == OrderStatus.rejected,
                Order.updated_at > since, Order.updated_at <= until,
                Order.portfolio_id.in_(total_portfolio_ids()))
         .order_by(Order.updated_at, Order.id).limit(limit))
    out = []
    for r in (await session.execute(q)).all():
        if r.kind is not None and r.broker_sent_at is not None:  # refused by the broker
            path = ["eng", "sync", broker_key(r.kind)]
            hops = ["invio", f"rifiutato {r.symbol}"]
        else:  # refused by the engine
            path, hops = ["mcp", "eng"], ["rifiutato"]
        out.append(_event(f"order:{r.id}:rejected", r.updated_at, "order_rejected", "eng",
                          path, hops, "error", symbol=r.symbol))
    return out


async def _fills(session, since, until, limit) -> list[dict]:
    q = (select(Fill.id, Fill.symbol, Fill.created_at, Fill.source, BrokerLink.kind)
         .join(Order, Order.id == Fill.order_id)
         .outerjoin(BrokerLink, BrokerLink.id == Order.broker_link_id)
         .where(Fill.created_at > since, Fill.created_at <= until,
                Fill.portfolio_id.in_(total_portfolio_ids()))
         .order_by(Fill.created_at, Fill.id).limit(limit))
    out = []
    for r in (await session.execute(q)).all():
        if r.kind is not None and r.source == "broker":
            path = [broker_key(r.kind), "sync", "eng"]
            hops = [f"eseguito {r.symbol}", "registrazione"]
        else:
            path, hops = ["eng", "sim"], [f"esecuzione simulata {r.symbol}"]
        out.append(_event(f"fill:{r.id}", r.created_at, "fill", "eng", path, hops,
                          symbol=r.symbol))
    return out


async def _auto(session, since, until, limit) -> list[dict]:
    q = (select(AutoTrade.id, AutoTrade.symbol, AutoTrade.trigger_at, AutoTrade.status)
         .where(AutoTrade.trigger_at > since, AutoTrade.trigger_at <= until,
                AutoTrade.portfolio_id.in_(total_portfolio_ids()))
         .order_by(AutoTrade.trigger_at, AutoTrade.id).limit(limit))
    return [_event(f"auto:{r.id}", r.trigger_at, "auto", "rules", ["px", "rules", "eng"],
                   [f"scatto {r.symbol}", "entrata"],
                   "error" if r.status == AUTO_REJECTED else "ok", symbol=r.symbol)
            for r in (await session.execute(q)).all()]


async def _telegram(session, since, until, limit) -> list[dict]:
    q = (select(TelegramOutbox.id, TelegramOutbox.category, TelegramOutbox.sent_at)
         .where(TelegramOutbox.status == OutboxStatus.sent,
                ~TelegramOutbox.source.like("agent:%"),
                TelegramOutbox.sent_at > since, TelegramOutbox.sent_at <= until)
         .order_by(TelegramOutbox.sent_at, TelegramOutbox.id).limit(limit))
    return [_event(f"tg:{r.id}", r.sent_at, "telegram", "eng", ["eng", "tg"],
                   [TELEGRAM_CATEGORY_IT.get(r.category, r.category)])
            for r in (await session.execute(q)).all()]


_SOURCES = (_mcp, _orders_sent, _orders_rejected, _fills, _auto, _telegram)


async def collect_events(session: AsyncSession, *, since: datetime, until: datetime,
                         limit: int) -> list[dict]:
    """The events with `since < ts <= until` (MCP calls: also those written after `since`),
    the `limit` oldest, sorted by (ts, id)."""
    events: list[dict] = []
    for source in _SOURCES:
        events += await source(session, since, until, limit)
    events.sort(key=lambda e: (e["ts"], e["id"]))
    return events[:limit]


async def flow_events(session: AsyncSession, *, after: datetime | None, limit: int,
                      now: datetime) -> list[dict]:
    """The events after the time cursor `after` (re-reading OVERLAP before it), or of the
    last FIRST_WINDOW without a cursor; at most `limit`, oldest first."""
    since = after - OVERLAP if after is not None else now - FIRST_WINDOW
    return await collect_events(session, since=since, until=now, limit=limit)
