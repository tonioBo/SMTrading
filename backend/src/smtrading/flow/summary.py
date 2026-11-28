"""The «Fiume dei flussi» summary of a window (1, 5 or 15 minutes, or `today`: since
00:00 of the configured time zone, Europe/Rome), built from the same
events as the river (one translation path: `flow.events`), so a band is never wider or
thinner than the comets it carries.

    links  {"a|b": {calls, errors, top_tools: [{tool, calls}] (at most 3)}}  per link
           (forward direction, `map.link_key`): the events that travel it, those that
           ended in error, the tools (MCP calls) or labels most seen on it
    nodes  {key: per_min}  events touching the node, per minute
    head   calls_per_min (agents' MCP calls), orders_today (orders created today, local
           time, portfolios in the totals), errors_window, mcp_latency_ms (mean, or None),
           truncated (the window held more events than read: the counts are a floor)

`flow_tools` (the lens): per family every catalog tool with its calls, errors and mean
time in the window, the unknown methods seen apart (`other`), and per link every tool or
label that travelled it.
"""

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.config import get_settings
from smtrading.engine.models import Order
from smtrading.flow.events import collect_events
from smtrading.flow.map import destination, families, family, link_key
from smtrading.portfolios.models import total_portfolio_ids

WINDOWS = (1, 5, 15)
TODAY = "today"
SUMMARY_LIMIT = 5000
TODAY_LIMIT = 20000  # a whole day of events (the agents' calls) read at most
TOP_TOOLS = 3

Window = int | str

# the name of a non-MCP event on its links
_KIND_LABEL = {"order_sent": "ordine inviato", "order_rejected": "ordine rifiutato",
               "fill": "esecuzione", "auto": "regola automatica"}


def _tool(event: dict) -> str:
    if event["kind"] in ("mcp", "telegram", "export"):
        return event["hops"][0]
    return _KIND_LABEL.get(event["kind"], event["kind"])


def _per_min(n: int, window_min: float) -> float:
    return round(n / window_min, 2)


def today_start(now: datetime, tz: str) -> datetime:
    """00:00 of `now`'s day in `tz`."""
    local = now.astimezone(ZoneInfo(tz))
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def window_bounds(window: Window, now: datetime, tz: str) -> tuple[datetime, float, int]:
    """(since, minutes, read limit) of a window: 1, 5, 15 (minutes) or `today`."""
    if window == TODAY:
        since = today_start(now, tz)
        return since, max((now - since).total_seconds() / 60, 1.0), TODAY_LIMIT
    if window not in WINDOWS:
        raise ValueError(f"unknown window {window!r}")
    return now - timedelta(minutes=int(window)), float(window), SUMMARY_LIMIT


async def _orders_today(session: AsyncSession, now: datetime, tz: str) -> int:
    midnight = today_start(now, tz)
    return await session.scalar(
        select(func.count()).select_from(Order)
        .where(Order.created_at >= midnight, Order.created_at <= now,
               Order.portfolio_id.in_(total_portfolio_ids()))) or 0


async def flow_summary(session: AsyncSession, *, now: datetime, window: Window | None = None,
                       window_min: int | None = None, tz: str | None = None) -> dict:
    """The summary of `window` (1, 5, 15 minutes before `now`, or `today`; `window_min`:
    the same in minutes); "today" in `tz` (default: the configured time zone)."""
    tz = tz or get_settings().tz
    since, window_min, limit = window_bounds(window if window is not None else window_min,
                                             now, tz)
    events = await collect_events(session, since=since, until=now, limit=limit)
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
            "orders_today": await _orders_today(session, now, tz),
            "errors_window": errors_window,
            "mcp_latency_ms": (round(sum(mcp_latencies) / len(mcp_latencies))
                               if mcp_latencies else None),
            "truncated": len(events) >= limit,
        },
    }


class _Stat:
    __slots__ = ("calls", "errors", "ms", "timed")

    def __init__(self) -> None:
        self.calls = self.errors = self.ms = self.timed = 0

    def add(self, event: dict) -> None:
        self.calls += 1
        self.errors += event["outcome"] == "error"
        if event["kind"] == "mcp":
            self.ms += event["latency_ms"] or 0
            self.timed += 1

    def dto(self, tool: str, **extra) -> dict:
        return {"tool": tool, **extra, "calls": self.calls, "errors": self.errors,
                "avg_ms": round(self.ms / self.timed) if self.timed else None}


async def flow_tools(session: AsyncSession, *, window: Window, now: datetime,
                     tz: str | None = None) -> dict:
    """{window, families: [{key, label, tools: [{tool, dest, calls, errors, avg_ms}]}],
    other: [{tool, dest, calls, errors, avg_ms}], links: {"a|b": [{tool, calls, errors,
    avg_ms}]}, truncated}: the lens of the window, from the river's own events. A resource
    read (`resource:<uri>`) counts in its family after the catalog tools."""
    tz = tz or get_settings().tz
    since, _, limit = window_bounds(window, now, tz)
    events = await collect_events(session, since=since, until=now, limit=limit)
    by_tool: dict[str, _Stat] = defaultdict(_Stat)
    by_link: dict[str, dict[str, _Stat]] = defaultdict(lambda: defaultdict(_Stat))
    for e in events:
        tool = _tool(e)
        if e["kind"] == "mcp":
            by_tool[tool].add(e)
        for key in {link_key(a, b) for a, b in zip(e["path"], e["path"][1:], strict=False)}:
            by_link[key][tool].add(e)
    fams = []
    for f in families():
        tools = [by_tool.get(t, _Stat()).dto(t, dest=destination(t)) for t in f["tools"]]
        tools += [by_tool[t].dto(t, dest=destination(t)) for t in sorted(by_tool)
                  if t.startswith("resource:") and family(t) == f["key"]]
        fams.append({"key": f["key"], "label": f["label"], "tools": tools})
    other = [by_tool[t].dto(t, dest=destination(t)) for t in sorted(by_tool)
             if family(t) is None]
    links = {k: sorted((st.dto(t) for t, st in tools.items()),
                       key=lambda d: (-d["calls"], d["tool"]))
             for k, tools in by_link.items()}
    return {"window": window, "families": fams, "other": other, "links": links,
            "truncated": len(events) >= limit}
