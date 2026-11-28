"""The data of the TV screens, from the modules the portal pages already use (no new
numbers), and only what that user sees in the portal: the «Fiume dei flussi» (admins
only, like its page), the campaign and portfolios (Roadmap live numbers, equity
snapshots, positions with their stop, the last fills: every user), the health («Salute
del sistema» sections and the sentinel's alerts for every user; the river's node health
for admins). No secret, no free text of an alert: titles and times only.

    flow_screen      {screen, now, map, summary, health, events, cursor}
    campaign_screen  {screen, now, campaign, equity, goal, portfolios, positions, fills,
                      notes}
    health_screen    {screen, now, status, checked_at, sections, nodes, alerts}
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.system_health import system_health
from smtrading.common.config import get_settings
from smtrading.engine.models import OPEN_STATUSES, Fill, Order, OrderSide, OrderType
from smtrading.flow.cache import TtlCache
from smtrading.flow.events import flow_events
from smtrading.flow.health import flow_health
from smtrading.flow.map import flow_map
from smtrading.flow.summary import flow_summary
from smtrading.portfolios.models import Portfolio, total_portfolio_ids
from smtrading.positions.models import Position
from smtrading.risk.models import EquitySnapshot, SnapshotKind
from smtrading.roadmap.service import live_numbers, load_roadmap
from smtrading.sentinel import engine as sentinel_engine

FLOW_WINDOW = 5
FLOW_EVENTS_LIMIT = 300
FILLS_MAX = 10
POSITIONS_MAX = 40
ALERTS_MAX = 10
HEALTH_TTL_S = 15
HEALTH_SECTIONS = ("services", "providers", "quotes", "paperclip", "telegram", "mcp",
                   "backup")
STOP_TYPES = (OrderType.stop, OrderType.stop_limit, OrderType.trailing_stop)
_PCT = Decimal("0.01")
_health_cache = TtlCache(HEALTH_TTL_S)


def _zone() -> ZoneInfo:
    return ZoneInfo(get_settings().tz)


# -- Fiume dei flussi --------------------------------------------------------------------


async def flow_screen(session: AsyncSession, redis, *, after: datetime | None,
                      now: datetime) -> dict[str, Any]:
    events = await flow_events(session, after=after, limit=FLOW_EVENTS_LIMIT, now=now)
    latest = max((e["ts"] for e in events), default=None)
    if after is not None and (latest is None or latest < after):
        latest = after
    return {
        "screen": "flow", "now": now,
        "map": await flow_map(session),
        "summary": await flow_summary(session, window=FLOW_WINDOW, now=now),
        "health": await flow_health(session, redis),
        "events": events, "cursor": latest or now,
    }


# -- Campagna e portafogli ---------------------------------------------------------------


async def _portfolios(session: AsyncSession, now: datetime, today: date) -> list[dict]:
    rows = (await session.execute(
        select(Portfolio.id, Portfolio.name, Portfolio.code)
        .where(Portfolio.id.in_(total_portfolio_ids())).order_by(Portfolio.name))).all()
    if not rows:
        return []
    ids = [r.id for r in rows]
    latest = {pid: (eq, ts) for pid, eq, ts in (await session.execute(
        select(EquitySnapshot.portfolio_id, EquitySnapshot.equity_eur, EquitySnapshot.ts)
        .ext(distinct_on(EquitySnapshot.portfolio_id))
        .where(EquitySnapshot.portfolio_id.in_(ids), EquitySnapshot.ts <= now)
        .order_by(EquitySnapshot.portfolio_id, EquitySnapshot.ts.desc(),
                  EquitySnapshot.id.desc()))).all()}
    opens = dict((await session.execute(
        select(EquitySnapshot.portfolio_id, EquitySnapshot.equity_eur)
        .where(EquitySnapshot.portfolio_id.in_(ids),
               EquitySnapshot.kind == SnapshotKind.day_open,
               EquitySnapshot.local_date == today))).all())
    out = []
    for r in rows:
        equity, ts = latest.get(r.id, (None, None))
        base = opens.get(r.id)
        change = (equity - base) if equity is not None and base is not None else None
        pct = ((change / base) * 100).quantize(_PCT) if change is not None and base else None
        out.append({"id": str(r.id), "name": r.name, "code": r.code,
                    "equity_eur": equity.quantize(_PCT) if equity is not None else None,
                    "day_change_eur": change.quantize(_PCT) if change is not None else None,
                    "day_change_pct": pct, "as_of": ts})
    return out


async def _positions(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(
        select(Position.portfolio_id, Portfolio.name, Position.symbol, Position.qty,
               Position.avg_price, Position.opened_at)
        .join(Portfolio, Portfolio.id == Position.portfolio_id)
        .where(Position.portfolio_id.in_(total_portfolio_ids()), Position.qty != 0)
        .order_by(Portfolio.name, Position.symbol).limit(POSITIONS_MAX))).all()
    if not rows:
        return []
    stops = (await session.execute(
        select(Order.portfolio_id, Order.symbol, Order.side, Order.type, Order.stop_price,
               Order.trail_pct, Order.created_at)
        .where(Order.portfolio_id.in_({r.portfolio_id for r in rows}),
               Order.type.in_(STOP_TYPES), Order.status.in_(OPEN_STATUSES))
        .order_by(Order.created_at.desc()))).all()
    stop_of: dict[tuple, Any] = {}
    for s in stops:
        stop_of.setdefault((s.portfolio_id, s.symbol, s.side), s)
    out = []
    for r in rows:
        side = OrderSide.sell if r.qty > 0 else OrderSide.buy  # the closing side
        s = stop_of.get((r.portfolio_id, r.symbol, side))
        out.append({"portfolio": r.name, "symbol": r.symbol, "qty": r.qty,
                    "avg_price": r.avg_price, "opened_at": r.opened_at,
                    "stop_price": s.stop_price if s is not None else None,
                    "stop_type": str(s.type) if s is not None else None,
                    "trail_pct": s.trail_pct if s is not None else None})
    return out


async def _fills(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(
        select(Fill.symbol, Fill.side, Fill.qty, Fill.price, Fill.created_at, Portfolio.name)
        .join(Portfolio, Portfolio.id == Fill.portfolio_id)
        .where(Fill.portfolio_id.in_(total_portfolio_ids()))
        .order_by(Fill.created_at.desc()).limit(FILLS_MAX))).all()
    return [{"portfolio": r.name, "symbol": r.symbol, "side": str(r.side), "qty": r.qty,
             "price": r.price, "at": r.created_at} for r in rows]


async def campaign_screen(session: AsyncSession, *, now: datetime) -> dict[str, Any]:
    zone = _zone()
    data = load_roadmap()
    live = await live_numbers(session, data, now, zone)
    c = data["campaign"]
    return {
        "screen": "campaign", "now": now,
        "campaign": {"name": c.get("name"), **live["campaign"]},
        "equity": live["equity"], "goal": live["goal"],
        "portfolios": await _portfolios(session, now, now.astimezone(zone).date()),
        "positions": await _positions(session),
        "fills": await _fills(session),
        "notes": live["notes"],
    }


# -- Salute e avvisi ---------------------------------------------------------------------


def _alerts(sentinel: dict[str, Any], state_history: list[dict]) -> dict[str, Any]:
    open_ = [{"title": o.get("title"), "label": o.get("label"), "level": o.get("level"),
              "since": o.get("since")} for o in (sentinel.get("open") or [])][:ALERTS_MAX]
    recent = [{"label": sentinel_engine.KIND_LABELS.get(h.get("kind") or "", h.get("kind")),
               "at": h.get("at")}
              for h in sorted(state_history, key=lambda h: h.get("at") or "",
                              reverse=True)][:ALERTS_MAX]
    return {"on": sentinel.get("on"), "open": open_, "recent": recent}


async def _system(session: AsyncSession, redis, now: datetime) -> dict[str, Any]:
    from smtrading.sentinel import settings as sentinel_settings

    health = await system_health(session, redis, now=now)
    try:
        history = (await sentinel_settings.load_state(session)).get("history") or []
    except Exception:  # noqa: BLE001
        await session.rollback()
        history = []
    return {"health": health, "history": history}


async def system_health_cached(session: AsyncSession, redis, now: datetime) -> dict:
    """{health, history}: «Salute del sistema» and the sentinel's history, cached
    HEALTH_TTL_S (every TV polls)."""
    return await _health_cache.get("system", lambda: _system(session, redis, now))


async def health_screen(session: AsyncSession, redis, *, now: datetime,
                        with_nodes: bool) -> dict[str, Any]:
    sysh = await system_health_cached(session, redis, now)
    health = sysh["health"]
    sections = [{"key": k, "label": s.get("label"), "status": s.get("status"),
                 "summary": s.get("summary")}
                for k in HEALTH_SECTIONS if (s := health["sections"].get(k))]
    nodes: list[dict] = []
    if with_nodes:
        fmap = await flow_map(session)
        fh = await flow_health(session, redis)
        for n in fmap["nodes"]:
            h = fh["nodes"].get(n["key"]) or {}
            nodes.append({"key": n["key"], "label": n["label"], "kind": n["kind"],
                          "state": h.get("state"), "reason": h.get("reason")})
    return {"screen": "health", "now": now, "status": health["status"],
            "checked_at": health["checked_at"], "sections": sections, "nodes": nodes,
            "alerts": _alerts(health.get("sentinel") or {}, sysh["history"])}
