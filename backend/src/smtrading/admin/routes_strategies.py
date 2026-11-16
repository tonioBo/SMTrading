"""Admin API «Strategie»: results per strategy (performance.strategies). Read-only (a
viewer can read it).

`GET /api/strategies/performance` → {as_of, strategies: [{ref, name, status,
portfolio_id, portfolio_name, trades, closed_trades, open_positions, win_rate_pct,
realized_pnl_eur, unrealized_pnl_eur, costs_eur, total_pnl_eur, first_trade_at,
last_trade_at, trade_list: [{symbol, direction, order_id, client_order_id, opened_at,
closed_at, qty, open_qty, currency, entry_price, exit_price, mark, realized_pnl_eur,
unrealized_pnl_eur, costs_eur, pnl_eur}]}]}: numbers as strings, null when unknown (no
quote or FX rate: shown «—»); `ref` null is «Senza strategia». By portfolio name, then
ref; trades newest first.
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends

from smtrading.admin.auth import DbSession, RedisClient, require_access
from smtrading.admin.common import Quotes
from smtrading.mcp.dto import to_jsonable
from smtrading.performance.strategies import strategy_performance

router = APIRouter(prefix="/api", tags=["strategies"], dependencies=[Depends(require_access)])


@router.get("/strategies/performance")
async def performance(session: DbSession, redis: RedisClient, qs: Quotes) -> dict[str, Any]:
    rows = await strategy_performance(session, qs, redis=redis)
    return to_jsonable({"as_of": datetime.now(UTC), "strategies": rows})
