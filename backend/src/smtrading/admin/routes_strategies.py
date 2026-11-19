"""Admin API «Strategie»: results per strategy (performance.strategies). Read-only (a
viewer can read it).

`GET /api/strategies/performance` → {as_of, strategies: [{ref, name, status,
portfolio_id, portfolio_name, trades, closed_trades, open_positions, win_rate_pct,
realized_pnl_eur, unrealized_pnl_eur, costs_eur, total_pnl_eur, first_trade_at,
last_trade_at, trade_list: [{symbol, direction, order_id, client_order_id, opened_at,
closed_at, qty, open_qty, currency, entry_price, exit_price, mark, realized_pnl_eur,
unrealized_pnl_eur, costs_eur, pnl_eur}]}]}: numbers as strings, null when unknown (no
quote or FX rate: shown «—»); `ref` null is «Senza strategia». By portfolio name, then
ref; trades newest first. The answer is cached CACHE_TTL_S in redis (the page refreshes
every minute): `as_of` is when it was computed.

`GET /api/strategies/expectations` → {as_of, min_trades, strategies: [{ref, name, status,
approved_at, expected: {source, backtest_id, from_date, to_date, trades, trades_per_month,
win_rate_pct, avg_trade_pct, max_drawdown_pct, sharpe, total_return_pct}, live: {since,
trades, open_trades, closed_trades, wins, win_rate_pct, avg_trade_pct, pnl_eur,
max_drawdown_pct, max_drawdown_eur, trades_per_month, days}, verdict, reasons}]}
(performance.expectations: «previsto contro reale»), cached like the results.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from redis.exceptions import RedisError

from smtrading.admin.auth import DbSession, RedisClient, require_access
from smtrading.admin.common import Quotes
from smtrading.mcp.dto import to_jsonable
from smtrading.performance.expectations import MIN_TRADES, strategy_expectations
from smtrading.performance.strategies import strategy_performance

log = logging.getLogger("smtrading.admin.strategies")
router = APIRouter(prefix="/api", tags=["strategies"], dependencies=[Depends(require_access)])

CACHE_KEY = "strategies:performance:v1"
EXPECTATIONS_KEY = "strategies:expectations:v1"
CACHE_TTL_S = 60


@router.get("/strategies/performance")
async def performance(session: DbSession, redis: RedisClient, qs: Quotes) -> dict[str, Any]:
    try:
        cached = await redis.get(CACHE_KEY)
    except RedisError:
        log.warning("strategies cache read failed", exc_info=True)
        cached = None
    if cached is not None:
        return json.loads(cached)
    rows = await strategy_performance(session, qs, redis=redis)
    body = to_jsonable({"as_of": datetime.now(UTC), "strategies": rows})
    try:
        await redis.set(CACHE_KEY, json.dumps(body), ex=CACHE_TTL_S)
    except RedisError:
        log.warning("strategies cache write failed", exc_info=True)
    return body


@router.get("/strategies/expectations")
async def expectations(session: DbSession, redis: RedisClient, qs: Quotes) -> dict[str, Any]:
    try:
        cached = await redis.get(EXPECTATIONS_KEY)
    except RedisError:
        log.warning("strategy expectations cache read failed", exc_info=True)
        cached = None
    if cached is not None:
        return json.loads(cached)
    rows = await strategy_expectations(session, qs, redis=redis)
    body = to_jsonable({"as_of": datetime.now(UTC), "min_trades": MIN_TRADES,
                        "strategies": rows})
    try:
        await redis.set(EXPECTATIONS_KEY, json.dumps(body), ex=CACHE_TTL_S)
    except RedisError:
        log.warning("strategy expectations cache write failed", exc_info=True)
    return body
