"""The «Fiume dei flussi» summary of a window (1, 5 or 15 minutes), built from the same
events as the river (one translation path: `flow.events`), so a band is never wider or
thinner than the comets it carries.

    links  {"a|b": {calls, errors, top_tools: [{tool, calls}] (at most 3)}}  per link
           (forward direction, `map.link_key`): the events that travel it, those that
           ended in error, the tools (MCP calls) or labels most seen on it
    nodes  {key: per_min}  events touching the node, per minute
    head   calls_per_min (agents' MCP calls), orders_today (orders created today, local
           time, portfolios in the totals), errors_window, mcp_latency_ms (mean, or None)
"""

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.config import get_settings
from smtrading.engine.models import Order
from smtrading.flow.events import collect_events
from smtrading.flow.map import link_key
from smtrading.portfolios.models import total_portfolio_ids

WINDOWS = (1, 5, 15)
SUMMARY_LIMIT = 5000
TOP_TOOLS = 3

# the name of a non-MCP event on its links
_KIND_LABEL = {"order_sent": "ordine inviato", "order_rejected": "ordine rifiutato",
               "fill": "esecuzione", "auto": "regola automatica"}


def _tool(event: dict) -> str:
    if event["kind"] in ("mcp", "telegram", "export"):
        return event["hops"][0]
    return _KIND_LABEL.get(event["kind"], event["kind"])


def _per_min(n: int, window_min: int) -> float:
    return round(n / window_min, 2)


async def _orders_today(session: AsyncSession, now: datetime, tz: str) -> int:
    local = now.astimezone(ZoneInfo(tz))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return await session.scalar(
        select(func.count()).select_from(Order)
        .where(Order.created_at >= midnight, Order.created_at <= now,
               Order.portfolio_id.in_(total_portfolio_ids()))) or 0


async def flow_summary(session: AsyncSession, *, window_min: int, now: datetime,
                       tz: str | None = None) -> dict:
    """The summary of the last `window_min` minutes before `now`; "today" in `tz` (default:
    the configured time zone)."""
    events = await collect_events(session, since=now - timedelta(minutes=window_min),
                                  until=now, limit=SUMMARY_LIMIT)
    calls: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    tools: dict[str, Counter[str]] = defaultdict(Counter)
    touches: Counter[str] = Counter()
    mcp_latencies: list[int] = []
    errors_window = 0
    for e in events:
        tool, failed = _tool(e), e["outcome"] == "error"
        errors_window += failed
        if e["kind"] == "mcp":
            mcp_latencies.append(e["latency_ms"] or 0)
        for key in {link_key(a, b) for a, b in zip(e["path"], e["path"][1:], strict=False)}:
            calls[key] += 1
            errors[key] += failed
            tools[key][tool] += 1
        for node in set(e["path"]):
            touches[node] += 1
    return {
        "links": {k: {"calls": n, "errors": errors[k],
                      "top_tools": [{"tool": t, "calls": c}
                                    for t, c in tools[k].most_common(TOP_TOOLS)]}
                  for k, n in calls.items()},
        "nodes": {k: _per_min(n, window_min) for k, n in touches.items()},
        "head": {
            "calls_per_min": _per_min(len(mcp_latencies), window_min),
            "orders_today": await _orders_today(session, now, tz or get_settings().tz),
            "errors_window": errors_window,
            "mcp_latency_ms": (round(sum(mcp_latencies) / len(mcp_latencies))
                               if mcp_latencies else None),
        },
    }
