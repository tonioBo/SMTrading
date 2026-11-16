"""Admin API: the dashboard summary and the aggregated equity curve. Every route requires
an admin session (router level). Read-only: nothing is committed.

- `GET /api/dashboard`: totals over the portfolios that could be valued (a portfolio whose
  valuation fails is listed with `error` set to the code and left out of the totals; one
  valued on cached quotes lists them in `stale_symbols`). `day_pnl_eur` sums the defined
  day P&Ls; `day_pnl_pct` is the summed day P&L over the summed day_open equity of the
  portfolios with a defined day %. An unreadable heartbeat gives `worker_heartbeat_age_s`
  null.
- `GET /api/weekly-goal`: the weekly goal of the home page (total and per portfolio, from
  `telegram.weekly_goal`, the same figures as the Telegram goal line).
- `GET /api/dashboard/equity?period=`: the `periodic` snapshots binned in SQL (UTC) to
  `bucket_s` = max(300s, period span / 500; for `all` the span since the first snapshot);
  per portfolio the last snapshot of each bucket. The total of a bucket sums every
  portfolio's latest value at or before it (a portfolio missing from a bucket carries its
  previous value, seeded with its last snapshot before the window: no false dips). Every
  series is also capped to 500 points.

Archived portfolios (Task 13) are left out of everything: totals, list, margin alerts and
the equity curves (their whole history).
"""

import logging
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from redis.exceptions import RedisError
from sqlalchemy import and_, func, select, text
from sqlalchemy.dialects.postgresql import distinct_on

from smtrading.admin.auth import DbSession, RedisClient, require_access
from smtrading.admin.common import Quotes
from smtrading.common.config import get_settings
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.heartbeat import heartbeat_age_s
from smtrading.common.money import quantize_money
from smtrading.connectors.models import Connector
from smtrading.mcp.dto import to_jsonable
from smtrading.performance.service import MAX_SERIES_POINTS, PERIODS, SPANS, downsample
from smtrading.portfolios.mode import (
    PortfolioMode,
    admin_mode_fields,
    mode_counts,
    mode_fields,
    portfolio_links,
)
from smtrading.portfolios.models import Portfolio, PortfolioStatus, live_portfolio_ids
from smtrading.portfolios.queries import day_pnl_detail
from smtrading.positions.models import Position
from smtrading.risk.models import EquitySnapshot, Notification, SnapshotKind
from smtrading.risk.settings import is_kill_switch_on
from smtrading.telegram.weekly_goal import weekly_goal_view
from smtrading.valuation.service import value_portfolio

log = logging.getLogger("smtrading.admin.dashboard")
router = APIRouter(prefix="/api", tags=["dashboard"], dependencies=[Depends(require_access)])

MARGIN_ALERT_KINDS = ("margin_warning", "liquidation")
MAX_MARGIN_ALERTS = 10
MIN_BUCKET_S = 300
_EPOCH = "1970-01-01 00:00:00+00"
_PCT_Q = Decimal("0.0001")


@router.get("/dashboard/modes")
async def dashboard_modes(session: DbSession) -> dict[str, int]:
    """Live portfolios per mode (cheap: no valuation), for the global mode banner."""
    modes = {m.value: 0 for m in PortfolioMode}
    portfolios = list(await session.scalars(
        select(Portfolio).where(Portfolio.status != PortfolioStatus.archived)
    ))
    links = await portfolio_links(session, portfolios)
    for p in portfolios:
        modes[mode_fields(p, links.get(p.broker_link_id))["mode"].value] += 1
    return modes


def _now() -> datetime:
    return datetime.now(UTC)


@router.get("/weekly-goal")
async def weekly_goal(session: DbSession) -> dict[str, Any]:
    return to_jsonable(await weekly_goal_view(session, _now(), ZoneInfo(get_settings().tz)))


@router.get("/dashboard")
async def dashboard(session: DbSession, redis: RedisClient, qs: Quotes) -> dict[str, Any]:
    portfolios = list(await session.scalars(
        select(Portfolio).where(Portfolio.status != PortfolioStatus.archived)
        .order_by(Portfolio.name)
    ))
    total_equity = Decimal(0)
    total_pnl = Decimal(0)
    pct_pnl = Decimal(0)  # the % covers only the portfolios with a defined day %
    pct_base = Decimal(0)
    items = []
    links = await portfolio_links(session, portfolios)
    for p in portfolios:
        item: dict[str, Any] = {
            "id": str(p.id), "name": p.name, "status": to_jsonable(p.status),
            **to_jsonable(admin_mode_fields(p, links.get(p.broker_link_id))),
            "equity_eur": None, "day_pnl_eur": None, "day_pnl_pct": None,
            "stale_symbols": [], "error": None,
        }
        items.append(item)
        try:
            v = await value_portfolio(session, qs, p.id, redis=redis)
            pnl, pct, base = await day_pnl_detail(session, qs, p, v)
        except SMTError as e:
            item["error"] = str(e.code)
            continue
        total_equity += v.equity_eur
        if pnl is not None:
            total_pnl += pnl
        if pct is not None:
            pct_pnl += pnl
            pct_base += base
        item.update(
            equity_eur=to_jsonable(v.equity_eur), day_pnl_eur=to_jsonable(pnl),
            day_pnl_pct=to_jsonable(pct), stale_symbols=list(v.stale_symbols),
        )

    modes = {m.value: 0 for m in PortfolioMode}
    for item in items:
        modes[item["mode"]] += 1

    total_pct = None
    if pct_base > 0:
        total_pct = (pct_pnl / pct_base * 100).quantize(_PCT_Q)
    connectors = (await session.execute(
        select(Connector.name, Connector.enabled, Connector.health_status,
               Connector.last_checked_at).order_by(Connector.name)
    )).all()
    alerts = list(await session.scalars(
        select(Notification)
        .where(
            Notification.kind.in_(MARGIN_ALERT_KINDS),
            Notification.portfolio_id.in_(live_portfolio_ids()),
        )
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(MAX_MARGIN_ALERTS)
    ))
    return {
        "total_equity_eur": to_jsonable(quantize_money(total_equity, "EUR")),
        "day_pnl_eur": to_jsonable(quantize_money(total_pnl, "EUR")),
        "day_pnl_pct": to_jsonable(total_pct),
        "portfolios": items,
        "modes": modes,
        "mode_counts": mode_counts((p, links.get(p.broker_link_id)) for p in portfolios),
        "connectors": [
            {"name": name, "enabled": enabled, "health_status": health,
             "last_checked_at": to_jsonable(checked)}
            for name, enabled, health, checked in connectors
        ],
        "margin_alerts": [
            {"id": n.id, "portfolio_id": to_jsonable(n.portfolio_id), "kind": n.kind,
             "payload": n.payload, "created_at": to_jsonable(n.created_at)}
            for n in alerts
        ],
        "kill_switch": await is_kill_switch_on(session),
        # the web app follows their quotes (WS) to refresh the values in real time
        "held_symbols": list(await session.scalars(
            select(Position.symbol).distinct()
            .where(Position.portfolio_id.in_(live_portfolio_ids()), Position.qty != 0)
            .order_by(Position.symbol)
        )),
        "worker_heartbeat_age_s": await _heartbeat_age(redis),
    }


async def _heartbeat_age(redis) -> float | None:
    try:
        return await heartbeat_age_s(redis)
    except (RedisError, OSError, ValueError):
        log.warning("worker heartbeat unreadable", exc_info=True)
        return None


def _series(points: list[tuple[datetime, Decimal]]) -> list[dict]:
    return [
        {"ts": to_jsonable(ts), "equity_eur": to_jsonable(quantize_money(eq, "EUR"))}
        for ts, eq in downsample(points)
    ]


def _bucket_seconds(span: timedelta) -> int:
    """At least 5 minutes; wider for long periods, so a series has about 500 buckets."""
    return max(MIN_BUCKET_S, math.ceil(span.total_seconds() / MAX_SERIES_POINTS))


@router.get("/dashboard/equity")
async def dashboard_equity(session: DbSession, period: str = "1m") -> dict[str, Any]:
    if period not in SPANS:
        raise SMTError(
            ErrorCode.INVALID_ORDER, f"period must be one of {list(PERIODS)}",
            {"field": "period", "allowed": list(PERIODS)},
        )
    now = datetime.now(UTC)
    periodic = and_(
        EquitySnapshot.kind == SnapshotKind.periodic,
        EquitySnapshot.portfolio_id.in_(live_portfolio_ids()),
    )
    span = SPANS[period]
    start = None if span is None else now - span
    if span is None:
        first = await session.scalar(select(func.min(EquitySnapshot.ts)).where(periodic))
        span = now - first if first is not None else timedelta(0)
    bucket_s = _bucket_seconds(span)

    bucket = func.date_bin(
        text(f"interval '{bucket_s} seconds'"), EquitySnapshot.ts, text(f"timestamptz '{_EPOCH}'"),
    ).label("bucket")
    stmt = (
        select(EquitySnapshot.portfolio_id, bucket, EquitySnapshot.equity_eur)
        .ext(distinct_on(EquitySnapshot.portfolio_id, bucket))
        .where(periodic)
        .order_by(
            EquitySnapshot.portfolio_id, bucket, EquitySnapshot.ts.desc(),
            EquitySnapshot.id.desc(),
        )
    )
    # each portfolio's last value before the window opens the carried totals
    seeds: dict[Any, Decimal] = {}
    if start is not None:
        stmt = stmt.where(EquitySnapshot.ts >= start)
        seeds = dict((await session.execute(
            select(EquitySnapshot.portfolio_id, EquitySnapshot.equity_eur)
            .ext(distinct_on(EquitySnapshot.portfolio_id))
            .where(periodic, EquitySnapshot.ts < start)
            .order_by(
                EquitySnapshot.portfolio_id, EquitySnapshot.ts.desc(), EquitySnapshot.id.desc()
            )
        )).all())
    rows = (await session.execute(stmt)).all()

    per_portfolio: dict[Any, list[tuple[datetime, Decimal]]] = {}
    by_bucket: dict[datetime, dict[Any, Decimal]] = {}
    for pid, ts, equity in rows:
        per_portfolio.setdefault(pid, []).append((ts, equity))
        by_bucket.setdefault(ts, {})[pid] = equity
    latest = dict(seeds)
    total: list[tuple[datetime, Decimal]] = []
    for ts in sorted(by_bucket):
        latest.update(by_bucket[ts])
        total.append((ts, sum(latest.values(), Decimal(0))))

    names = dict((await session.execute(
        select(Portfolio.id, Portfolio.name).where(Portfolio.id.in_(list(per_portfolio)))
    )).all()) if per_portfolio else {}
    portfolios = [
        {"id": str(pid), "name": names.get(pid), "series": _series(points)}
        for pid, points in sorted(per_portfolio.items(), key=lambda kv: names.get(kv[0], ""))
    ]
    return {
        "period": period, "bucket_s": bucket_s, "series": _series(total),
        "portfolios": portfolios,
    }
