"""Admin API «Posizioni»: the open positions of every non-archived portfolio with their
protective orders and the risk at the stops (positions.open_view). Read-only (a viewer
can read it).

`GET /api/positions/open` → {as_of, positions: [{portfolio_id, portfolio_name, symbol,
name, market, currency, side, qty, avg_price, opened_at, last, stale, value_eur, pnl_eur,
pnl_pct, stop: {order_id, price, tif}|null, target: {...}|null, distance_to_stop_pct,
risk_at_stop_eur, protection: "ok"|"senza stop"|"copre parziale"|"scade oggi",
extended: {session: "pre"|"post", price, change_pct_vs_close, ts}|null (US, regular
session closed; information only), extended_beyond_stop, plan_state (pending | grace |
closing | null: closing at the end of a «piano»), plan_grace_until}],
portfolios: [{portfolio_id, portfolio_name, mode..., positions, value_eur, pnl_eur,
risk_at_stop_eur, risk_unknown, unprotected, partial_cover, expiring_today,
daily_loss_limit_eur, risk_pct_of_limit}]}:
numbers as strings; a value that cannot be computed (no quote, no FX) is null.
"""

from fastapi import APIRouter, Depends, Request

from smtrading.admin.auth import DbSession, RedisClient, require_access
from smtrading.admin.common import Quotes
from smtrading.mcp.dto import to_jsonable
from smtrading.positions.open_view import open_positions

router = APIRouter(prefix="/api", tags=["positions"], dependencies=[Depends(require_access)])


@router.get("/positions/open")
async def positions_open(
    request: Request, session: DbSession, redis: RedisClient, qs: Quotes
) -> dict:
    router_ = request.app.state.mcp_runtime.router
    return to_jsonable(await open_positions(session, qs, redis=redis, router=router_))
