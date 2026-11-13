"""Admin API: the dashboard summary and the aggregated equity curve. Every route requires
an admin session (router level). Read-only: nothing is committed.

- `GET /api/dashboard`: totals over the portfolios that could be valued (a portfolio whose
  valuation fails is listed with `error` set to the code and left out of the totals; one
  valued on cached quotes lists them in `stale_symbols`). `day_pnl_pct` is the summed day
  P&L over the summed day_open equity of the portfolios that have a day_open today.
- `GET /api/dashboard/equity?period=`: the `periodic` snapshots bucketed to 5 minutes
  (UTC); per portfolio the last snapshot of each bucket; the total of a bucket sums every
  portfolio's latest value at or before it (a portfolio missing from one bucket carries
  its previous value: no false dips). Every series is downsampled to at most 500 points.
"""

import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import distinct_on

from smtrading.admin.auth import DbSession, RedisClient, require_admin
from smtrading.admin.routes_portfolios import Quotes
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import quantize_money
from smtrading.connectors.models import Connector
from smtrading.mcp.dto import to_jsonable
from smtrading.performance.service import _SPANS, PERIODS, _downsample
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.queries import day_pnl, today_day_open
from smtrading.risk.models import EquitySnapshot, Notification, SnapshotKind
from smtrading.risk.settings import is_kill_switch_on
from smtrading.valuation.service import value_portfolio

router = APIRouter(prefix="/api", tags=["dashboard"], dependencies=[Depends(require_admin)])

HEARTBEAT_KEY = "worker:heartbeat"  # written by the worker (smtrading.worker.main)
MARGIN_ALERT_KINDS = ("margin_warning", "liquidation")
MAX_MARGIN_ALERTS = 10
BUCKET_S = 300
_PCT_Q = Decimal("0.0001")


@router.get("/dashboard")
async def dashboard(session: DbSession, redis: RedisClient, qs: Quotes) -> dict[str, Any]:
    portfolios = list(await session.scalars(select(Portfolio).order_by(Portfolio.name)))
    total_equity = Decimal(0)
    total_pnl = Decimal(0)
    pnl_base = Decimal(0)
    items = []
    for p in portfolios:
        item: dict[str, Any] = {
            "id": str(p.id), "name": p.name, "status": to_jsonable(p.status),
            "equity_eur": None, "day_pnl_eur": None, "day_pnl_pct": None,
            "stale_symbols": [], "error": None,
        }
        items.append(item)
        try:
            v = await value_portfolio(session, qs, p.id, redis=redis)
            pnl, pct = await day_pnl(session, qs, p, v)
            day_open = await today_day_open(session, p, v.ts) if pnl is not None else None
        except SMTError as e:
            item["error"] = str(e.code)
            continue
        total_equity += v.equity_eur
        if pnl is not None and day_open is not None:
            total_pnl += pnl
            pnl_base += day_open.equity_eur
        item.update(
            equity_eur=to_jsonable(v.equity_eur), day_pnl_eur=to_jsonable(pnl),
            day_pnl_pct=to_jsonable(pct), stale_symbols=list(v.stale_symbols),
        )

    total_pct = None
    if pnl_base > 0:
        total_pct = (total_pnl / pnl_base * 100).quantize(_PCT_Q)
    connectors = (await session.execute(
        select(Connector.name, Connector.enabled, Connector.health_status,
               Connector.last_checked_at).order_by(Connector.name)
    )).all()
    alerts = list(await session.scalars(
        select(Notification)
        .where(Notification.kind.in_(MARGIN_ALERT_KINDS))
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(MAX_MARGIN_ALERTS)
    ))
    return {
        "total_equity_eur": to_jsonable(quantize_money(total_equity, "EUR")),
        "day_pnl_eur": to_jsonable(quantize_money(total_pnl, "EUR")),
        "day_pnl_pct": to_jsonable(total_pct),
        "portfolios": items,
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
        "worker_heartbeat_age_s": await _heartbeat_age(redis),
    }


async def _heartbeat_age(redis) -> float | None:
    raw = await redis.get(HEARTBEAT_KEY)
    if raw is None:
        return None
    return max(0.0, time.time() - float(raw))


def _series(points: list[tuple[datetime, Decimal]]) -> list[dict]:
    return [
        {"ts": to_jsonable(ts), "equity_eur": to_jsonable(quantize_money(eq, "EUR"))}
        for ts, eq in _downsample(points)
    ]


@router.get("/dashboard/equity")
async def dashboard_equity(session: DbSession, period: str = "1m") -> dict[str, Any]:
    if period not in _SPANS:
        raise SMTError(
            ErrorCode.INVALID_ORDER, f"period must be one of {list(PERIODS)}",
            {"field": "period", "allowed": list(PERIODS)},
        )
    bucket = func.date_bin(
        text(f"interval '{BUCKET_S} seconds'"), EquitySnapshot.ts,
        text("timestamptz '1970-01-01 00:00:00+00'"),
    ).label("bucket")
    stmt = (
        select(EquitySnapshot.portfolio_id, bucket, EquitySnapshot.equity_eur)
        .ext(distinct_on(EquitySnapshot.portfolio_id, bucket))
        .where(EquitySnapshot.kind == SnapshotKind.periodic)
        .order_by(
            EquitySnapshot.portfolio_id, bucket, EquitySnapshot.ts.desc(),
            EquitySnapshot.id.desc(),
        )
    )
    span = _SPANS[period]
    if span is not None:
        stmt = stmt.where(EquitySnapshot.ts >= datetime.now(UTC) - span)
    rows = (await session.execute(stmt)).all()

    per_portfolio: dict[Any, list[tuple[datetime, Decimal]]] = {}
    by_bucket: dict[datetime, dict[Any, Decimal]] = {}
    for pid, ts, equity in rows:
        per_portfolio.setdefault(pid, []).append((ts, equity))
        by_bucket.setdefault(ts, {})[pid] = equity
    latest: dict[Any, Decimal] = {}
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
    return {"period": period, "series": _series(total), "portfolios": portfolios}
